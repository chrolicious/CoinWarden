import argparse

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


def _gold(copper: int) -> str:
    return f"{copper / COPPER_PER_GOLD:,.0f}g"


def main():
    parser = argparse.ArgumentParser(description="Cross-realm spread opportunities from the latest snapshot")
    parser.add_argument("--min-sell-listings", type=int, default=3)
    parser.add_argument("--sanity-multiple", type=float, default=4.0,
                        help="max sell price as a multiple of the cross-realm anchor price")
    parser.add_argument("--max-buy", type=int, default=100_000,
                        help="max buy price in gold (position sizing)")
    parser.add_argument("--min-profit", type=int, default=500, help="minimum net profit in gold")
    parser.add_argument("--min-roi", type=float, default=0.2)
    parser.add_argument("--max-roi", type=float, default=4.0,
                        help="spreads above this are treated as too good to be true")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    db = get_client(Config())
    rows = cross_realm_spreads(db, args.min_sell_listings, args.sanity_multiple, args.max_buy,
                               args.min_profit, args.min_roi, args.max_roi, args.limit)
    db.close()

    print(f"{'item':<42} {'buy':<24} {'sell':<24} {'anchor':>9} {'net':>9} {'roi':>6}")
    for (item_id, name, quality, cls, subcls, buy_realm, buy_price, buy_n,
         sell_realm, sell_price, sell_median, sell_n, anchor, realms, net, roi) in rows:
        label = (name or f"item {item_id}")[:40]
        buy = f"{buy_realm[:10]} {_gold(buy_price)} x{buy_n}"
        sell = f"{sell_realm[:10]} {_gold(sell_price)} x{sell_n}"
        print(f"{label:<42} {buy:<24} {sell:<24} {_gold(anchor):>9} {_gold(net):>9} {roi:>6.0%}")


if __name__ == "__main__":
    main()
