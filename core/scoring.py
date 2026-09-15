import argparse
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.db import get_client

AH_CUT = 0.05
COPPER_PER_GOLD = 10_000

# Cross-realm spread on the latest snapshot. Buy at the cheapest listing on the
# cheapest realm; sell by undercutting the cheapest listing on the best other
# realm. The sell realm needs a minimum listing count, and its price must sit
# within a sane multiple of the item's cross-realm anchor (lower-median of the
# cheapest listing per realm) - a realm where every listing is troll-priced
# passes the count gate but not the anchor.
# Params: min_sell_listings, sanity_multiple, max_buy_copper, min_profit_copper, min_roi, max_roi, limit
CROSS_REALM_SPREAD_SQL = f"""
WITH latest AS (
    SELECT *
    FROM item_price_snapshots
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM item_price_snapshots)
),
anchor AS (
    SELECT item_id, min_unit_price AS anchor_price, n AS realm_count
    FROM (
        SELECT item_id, min_unit_price,
               ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY min_unit_price) AS rn,
               COUNT(*) OVER (PARTITION BY item_id) AS n
        FROM latest
    )
    WHERE rn = (n + 1) / 2
),
buy AS (
    SELECT item_id, realm_slug AS buy_realm, min_unit_price AS buy_price,
           listing_count AS buy_listings,
           ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY min_unit_price ASC, listing_count DESC) AS rn
    FROM latest
),
sell AS (
    SELECT item_id, realm_slug AS sell_realm, min_unit_price AS sell_price,
           median_unit_price AS sell_median, listing_count AS sell_listings,
           ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY min_unit_price DESC) AS rn
    FROM latest
    WHERE listing_count >= ?
),
spread AS (
    SELECT b.item_id, b.buy_realm, b.buy_price, b.buy_listings,
           s.sell_realm, s.sell_price, s.sell_median, s.sell_listings,
           a.anchor_price, a.realm_count,
           CAST(s.sell_price * (1 - {AH_CUT}) - b.buy_price AS INTEGER) AS net_profit
    FROM buy b
    JOIN anchor a ON a.item_id = b.item_id
    JOIN sell s ON s.item_id = b.item_id AND s.rn = 1
    WHERE b.rn = 1
      AND s.sell_realm != b.buy_realm
      AND s.sell_price <= a.anchor_price * ?
)
SELECT sp.item_id, i.name, i.quality, i.item_class, i.item_subclass,
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


def cross_realm_spreads(db, min_sell_listings: int, sanity_multiple: float, max_buy_gold: int,
                        min_profit_gold: int, min_roi: float, max_roi: float, limit: int) -> list[tuple]:
    result = db.execute(
        CROSS_REALM_SPREAD_SQL,
        [min_sell_listings, sanity_multiple, max_buy_gold * COPPER_PER_GOLD,
         min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit],
    )
    return list(result.rows)


# Timing flip on a single realm: current cheapest listing vs the item's own
# trailing-window distribution of cheapest listings (p25/p50/p75). Sell target
# is p50 - the price you'd normally have to undercut. Turnover events (listing
# count dropping hour to hour) are the liquidity gate: sold or expired, either
# way the item moves.
# Params: window_start_iso, min_snapshots, min_turnover, min_discount, max_buy_copper,
#         min_profit_copper, min_roi, max_roi, limit
TIMING_FLIP_SQL = f"""
WITH win AS (
    SELECT connected_realm_id, item_id, min_unit_price, listing_count, fetched_at
    FROM item_price_snapshots
    WHERE fetched_at >= ?
),
ranked AS (
    SELECT connected_realm_id, item_id, min_unit_price, listing_count,
           ROW_NUMBER() OVER (PARTITION BY connected_realm_id, item_id ORDER BY min_unit_price) AS rn,
           COUNT(*) OVER (PARTITION BY connected_realm_id, item_id) AS n,
           LAG(listing_count) OVER (PARTITION BY connected_realm_id, item_id ORDER BY fetched_at) AS prev_count
    FROM win
),
stats AS (
    SELECT connected_realm_id, item_id,
           MAX(n) AS snapshots,
           MAX(CASE WHEN rn = (n + 3) / 4 THEN min_unit_price END) AS p25,
           MAX(CASE WHEN rn = (n + 1) / 2 THEN min_unit_price END) AS p50,
           MAX(CASE WHEN rn = (3 * n + 1) / 4 THEN min_unit_price END) AS p75,
           SUM(CASE WHEN prev_count IS NOT NULL AND listing_count < prev_count THEN 1 ELSE 0 END) AS turnover_events
    FROM ranked
    GROUP BY connected_realm_id, item_id
),
latest AS (
    SELECT *
    FROM item_price_snapshots
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM item_price_snapshots)
),
scored AS (
    SELECT l.item_id, l.realm_slug, l.min_unit_price AS buy_price, l.listing_count,
           s.p25, s.p50, s.p75, s.snapshots, s.turnover_events,
           1.0 - 1.0 * l.min_unit_price / s.p50 AS discount,
           CAST(s.p50 * (1 - {AH_CUT}) - l.min_unit_price AS INTEGER) AS net_profit,
           ROUND(1.0 * (s.p50 - l.min_unit_price) / MAX(s.p75 - s.p25, s.p50 * 0.05), 1) AS zscore
    FROM latest l
    JOIN stats s ON s.connected_realm_id = l.connected_realm_id AND s.item_id = l.item_id
    WHERE s.snapshots >= ?
      AND s.turnover_events >= ?
)
SELECT sc.item_id, i.name, sc.realm_slug, sc.buy_price, sc.listing_count,
       sc.p25, sc.p50, sc.p75, sc.snapshots, sc.turnover_events,
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


def timing_flips(db, window_days: int, min_snapshots: int, min_turnover: int, min_discount: float,
                 max_buy_gold: int, min_profit_gold: int, min_roi: float, max_roi: float,
                 limit: int) -> list[tuple]:
    window_start = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    result = db.execute(
        TIMING_FLIP_SQL,
        [window_start, min_snapshots, min_turnover, min_discount, max_buy_gold * COPPER_PER_GOLD,
         min_profit_gold * COPPER_PER_GOLD, min_roi, max_roi, limit],
    )
    return list(result.rows)


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


def _print_spreads(rows: list[tuple]) -> None:
    print(f"{'item':<42} {'buy':<24} {'sell':<24} {'anchor':>9} {'net':>9} {'roi':>6}")
    for (item_id, name, quality, cls, subcls, buy_realm, buy_price, buy_n,
         sell_realm, sell_price, sell_median, sell_n, anchor, realms, net, roi) in rows:
        label = (name or f"item {item_id}")[:40]
        buy = f"{buy_realm[:10]} {_gold(buy_price)} x{buy_n}"
        sell = f"{sell_realm[:10]} {_gold(sell_price)} x{sell_n}"
        print(f"{label:<42} {buy:<24} {sell:<24} {_gold(anchor):>9} {_gold(net):>9} {roi:>6.0%}")


def _print_timing(rows: list[tuple]) -> None:
    print(f"{'item':<42} {'realm':<11} {'now':>9} {'p25':>9} {'p50':>9} {'p75':>9} "
          f"{'snaps':>5} {'turn':>4} {'disc':>5} {'net':>9} {'roi':>5} {'z':>5}")
    for (item_id, name, realm, buy_price, n, p25, p50, p75, snaps, turnover,
         discount, net, roi, z) in rows:
        label = (name or f"item {item_id}")[:40]
        print(f"{label:<42} {realm[:10]:<11} {_gold(buy_price):>9} {_gold(p25):>9} {_gold(p50):>9} "
              f"{_gold(p75):>9} {snaps:>5} {turnover:>4} {discount:>5.0%} {_gold(net):>9} {roi:>5.0%} {z:>5}")


def main():
    parser = argparse.ArgumentParser(description="CoinWarden scoring queries")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spreads", help="cross-realm spread opportunities from the latest snapshot")
    sp.add_argument("--min-sell-listings", type=int, default=3)
    sp.add_argument("--sanity-multiple", type=float, default=4.0,
                    help="max sell price as a multiple of the cross-realm anchor price")
    _add_risk_args(sp)

    tp = sub.add_parser("timing", help="same-realm dips vs the item's own trailing history")
    tp.add_argument("--window-days", type=int, default=14)
    tp.add_argument("--min-snapshots", type=int, default=48,
                    help="hours of history the item must have in the window")
    tp.add_argument("--min-turnover", type=int, default=2,
                    help="hour-to-hour listing-count drops required in the window")
    tp.add_argument("--min-discount", type=float, default=0.3, help="current price below p50 by at least this")
    _add_risk_args(tp)

    args = parser.parse_args()
    db = get_client(Config())
    if args.command == "spreads":
        rows = cross_realm_spreads(db, args.min_sell_listings, args.sanity_multiple, args.max_buy,
                                   args.min_profit, args.min_roi, args.max_roi, args.limit)
        _print_spreads(rows)
    else:
        rows = timing_flips(db, args.window_days, args.min_snapshots, args.min_turnover, args.min_discount,
                            args.max_buy, args.min_profit, args.min_roi, args.max_roi, args.limit)
        _print_timing(rows)
    db.close()


if __name__ == "__main__":
    main()
