import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.addon_sync import push_events
from core.db import SCHEMA, prune, rollup_days
from core.scoring import cross_realm_spreads


GOLD = 10_000


class PreGameSafetyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.conn.close()

    def _snapshot(self, realm, realm_id, price):
        self.conn.execute(
            "INSERT INTO item_price_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            (realm, realm_id, 1, "", price * GOLD, price * GOLD, 1, 1, self.now.isoformat()),
        )

    def _sale(self, auction_id, price):
        self.conn.execute(
            "INSERT INTO sale_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (2, auction_id, "sell", 1, "", price * GOLD, 1, "LONG", self.now.isoformat(), self.now.isoformat(), "sold"),
        )

    def test_spread_uses_pooled_sale_median_and_current_target(self):
        self._snapshot("buy", 1, 100)
        self._snapshot("sell", 2, 150)
        for auction_id in range(100):
            self._sale(auction_id, 110)
        self._sale(100, 500)
        self.conn.commit()

        row = cross_realm_spreads(self.conn, 2, 6, 10_000, 0, 0, 10, 1)[0]
        self.assertEqual(row["sold_median_7d"], 110 * GOLD)
        self.assertEqual(row["target_sell_price"], 110 * GOLD)
        self.assertEqual(row["net_profit"], int(110 * GOLD * .95) - 100 * GOLD)

    def test_completed_daily_rollup_is_not_replaced_after_hourly_pruning(self):
        for hour in range(24):
            stamp = f"2026-09-17T{hour:02d}:00:00+00:00"
            price = (100 if hour < 12 else 500) * GOLD
            self.conn.execute(
                "INSERT INTO item_price_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                ("realm", 1, 1, "", price, price, 1, 1, stamp),
            )
        self.conn.commit()
        rollup_days(self.conn, "2026-09-17", "2026-09-18")
        prune(self.conn, "2026-09-17T23:00:00+00:00", "2026-01-01", "2026-01-01")
        rollup_days(self.conn, "2026-09-17", "2026-09-18")

        self.assertEqual(self.conn.execute("SELECT typical_price FROM daily_item_prices").fetchone()[0], 100 * GOLD)

    def test_sequenced_events_sync_even_when_they_share_a_second(self):
        events = [
            {"event_id": 1, "ts": 100, "type": "gold_delta", "delta": -1},
            {"event_id": 2, "ts": 100, "type": "gold_delta", "delta": -2},
        ]
        with patch("core.addon_sync.requests.post") as post:
            post.return_value.json.return_value = {"added": 2, "total": 2}
            cursor = push_events("fixture", events, {"event_id": 0, "ts": 99})

        self.assertEqual(post.call_args.kwargs["json"]["events"][1]["event_id"], 2)
        self.assertEqual(cursor, {"event_id": 2, "ts": 100})


if __name__ == "__main__":
    unittest.main()
