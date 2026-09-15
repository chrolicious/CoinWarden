import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.db import open_db
from core.variants import bonus_ids

AH_CUT = 0.05
COPPER_PER_GOLD = 10_000


def _rows(conn: sqlite3.Connection, sql: str, params: list) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_latest_snapshot(conn: sqlite3.Connection) -> None:
    """Materialise the newest snapshot once; the scoring CTEs read it several
    times and SQLite would otherwise re-scan the whole table for each."""
    conn.executescript("""
        DROP TABLE IF EXISTS temp.latest_snapshot;
        CREATE TEMP TABLE latest_snapshot AS
            SELECT * FROM item_price_snapshots
            WHERE fetched_at = (SELECT MAX(fetched_at) FROM item_price_snapshots);
        CREATE INDEX temp.idx_latest_item ON latest_snapshot (item_id, variant);
    """)


# Cross-realm spread on the latest snapshot, per item variant. Buy at the
# cheapest listing on the cheapest realm; sell by undercutting the cheapest
# listing on the best other realm. The sell realm needs a minimum listing
# count, and its price must sit within a sane multiple of the variant's
# cross-realm anchor (lower-median of the cheapest listing per realm).
# Params: min_sell_listings, sanity_multiple, max_buy_copper, min_profit_copper, min_roi, max_roi, limit
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
    SELECT item_id, variant, realm_slug AS sell_realm, min_unit_price AS sell_price,
           median_unit_price AS sell_median, listing_count AS sell_listings,
           ROW_NUMBER() OVER (PARTITION BY item_id, variant ORDER BY min_unit_price DESC) AS rn
    FROM latest
    WHERE listing_count >= ?
),
spread AS (
    SELECT b.item_id, b.variant, b.buy_realm, b.buy_price, b.buy_listings,
           s.sell_realm, s.sell_price, s.sell_median, s.sell_listings,
           a.anchor_price, a.realm_count,
           CAST(s.sell_price * (1 - {AH_CUT}) - b.buy_price AS INTEGER) AS net_profit
    FROM buy b
    JOIN anchor a ON a.item_id = b.item_id AND a.variant = b.variant
    JOIN sell s ON s.item_id = b.item_id AND s.variant = b.variant AND s.rn = 1
    WHERE b.rn = 1
      AND s.sell_realm != b.buy_realm
      AND s.sell_price <= a.anchor_price * ?
)
SELECT sp.item_id, sp.variant, i.name, i.quality, i.item_class, i.item_subclass,
       sp.buy_realm, sp.buy_price, sp.buy_listings,
       sp.sell_realm, sp.sell_price, sp.sell_median, sp.sell_listings,
       sp.anchor_price, sp.realm_count, sp.net_profit,
       ROUND(1.0 * sp.net_profit / sp.buy_price, 2) AS roi
FROM spread sp
LEFT JOIN items i ON i.item_id = sp.item_id
WHERE sp.buy_price <= ?
  AND sp.net_profit >= ?
  AND 1.0 * sp.net_profit / sp.buy_price BETWEEN ? AND ?
ORDER BY sp.net_profit DESC
LIMIT ?
"""


def cross_realm_spreads(conn, min_sell_listings: int, sanity_multiple: float, max_buy_gold: int,
                        min_profit_gold: int, min_roi: float, max_roi: float, limit: int) -> list[dict]:
    ensure_latest_snapshot(conn)
    return _rows(conn, CROSS_REALM_SPREAD_SQL,
                 [min_sell_listings, sanity_multiple, max_buy_gold * COPPER_PER_GOLD,
                  min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit])


# Timing flip on a single realm, per item variant: the current cheapest listing
# vs the variant's own baseline. The baseline is the distribution of daily
# typical prices (median of each day's cheapest listing) over the window;
# while fewer than ? days exist it falls back to the hourly cheapest listings.
# Turnover (hour-to-hour listing-count drops) is the liquidity gate and comes
# from the hourly table plus the daily rollups.
# Params: day_window_start, hourly_window_start, min_days, min_samples, min_turnover,
#         min_discount, max_buy_copper, min_profit_copper, min_roi, max_roi, limit
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
scored AS (
    SELECT l.item_id, l.variant, l.realm_slug, l.min_unit_price AS buy_price, l.listing_count,
           b.p25, b.p50, b.p75, b.days, b.samples, b.source, b.turnover_events,
           1.0 - 1.0 * l.min_unit_price / b.p50 AS discount,
           CAST(b.p50 * (1 - {AH_CUT}) - l.min_unit_price AS INTEGER) AS net_profit,
           ROUND(1.0 * (b.p50 - l.min_unit_price) / MAX(b.p75 - b.p25, b.p50 * 0.05), 1) AS zscore
    FROM latest l
    JOIN baseline b ON b.connected_realm_id = l.connected_realm_id AND b.item_id = l.item_id AND b.variant = l.variant
    WHERE b.samples >= ?
      AND b.turnover_events >= ?
)
SELECT sc.item_id, sc.variant, i.name, sc.realm_slug, sc.buy_price, sc.listing_count,
       sc.p25, sc.p50, sc.p75, sc.days, sc.samples, sc.source, sc.turnover_events,
       ROUND(sc.discount, 2) AS discount, sc.net_profit,
       ROUND(1.0 * sc.net_profit / sc.buy_price, 2) AS roi, sc.zscore
FROM scored sc
LEFT JOIN items i ON i.item_id = sc.item_id
WHERE sc.discount >= ?
  AND sc.buy_price <= ?
  AND sc.net_profit >= ?
  AND 1.0 * sc.net_profit / sc.buy_price BETWEEN ? AND ?
ORDER BY sc.net_profit DESC
LIMIT ?
"""


def timing_flips(conn, window_days: int, min_days: int, min_samples: int, min_turnover: int,
                 min_discount: float, max_buy_gold: int, min_profit_gold: int, min_roi: float,
                 max_roi: float, limit: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    day_window_start = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    hourly_window_start = (now - timedelta(days=window_days)).isoformat()
    ensure_latest_snapshot(conn)
    return _rows(conn, TIMING_FLIP_SQL,
                 [day_window_start, hourly_window_start, min_days, min_days, min_days, min_days,
                  min_samples, min_turnover, min_discount, max_buy_gold * COPPER_PER_GOLD,
                  min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit])


# Published with permissive filters; the frontend applies the user's own
# thresholds client-side so tightening them never requires a re-run.
PUBLISH_SPREADS = dict(min_sell_listings=2, sanity_multiple=6.0, max_buy_gold=2_000_000,
                       min_profit_gold=100, min_roi=0.1, max_roi=10.0, limit=5000)
PUBLISH_TIMING = dict(window_days=30, min_days=5, min_samples=24, min_turnover=1, min_discount=0.15,
                      max_buy_gold=2_000_000, min_profit_gold=100, min_roi=0.1, max_roi=10.0, limit=5000)

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
    for sql, bucket in ((HOURLY_HISTORY_SQL, "hourly"), (DAILY_HISTORY_SQL, "daily")):
        for r in _rows(conn, sql, [retention_start]):
            item_id = r.pop("item_id")
            key = f"{item_id}|{r.pop('variant')}"
            shards[item_id % HISTORY_SHARDS].setdefault(key, {"hourly": [], "daily": []})[bucket].append(r)
    published = sum(len(s) for s in shards)
    shard_files = [(f"history/{i}.json", {"generated_at": fetched_at, "shards": HISTORY_SHARDS, "items": s})
                   for i, s in enumerate(shards)]
    storage.put_json_many(shard_files)
    print(f"published {len(spreads)} spreads, {len(timing)} timing flips, {len(items)} items, "
          f"{bonus_count} bonus entries, {published} variant histories in {HISTORY_SHARDS} shards", flush=True)


def _gold(copper: int) -> str:
    return f"{copper / COPPER_PER_GOLD:,.0f}g"


def _add_risk_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-buy", type=int, default=100_000,
                        help="max buy price in gold (position sizing)")
    parser.add_argument("--min-profit", type=int, default=500, help="minimum net profit in gold")
    parser.add_argument("--min-roi", type=float, default=0.2)
    parser.add_argument("--max-roi", type=float, default=4.0,
                        help="opportunities above this are treated as too good to be true")
    parser.add_argument("--limit", type=int, default=30)


def _label(r: dict) -> str:
    name = r["name"] or f"item {r['item_id']}"
    return (f"{name} [{r['variant'][:18]}]" if r["variant"] else name)[:44]


def _print_spreads(rows: list[dict]) -> None:
    print(f"{'item':<46} {'buy':<24} {'sell':<24} {'anchor':>9} {'net':>9} {'roi':>6}")
    for r in rows:
        buy = f"{r['buy_realm'][:10]} {_gold(r['buy_price'])} x{r['buy_listings']}"
        sell = f"{r['sell_realm'][:10]} {_gold(r['sell_price'])} x{r['sell_listings']}"
        print(f"{_label(r):<46} {buy:<24} {sell:<24} {_gold(r['anchor_price']):>9} "
              f"{_gold(r['net_profit']):>9} {r['roi']:>6.0%}")


def _print_timing(rows: list[dict]) -> None:
    print(f"{'item':<46} {'realm':<11} {'now':>9} {'p25':>9} {'p50':>9} {'p75':>9} "
          f"{'base':>6} {'turn':>4} {'disc':>5} {'net':>9} {'roi':>5} {'z':>5}")
    for r in rows:
        base = f"{r['days']}d" if r["source"] == "daily" else f"{r['samples']}h"
        print(f"{_label(r):<46} {r['realm_slug'][:10]:<11} {_gold(r['buy_price']):>9} {_gold(r['p25']):>9} "
              f"{_gold(r['p50']):>9} {_gold(r['p75']):>9} {base:>6} {r['turnover_events']:>4} "
              f"{r['discount']:>5.0%} {_gold(r['net_profit']):>9} {r['roi']:>5.0%} {r['zscore']:>5}")


def main():
    parser = argparse.ArgumentParser(description="CoinWarden scoring queries")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spreads", help="cross-realm spread opportunities from the latest snapshot")
    sp.add_argument("--min-sell-listings", type=int, default=3)
    sp.add_argument("--sanity-multiple", type=float, default=4.0,
                    help="max sell price as a multiple of the cross-realm anchor price")
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
        rows = cross_realm_spreads(conn, args.min_sell_listings, args.sanity_multiple, args.max_buy,
                                   args.min_profit, args.min_roi, args.max_roi, args.limit)
        _print_spreads(rows)
    else:
        rows = timing_flips(conn, args.window_days, args.min_days, args.min_samples, args.min_turnover,
                            args.min_discount, args.max_buy, args.min_profit, args.min_roi, args.max_roi, args.limit)
        _print_timing(rows)
    conn.close()


if __name__ == "__main__":
    main()
