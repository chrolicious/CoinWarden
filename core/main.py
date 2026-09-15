import faulthandler
import os
import statistics
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core.config import Config
from core.api_client import BattleNetClient
from core.db import open_db, insert_item_snapshots, rollup_days, prune, compact, replace_bonus_data
from core.items import sync_item_metadata
from core.scoring import publish
from core.storage import get_storage
from core.variants import variant_key, fetch_bonus_table, serialize


def _unit_price(auction: dict) -> float | None:
    if auction.get("unit_price") is not None:
        return auction["unit_price"]
    if auction.get("buyout") is not None:
        quantity = auction.get("quantity", 1) or 1
        return auction["buyout"] / quantity
    return None


def _aggregate_by_variant(auctions: list[dict]) -> dict[tuple[int, str], dict]:
    prices: dict[tuple[int, str], list[float]] = defaultdict(list)
    quantity: dict[tuple[int, str], int] = defaultdict(int)

    for auction in auctions:
        price = _unit_price(auction)
        if price is None:
            continue
        key = (auction["item"]["id"], variant_key(auction["item"]))
        prices[key].append(price)
        quantity[key] += auction.get("quantity", 1) or 1

    return {
        key: {
            "min_unit_price": round(min(p)),
            "median_unit_price": round(statistics.median(p)),
            "listing_count": len(p),
            "total_quantity": quantity[key],
        }
        for key, p in prices.items()
    }


def _rows_from_aggregates(realm_slug: str, connected_realm_id: int, aggregates: dict, fetched_at: str) -> list[tuple]:
    return [
        (realm_slug, connected_realm_id, item_id, variant,
         agg["min_unit_price"], agg["median_unit_price"], agg["listing_count"], agg["total_quantity"], fetched_at)
        for (item_id, variant), agg in aggregates.items()
    ]


def main():
    faulthandler.dump_traceback_later(3300, exit=True, file=sys.stderr)

    try:
        _run()
    except Exception:
        faulthandler.cancel_dump_traceback_later()
        traceback.print_exc()
        os._exit(1)

    faulthandler.cancel_dump_traceback_later()
    os._exit(0)


def _run():
    config = Config()
    client = BattleNetClient(config)
    storage = get_storage(config)

    db_path = config.data_dir / "coinwarden.sqlite"
    storage.download_db(db_path)
    conn = open_db(db_path)

    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()

    seen_connected_realm_ids = set()
    seen_item_ids: set[int] = set()
    for realm_slug in config.realm_slugs:
        connected_realm_id = client.get_connected_realm_id(realm_slug)
        print(f"{realm_slug}: connected realm ID {connected_realm_id}", flush=True)

        if connected_realm_id in seen_connected_realm_ids:
            print("  (already scanned as part of another realm in this cluster)", flush=True)
            continue
        seen_connected_realm_ids.add(connected_realm_id)

        t0 = time.monotonic()
        auctions = client.get_auctions_for_connected_realm(connected_realm_id).get("auctions", [])
        print(f"  fetched {len(auctions)} listings in {time.monotonic() - t0:.2f}s", flush=True)

        aggregates = _aggregate_by_variant(auctions)
        seen_item_ids.update(item_id for item_id, _ in aggregates)
        rows = _rows_from_aggregates(realm_slug, connected_realm_id, aggregates, fetched_at)
        print(f"  {len(aggregates)} item variants across {len(seen_item_ids)} items so far", flush=True)
        insert_item_snapshots(conn, rows)

    sync_item_metadata(client, conn, seen_item_ids)

    bonus_table = fetch_bonus_table()
    if bonus_table:
        replace_bonus_data(conn, serialize(bonus_table))
        print(f"bonus table: {len(bonus_table)} entries refreshed", flush=True)

    # Roll up every completed UTC day still present in the hourly table, then prune.
    today = now.strftime("%Y-%m-%d")
    hourly_cutoff = (now - timedelta(days=config.hourly_retention_days)).isoformat()
    earliest = conn.execute("SELECT MIN(fetched_at) FROM item_price_snapshots").fetchone()[0] or fetched_at
    t0 = time.monotonic()
    rolled = rollup_days(conn, earliest[:10], today)
    print(f"rolled up {rolled} daily rows in {time.monotonic() - t0:.2f}s", flush=True)
    pruned_hourly, pruned_daily = prune(conn, hourly_cutoff,
                                       (now - timedelta(days=config.daily_retention_days)).strftime("%Y-%m-%d"))
    print(f"pruned {pruned_hourly} hourly rows (> {config.hourly_retention_days} d) "
          f"and {pruned_daily} daily rows (> {config.daily_retention_days} d)", flush=True)

    t0 = time.monotonic()
    publish(conn, storage, fetched_at)
    print(f"  publish took {time.monotonic() - t0:.2f}s", flush=True)

    compact(conn)
    conn.close()
    storage.upload_db(db_path)


if __name__ == "__main__":
    main()
