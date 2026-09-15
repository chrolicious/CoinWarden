"""Backtest harness: log every published recommendation, then check it against
what actually happened.

- Buy-realism: was the recommended listing's price still achievable on the buy
  realm in the *next* hourly snapshot? This is the honest limit of what hourly
  data lets you act on - a listing gone within the hour was never buyable by
  this tool regardless of how good the recommendation looked.
- Sell-realism: did a real sale (from the diffing in core/listings.py) happen
  for that variant on the sell realm within the sell horizon (default 48h,
  matching the deposit-risk assumption in core/scoring.py)?

Nothing here executes a trade or assumes one happened - it only checks whether
the market behaved the way the recommendation assumed it would.
"""
import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.db import open_db

BUY_WINDOW_MINUTES = 90
BUY_PRICE_TOLERANCE = 1.02


def log(conn: sqlite3.Connection, spreads: list[dict], timing: list[dict], fetched_at: str) -> int:
    rows = []
    for kind, source in (("spread", spreads), ("timing", timing)):
        for rank, r in enumerate(source, start=1):
            sell_realm = r["sell_realm"] if kind == "spread" else r["realm_slug"]
            rows.append((
                fetched_at, kind, r["item_id"], r["variant"], rank,
                r["buy_realm"] if kind == "spread" else r["realm_slug"], r["buy_price"], sell_realm,
                r["sold_median_7d"], r["net_profit"], int(r["score_per_day"]), r["p_sold_48h"], r["days_to_sell"],
            ))
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO recommendation_log "
            "(fetched_at, kind, item_id, variant, rank, buy_realm, buy_price, sell_realm, "
            " sold_median_7d, net_profit, score_per_day, p_sold_48h, days_to_sell) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


EVALUATE_SQL = """
WITH candidates AS (
    SELECT * FROM recommendation_log
    WHERE fetched_at >= ? AND fetched_at <= ?
),
buy_check AS (
    SELECT c.fetched_at, c.kind, c.item_id, c.variant,
           MIN(s.min_unit_price) AS next_min_price
    FROM candidates c
    JOIN item_price_snapshots s
      ON s.realm_slug = c.buy_realm AND s.item_id = c.item_id AND s.variant = c.variant
     AND s.fetched_at > c.fetched_at
     AND s.fetched_at <= datetime(c.fetched_at, ?)
    GROUP BY c.fetched_at, c.kind, c.item_id, c.variant
),
sell_check AS (
    SELECT c.fetched_at, c.kind, c.item_id, c.variant,
           MIN(e.vanished_at) AS first_sold_at,
           MIN(e.unit_price) AS first_sold_price
    FROM candidates c
    JOIN sale_events e
      ON e.realm_slug = c.sell_realm AND e.item_id = c.item_id AND e.variant = c.variant AND e.kind = 'sold'
     AND e.vanished_at > c.fetched_at
     AND e.vanished_at <= datetime(c.fetched_at, ?)
    GROUP BY c.fetched_at, c.kind, c.item_id, c.variant
)
SELECT c.*, b.next_min_price, sc.first_sold_at, sc.first_sold_price
FROM candidates c
LEFT JOIN buy_check b ON b.fetched_at = c.fetched_at AND b.kind = c.kind AND b.item_id = c.item_id AND b.variant = c.variant
LEFT JOIN sell_check sc ON sc.fetched_at = c.fetched_at AND sc.kind = c.kind AND sc.item_id = c.item_id AND sc.variant = c.variant
"""


def evaluate(conn: sqlite3.Connection, min_age_hours: float, sell_horizon_days: float,
            window_start_iso: str | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    window_end = (now - timedelta(hours=min_age_hours)).isoformat()
    window_start = window_start_iso or "2000-01-01"
    cur = conn.execute(
        EVALUATE_SQL,
        [window_start, window_end, f"+{BUY_WINDOW_MINUTES} minutes", f"+{sell_horizon_days} days"],
    )
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["buy_achieved"] = r["next_min_price"] is not None and r["next_min_price"] <= r["buy_price"] * BUY_PRICE_TOLERANCE
        r["sold_within_horizon"] = r["first_sold_at"] is not None
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}

    def rate(pred):
        matched = [r for r in rows if pred(r)]
        return len(matched), (len(matched) / len(rows)) if rows else 0.0

    buyable_n, buyable_rate = rate(lambda r: r["buy_achieved"])
    sold_n, sold_rate = rate(lambda r: r["sold_within_horizon"])
    both_n, both_rate = rate(lambda r: r["buy_achieved"] and r["sold_within_horizon"])

    realized = [r["net_profit"] for r in rows if r["buy_achieved"] and r["sold_within_horizon"]]

    # Calibration: predicted P(sold within 48h) vs actual, bucketed by decile.
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        b = min(9, int(r["p_sold_48h"] * 10))
        buckets.setdefault(b, []).append(r)
    calibration = []
    for b in sorted(buckets):
        bucket_rows = buckets[b]
        actual = sum(1 for r in bucket_rows if r["sold_within_horizon"]) / len(bucket_rows)
        predicted_avg = sum(r["p_sold_48h"] for r in bucket_rows) / len(bucket_rows)
        calibration.append({"bucket": f"{b*10}-{b*10+10}%", "n": len(bucket_rows),
                            "predicted": round(predicted_avg, 3), "actual": round(actual, 3)})

    return {
        "n": len(rows),
        "buy_achieved": {"n": buyable_n, "rate": round(buyable_rate, 3)},
        "sold_within_horizon": {"n": sold_n, "rate": round(sold_rate, 3)},
        "both": {"n": both_n, "rate": round(both_rate, 3)},
        "simulated_net_profit": sum(realized),
        "avg_net_profit_per_realized_flip": (sum(realized) / len(realized)) if realized else 0,
        "calibration": calibration,
    }


def _gold(copper) -> str:
    return f"{copper / 10_000:,.0f}g"


def main():
    parser = argparse.ArgumentParser(description="Evaluate logged recommendations against what actually happened")
    parser.add_argument("--min-age-hours", type=float, default=2.0,
                        help="only evaluate recommendations at least this old (buy-realism needs >=1.5h)")
    parser.add_argument("--sell-horizon-days", type=float, default=2.0)
    parser.add_argument("--days", type=float, default=14, help="how far back to look for logged recommendations")
    args = parser.parse_args()

    config = Config()
    conn = open_db(config.data_dir / "coinwarden.sqlite")
    window_start = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    rows = evaluate(conn, args.min_age_hours, args.sell_horizon_days, window_start)
    conn.close()

    for kind in ("spread", "timing"):
        subset = [r for r in rows if r["kind"] == kind]
        summary = summarize(subset)
        print(f"\n=== {kind} ({summary['n']} recommendations evaluated) ===")
        if summary["n"] == 0:
            continue
        print(f"buy achievable next hour: {summary['buy_achieved']['n']}/{summary['n']} "
              f"({summary['buy_achieved']['rate']:.0%})")
        print(f"sold within {args.sell_horizon_days:.0f}d: {summary['sold_within_horizon']['n']}/{summary['n']} "
              f"({summary['sold_within_horizon']['rate']:.0%})")
        print(f"both (a real, actionable flip): {summary['both']['n']}/{summary['n']} "
              f"({summary['both']['rate']:.0%})")
        print(f"simulated net profit (realized flips only): {_gold(summary['simulated_net_profit'])}")
        if summary["avg_net_profit_per_realized_flip"]:
            print(f"avg profit per realized flip: {_gold(int(summary['avg_net_profit_per_realized_flip']))}")
        print("P(sold in 48h) calibration - predicted vs actual, by predicted decile:")
        for c in summary["calibration"]:
            print(f"  {c['bucket']:>8}  n={c['n']:<5} predicted={c['predicted']:.0%}  actual={c['actual']:.0%}")


if __name__ == "__main__":
    main()
