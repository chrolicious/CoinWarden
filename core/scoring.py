import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.db import open_db
from core.variants import bonus_ids
from core.backtest import log as log_recommendations

AH_CUT = 0.05
COPPER_PER_GOLD = 10_000
SALES_WINDOW_DAYS = 7

# Deposit risk (v2 addition): the gold lost if a relisted item expires unsold.
# WoW's real deposit formula also applies a per-account reputation discount
# (up to ~90% at max AH faction rep) that we have no way to know, so this is
# deliberately the worst case (no discount) rather than a precise number -
# it biases the ranking safely instead of ignoring the risk. Assumes you
# always relist at the longest duration (48h) to maximise sell odds, which
# is also the worst-case deposit rate.
DEPOSIT_RATE_48H = 0.10
SELL_HORIZON_DAYS = 2.0


def _rows(conn: sqlite3.Connection, sql: str, params: list) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_latest_snapshot(conn: sqlite3.Connection) -> None:
    """Materialise the newest snapshot and a 7-day sales summary once; the
    scoring CTEs read them several times and SQLite would otherwise re-scan
    the tables for each."""
    window_start = (datetime.now(timezone.utc) - timedelta(days=SALES_WINDOW_DAYS)).strftime("%Y-%m-%d")
    conn.executescript(f"""
        DROP TABLE IF EXISTS temp.latest_snapshot;
        CREATE TEMP TABLE latest_snapshot AS
            SELECT * FROM item_price_snapshots
            WHERE fetched_at = (SELECT MAX(fetched_at) FROM item_price_snapshots);
        CREATE INDEX temp.idx_latest_item ON latest_snapshot (item_id, variant);

        DROP TABLE IF EXISTS temp.sales_7d;
        CREATE TEMP TABLE sales_7d AS
            SELECT connected_realm_id, item_id, variant,
                   SUM(sold_count) AS sold_7d,
                   SUM(relist_count) AS relist_7d,
                   SUM(expired_count) AS expired_7d,
                   MAX(sold_median_price) AS sold_median_7d
            FROM daily_sales
            WHERE day >= '{window_start}'
            GROUP BY connected_realm_id, item_id, variant;
        CREATE INDEX temp.idx_sales_7d ON sales_7d (connected_realm_id, item_id, variant);
    """)


# Shared tail: from a `priced` CTE carrying buy_price, net_profit (profit if
# the sale happens, already computed from a *realized* sold price - never the
# current ask), sold_7d, sell_listings (current supply on the sell side) and
# vendor_sell_price, derive expected days-to-sell, P(sale within the relist
# horizon), the worst-case deposit loss, and rank by expected value per day
# of capital locked - not raw profit, so illiquid/expensive-to-relist items
# stop floating to the top on a big-but-unlikely number.
# Params: max_buy_copper, min_profit_copper, min_roi, max_roi, limit
RISK_SCORE_SQL_TAIL = f"""
scored AS (
    SELECT *,
           ROUND(MAX(1.0 * sell_listings * {SALES_WINDOW_DAYS} / sold_7d, 0.1), 1) AS days_to_sell,
           ROUND(COALESCE(vendor_sell_price, 0) * {DEPOSIT_RATE_48H}, 0) AS deposit_estimate
    FROM priced
),
final AS (
    SELECT *,
           ROUND(1 - EXP(-{SELL_HORIZON_DAYS} / days_to_sell), 3) AS p_sold_48h,
           CAST((1 - EXP(-{SELL_HORIZON_DAYS} / days_to_sell)) * net_profit
                - EXP(-{SELL_HORIZON_DAYS} / days_to_sell) * deposit_estimate AS INTEGER) AS expected_value
    FROM scored
)
SELECT *, ROUND(1.0 * net_profit / buy_price, 2) AS roi,
       ROUND(1.0 * expected_value / days_to_sell, 0) AS score_per_day
FROM final
WHERE buy_price <= ?
  AND net_profit >= ?
  AND 1.0 * net_profit / buy_price BETWEEN ? AND ?
ORDER BY score_per_day DESC
LIMIT ?
"""

# Cross-realm spread on the latest snapshot, per item variant. Buy at the
# cheapest listing on the cheapest realm; sell on the other realm with the
# best *actually realized* sold price (not the cheapest current ask - that's
# a wish, not a transaction), gated on a minimum number of confirmed sales in
# the last 7 days so zero-evidence variants never rank on a guess. The buy
# price still needs to sit within a sane multiple of the cross-realm anchor
# (median of the cheapest listing per realm) to filter obvious troll listings.
# Params: min_sold_evidence, sanity_multiple, (RISK_SCORE_SQL_TAIL params)
CROSS_REALM_SPREAD_SQL = f"""
WITH latest AS (
    SELECT * FROM latest_snapshot
),
anchor AS (
    SELECT item_id, variant, min_unit_price AS anchor_price, n AS realm_count
    FROM (
        SELECT item_id, variant, min_unit_price,
               ROW_NUMBER() OVER (PARTITION BY item_id, variant ORDER BY min_unit_price) AS rn,
               COUNT(*) OVER (PARTITION BY item_id, variant) AS n
        FROM latest
    )
    WHERE rn = (n + 1) / 2
),
buy AS (
    SELECT item_id, variant, realm_slug AS buy_realm, min_unit_price AS buy_price,
           listing_count AS buy_listings,
           ROW_NUMBER() OVER (PARTITION BY item_id, variant ORDER BY min_unit_price ASC, listing_count DESC) AS rn
    FROM latest
),
sell AS (
    SELECT l.item_id, l.variant, l.realm_slug AS sell_realm, l.min_unit_price AS current_ask,
           l.listing_count AS sell_listings,
           s.sold_7d, s.sold_median_7d, s.relist_7d,
           ROW_NUMBER() OVER (PARTITION BY l.item_id, l.variant ORDER BY s.sold_median_7d DESC) AS rn
    FROM latest l
    JOIN sales_7d s ON s.connected_realm_id = l.connected_realm_id AND s.item_id = l.item_id AND s.variant = l.variant
    WHERE s.sold_7d >= ?
),
spread AS (
    SELECT b.item_id, b.variant, b.buy_realm, b.buy_price, b.buy_listings,
           s.sell_realm, s.current_ask, s.sell_listings, s.sold_7d, s.sold_median_7d, s.relist_7d,
           a.anchor_price, a.realm_count,
           CAST(s.sold_median_7d * (1 - {AH_CUT}) - b.buy_price AS INTEGER) AS net_profit
    FROM buy b
    JOIN anchor a ON a.item_id = b.item_id AND a.variant = b.variant
    JOIN sell s ON s.item_id = b.item_id AND s.variant = b.variant AND s.rn = 1
    WHERE b.rn = 1
      AND s.sell_realm != b.buy_realm
      AND s.sold_median_7d <= a.anchor_price * ?
),
priced AS (
    SELECT sp.item_id, sp.variant, i.name, i.quality, i.item_class, i.item_subclass,
           i.vendor_sell_price,
           sp.buy_realm, sp.buy_price, sp.buy_listings,
           sp.sell_realm, sp.current_ask, sp.sell_listings AS sell_listings,
           sp.sold_7d AS sold_7d, sp.sold_median_7d, sp.relist_7d,
           sp.anchor_price, sp.realm_count, sp.net_profit
    FROM spread sp
    LEFT JOIN items i ON i.item_id = sp.item_id
),
{RISK_SCORE_SQL_TAIL}
"""


def cross_realm_spreads(conn, min_sold_evidence: int, sanity_multiple: float, max_buy_gold: int,
                        min_profit_gold: int, min_roi: float, max_roi: float, limit: int) -> list[dict]:
    ensure_latest_snapshot(conn)
    return _rows(conn, CROSS_REALM_SPREAD_SQL,
                 [min_sold_evidence, sanity_multiple, max_buy_gold * COPPER_PER_GOLD,
                  min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit])


# Timing flip on a single realm, per item variant: current cheapest listing vs
# the variant's own trailing baseline (daily typical prices once enough days
# exist, hourly cheapest listings before that) - this is the dip-detection
# signal (discount/zscore) and is unaffected by the sales work below, since a
# deviation from the normal *ask* is exactly what "priced below normal" means.
# The profit/ranking side is separate: it uses the realm's own realized sold
# price in the last 7 days, gated the same way as spreads, so a "40% below
# normal ask" item that nobody actually buys still won't rank highly.
# Params: day_window_start, hourly_window_start, min_days, min_samples, min_turnover,
#         min_discount, min_sold_evidence, (RISK_SCORE_SQL_TAIL params)
TIMING_FLIP_SQL = f"""
WITH daily AS (
    SELECT connected_realm_id, item_id, variant, typical_price AS price, turnover_events
    FROM daily_item_prices
    WHERE day >= ?
),
hourly AS (
    SELECT connected_realm_id, item_id, variant, min_unit_price AS price, listing_count, fetched_at,
           LAG(listing_count) OVER (PARTITION BY connected_realm_id, item_id, variant ORDER BY fetched_at) AS prev_count
    FROM item_price_snapshots
    WHERE fetched_at >= ?
),
daily_stats AS (
    SELECT connected_realm_id, item_id, variant, COUNT(*) AS days, SUM(turnover_events) AS turnover,
           MAX(CASE WHEN rn = (n + 3) / 4 THEN price END) AS p25,
           MAX(CASE WHEN rn = (n + 1) / 2 THEN price END) AS p50,
           MAX(CASE WHEN rn = (3 * n + 1) / 4 THEN price END) AS p75
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY connected_realm_id, item_id, variant ORDER BY price) AS rn,
                  COUNT(*) OVER (PARTITION BY connected_realm_id, item_id, variant) AS n
        FROM daily
    )
    GROUP BY connected_realm_id, item_id, variant
),
hourly_stats AS (
    SELECT connected_realm_id, item_id, variant, COUNT(*) AS samples,
           SUM(CASE WHEN prev_count IS NOT NULL AND listing_count < prev_count THEN 1 ELSE 0 END) AS turnover,
           MAX(CASE WHEN rn = (n + 3) / 4 THEN price END) AS p25,
           MAX(CASE WHEN rn = (n + 1) / 2 THEN price END) AS p50,
           MAX(CASE WHEN rn = (3 * n + 1) / 4 THEN price END) AS p75
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY connected_realm_id, item_id, variant ORDER BY price) AS rn,
                  COUNT(*) OVER (PARTITION BY connected_realm_id, item_id, variant) AS n
        FROM hourly
    )
    GROUP BY connected_realm_id, item_id, variant
),
baseline AS (
    SELECT h.connected_realm_id, h.item_id, h.variant,
           CASE WHEN COALESCE(d.days, 0) >= ? THEN 'daily' ELSE 'hourly' END AS source,
           CASE WHEN COALESCE(d.days, 0) >= ? THEN d.p25 ELSE h.p25 END AS p25,
           CASE WHEN COALESCE(d.days, 0) >= ? THEN d.p50 ELSE h.p50 END AS p50,
           CASE WHEN COALESCE(d.days, 0) >= ? THEN d.p75 ELSE h.p75 END AS p75,
           COALESCE(d.days, 0) AS days, h.samples,
           h.turnover + COALESCE(d.turnover, 0) AS turnover_events
    FROM hourly_stats h
    LEFT JOIN daily_stats d ON d.connected_realm_id = h.connected_realm_id AND d.item_id = h.item_id AND d.variant = h.variant
),
latest AS (
    SELECT * FROM latest_snapshot
),
dip AS (
    SELECT l.item_id, l.variant, l.realm_slug, l.min_unit_price AS buy_price, l.listing_count,
           b.p25, b.p50, b.p75, b.days, b.samples, b.source, b.turnover_events,
           s.sold_7d, s.sold_median_7d, s.relist_7d,
           1.0 - 1.0 * l.min_unit_price / b.p50 AS discount,
           ROUND(1.0 * (b.p50 - l.min_unit_price) / MAX(b.p75 - b.p25, b.p50 * 0.05), 1) AS zscore
    FROM latest l
    JOIN baseline b ON b.connected_realm_id = l.connected_realm_id AND b.item_id = l.item_id AND b.variant = l.variant
    JOIN sales_7d s ON s.connected_realm_id = l.connected_realm_id AND s.item_id = l.item_id AND s.variant = l.variant
    WHERE b.samples >= ?
      AND b.turnover_events >= ?
      AND s.sold_7d >= ?
      AND 1.0 - 1.0 * l.min_unit_price / b.p50 >= ?
),
priced AS (
    SELECT d.item_id, d.variant, i.name, i.quality, i.item_class, i.item_subclass, i.vendor_sell_price,
           d.realm_slug, d.buy_price, d.listing_count AS sell_listings,
           d.p25, d.p50, d.p75, d.days, d.samples, d.source, d.turnover_events,
           d.sold_7d, d.sold_median_7d, d.relist_7d, d.discount, d.zscore,
           CAST(d.sold_median_7d * (1 - {AH_CUT}) - d.buy_price AS INTEGER) AS net_profit
    FROM dip d
    LEFT JOIN items i ON i.item_id = d.item_id
),
{RISK_SCORE_SQL_TAIL}
"""


def timing_flips(conn, window_days: int, min_days: int, min_samples: int, min_turnover: int,
                 min_discount: float, min_sold_evidence: int, max_buy_gold: int, min_profit_gold: int,
                 min_roi: float, max_roi: float, limit: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    day_window_start = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    hourly_window_start = (now - timedelta(days=window_days)).isoformat()
    ensure_latest_snapshot(conn)
    return _rows(conn, TIMING_FLIP_SQL,
                 [day_window_start, hourly_window_start, min_days, min_days, min_days, min_days,
                  min_samples, min_turnover, min_sold_evidence, min_discount,
                  max_buy_gold * COPPER_PER_GOLD, min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit])


# Published with permissive filters; the frontend applies the user's own
# thresholds client-side so tightening them never requires a re-run.
PUBLISH_SPREADS = dict(min_sold_evidence=2, sanity_multiple=6.0, max_buy_gold=2_000_000,
                       min_profit_gold=100, min_roi=0.1, max_roi=10.0, limit=5000)
PUBLISH_TIMING = dict(window_days=30, min_days=5, min_samples=24, min_turnover=1, min_discount=0.15,
                      min_sold_evidence=2, max_buy_gold=2_000_000, min_profit_gold=100, min_roi=0.1,
                      max_roi=10.0, limit=5000)

# Histories are published in shards (item_id % HISTORY_SHARDS) for every item
# variant featured in the last FEATURED_RETENTION_DAYS, so an item that drops
# out of the lists - or a favorite - keeps a fresh chart without publishing all
# ~28k variants every hour (R2 write operations are the scarce free-tier resource).
HISTORY_SHARDS = 256
FEATURED_RETENTION_DAYS = 7

HOURLY_HISTORY_SQL = """
SELECT s.item_id, s.variant, s.realm_slug, s.fetched_at, s.min_unit_price, s.median_unit_price, s.listing_count
FROM featured_items f
JOIN item_price_snapshots s ON s.item_id = f.item_id AND s.variant = f.variant
WHERE f.last_featured_at >= ?
ORDER BY s.item_id, s.variant, s.fetched_at
"""

DAILY_HISTORY_SQL = """
SELECT d.item_id, d.variant, d.realm_slug, d.day, d.low_price, d.typical_price, d.high_price,
       d.median_price, d.avg_listing_count, d.turnover_events
FROM featured_items f
JOIN daily_item_prices d ON d.item_id = f.item_id AND d.variant = f.variant
WHERE f.last_featured_at >= ?
ORDER BY d.item_id, d.variant, d.day
"""

SALES_HISTORY_SQL = """
SELECT s.item_id, s.variant, s.realm_slug, s.day, s.sold_count, s.sold_quantity,
       s.sold_min_price, s.sold_median_price, s.sold_max_price, s.relist_count, s.expired_count
FROM featured_items f
JOIN daily_sales s ON s.item_id = f.item_id AND s.variant = f.variant
WHERE f.last_featured_at >= ?
ORDER BY s.item_id, s.variant, s.day
"""


def _publish_bonus_subset(conn: sqlite3.Connection, storage, variants: set[str], fetched_at: str) -> int:
    ids = set()
    for v in variants:
        ids.update(bonus_ids(v))
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT bonus_id, data FROM bonus_data WHERE bonus_id IN ({placeholders})", list(ids)).fetchall()
    table = {str(bonus_id): json.loads(data) for bonus_id, data in rows}
    storage.put_json("bonuses.json", {"generated_at": fetched_at, "bonuses": table}, cache_seconds=3600)
    return len(table)


def publish(conn: sqlite3.Connection, storage, fetched_at: str) -> None:
    spreads = cross_realm_spreads(conn, **PUBLISH_SPREADS)
    timing = timing_flips(conn, **PUBLISH_TIMING)
    storage.put_json("latest/spreads.json", {"generated_at": fetched_at, "rows": spreads})
    storage.put_json("latest/timing.json", {"generated_at": fetched_at, "rows": timing})
    logged = log_recommendations(conn, spreads, timing, fetched_at)

    items = _rows(conn, "SELECT item_id, name, quality, item_class, item_subclass, inventory_type, item_level, icon_url "
                        "FROM items WHERE name IS NOT NULL", [])
    storage.put_json("items.json", {"generated_at": fetched_at, "items": items}, cache_seconds=3600)

    featured = {(r["item_id"], r["variant"]) for r in spreads} | {(r["item_id"], r["variant"]) for r in timing}
    with conn:
        conn.executemany(
            "INSERT INTO featured_items (item_id, variant, last_featured_at) VALUES (?, ?, ?) "
            "ON CONFLICT(item_id, variant) DO UPDATE SET last_featured_at = excluded.last_featured_at",
            [(item_id, variant, fetched_at) for item_id, variant in featured],
        )
    retention_start = (datetime.now(timezone.utc) - timedelta(days=FEATURED_RETENTION_DAYS)).isoformat()
    bonus_count = _publish_bonus_subset(conn, storage, {v for _, v in featured}, fetched_at)

    shards: list[dict[str, dict]] = [{} for _ in range(HISTORY_SHARDS)]
    for sql, bucket in ((HOURLY_HISTORY_SQL, "hourly"), (DAILY_HISTORY_SQL, "daily"), (SALES_HISTORY_SQL, "sales")):
        for r in _rows(conn, sql, [retention_start]):
            item_id = r.pop("item_id")
            key = f"{item_id}|{r.pop('variant')}"
            shards[item_id % HISTORY_SHARDS].setdefault(key, {"hourly": [], "daily": [], "sales": []})[bucket].append(r)
    published = sum(len(s) for s in shards)
    shard_files = [(f"history/{i}.json", {"generated_at": fetched_at, "shards": HISTORY_SHARDS, "items": s})
                   for i, s in enumerate(shards)]
    storage.put_json_many(shard_files)
    print(f"published {len(spreads)} spreads, {len(timing)} timing flips, {len(items)} items, "
          f"{bonus_count} bonus entries, {published} variant histories in {HISTORY_SHARDS} shards, "
          f"{logged} recommendations logged for backtesting", flush=True)


def _gold(copper: int) -> str:
    return f"{copper / COPPER_PER_GOLD:,.0f}g"


def _add_risk_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-buy", type=int, default=100_000,
                        help="max buy price in gold (position sizing)")
    parser.add_argument("--min-profit", type=int, default=500, help="minimum net profit in gold")
    parser.add_argument("--min-roi", type=float, default=0.2)
    parser.add_argument("--max-roi", type=float, default=4.0,
                        help="opportunities above this are treated as too good to be true")
    parser.add_argument("--min-sold-evidence", type=int, default=1,
                        help="minimum confirmed sales in the last 7 days")
    parser.add_argument("--limit", type=int, default=30)


def _label(r: dict) -> str:
    name = r["name"] or f"item {r['item_id']}"
    return (f"{name} [{r['variant'][:18]}]" if r["variant"] else name)[:44]


def _print_spreads(rows: list[dict]) -> None:
    print(f"{'item':<46} {'buy':<24} {'sell realm':<24} {'sold7d':>6} {'d2sell':>6} "
          f"{'p(sold)':>7} {'deposit':>8} {'net':>9} {'EV/day':>8}")
    for r in rows:
        buy = f"{r['buy_realm'][:10]} {_gold(r['buy_price'])} x{r['buy_listings']}"
        sell = f"{r['sell_realm'][:10]} sold@{_gold(r['sold_median_7d'])} x{r['sold_7d']}"
        print(f"{_label(r):<46} {buy:<24} {sell:<24} {r['sold_7d']:>6} {r['days_to_sell']:>6} "
              f"{r['p_sold_48h']:>7.0%} {_gold(int(r['deposit_estimate'])):>8} "
              f"{_gold(r['net_profit']):>9} {_gold(int(r['score_per_day'])):>8}")


def _print_timing(rows: list[dict]) -> None:
    print(f"{'item':<46} {'realm':<11} {'now':>9} {'p50':>9} {'disc':>5} {'sold7d':>6} "
          f"{'d2sell':>6} {'net':>9} {'EV/day':>8}")
    for r in rows:
        print(f"{_label(r):<46} {r['realm_slug'][:10]:<11} {_gold(r['buy_price']):>9} {_gold(r['p50']):>9} "
              f"{r['discount']:>5.0%} {r['sold_7d']:>6} {r['days_to_sell']:>6} "
              f"{_gold(r['net_profit']):>9} {_gold(int(r['score_per_day'])):>8}")


def main():
    parser = argparse.ArgumentParser(description="CoinWarden scoring queries")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spreads", help="cross-realm spread opportunities from the latest snapshot")
    sp.add_argument("--sanity-multiple", type=float, default=4.0,
                    help="max realized sell price as a multiple of the cross-realm anchor price")
    _add_risk_args(sp)

    tp = sub.add_parser("timing", help="same-realm dips vs the variant's own baseline")
    tp.add_argument("--window-days", type=int, default=30)
    tp.add_argument("--min-days", type=int, default=5, help="daily rollups needed before they replace the hourly baseline")
    tp.add_argument("--min-samples", type=int, default=24, help="hourly snapshots the variant must have")
    tp.add_argument("--min-turnover", type=int, default=2, help="listing-count drops required in the window")
    tp.add_argument("--min-discount", type=float, default=0.3, help="current price below p50 by at least this")
    _add_risk_args(tp)

    args = parser.parse_args()
    config = Config()
    conn = open_db(config.data_dir / "coinwarden.sqlite")
    if args.command == "spreads":
        rows = cross_realm_spreads(conn, args.min_sold_evidence, args.sanity_multiple, args.max_buy,
                                   args.min_profit, args.min_roi, args.max_roi, args.limit)
        _print_spreads(rows)
    else:
        rows = timing_flips(conn, args.window_days, args.min_days, args.min_samples, args.min_turnover,
                            args.min_discount, args.min_sold_evidence, args.max_buy, args.min_profit,
                            args.min_roi, args.max_roi, args.limit)
        _print_timing(rows)
    conn.close()


if __name__ == "__main__":
    main()
