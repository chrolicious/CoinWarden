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
from core.db import open_db, insert_item_snapshots, prune_snapshots, compact
from core.items import sync_item_metadata
from core.scoring import publish
from core.storage import get_storage


def _unit_price(auction: dict) -> float | None:
    if auction.get("unit_price") is not None:
        return auction["unit_price"]
    if auction.get("buyout") is not None:
        quantity = auction.get("quantity", 1) or 1
        return auction["buyout"] / quantity
    return None


def _aggregate_by_item(auctions: list[dict]) -> dict[int, dict]:
    prices_by_item: dict[int, list[float]] = defaultdict(list)
    quantity_by_item: dict[int, int] = defaultdict(int)

    for auction in auctions:
        price = _unit_price(auction)
        if price is None:
            continue
        item_id = auction["item"]["id"]
        prices_by_item[item_id].append(price)
        quantity_by_item[item_id] += auction.get("quantity", 1) or 1

    aggregates = {}
    for item_id, prices in prices_by_item.items():
        aggregates[item_id] = {
            "min_unit_price": round(min(prices)),
            "median_unit_price": round(statistics.median(prices)),
            "listing_count": len(prices),
            "total_quantity": quantity_by_item[item_id],
        }
    return aggregates


def _rows_from_aggregates(realm_slug: str, connected_realm_id: int, aggregates: dict[int, dict], fetched_at: str) -> list[tuple]:
    return [
        (
            realm_slug,
            connected_realm_id,
            item_id,
            agg["min_unit_price"],
            agg["median_unit_price"],
            agg["listing_count"],
            agg["total_quantity"],
            fetched_at,
        )
        for item_id, agg in aggregates.items()
    ]


def main():
    faulthandler.dump_traceback_later(900, exit=True, file=sys.stderr)

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

    fetched_at = datetime.now(timezone.utc).isoformat()

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

        aggregates = _aggregate_by_item(auctions)
        seen_item_ids.update(aggregates.keys())
        rows = _rows_from_aggregates(realm_slug, connected_realm_id, aggregates, fetched_at)
        insert_item_snapshots(conn, rows)

    sync_item_metadata(client, conn, seen_item_ids)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.retention_days)).isoformat()
    pruned = prune_snapshots(conn, cutoff)
    print(f"pruned {pruned} rows older than {config.retention_days} days", flush=True)

    t0 = time.monotonic()
    publish(conn, storage, fetched_at)
    print(f"  publish took {time.monotonic() - t0:.2f}s", flush=True)

    compact(conn)
    conn.close()
    storage.upload_db(db_path)


if __name__ == "__main__":
    main()
