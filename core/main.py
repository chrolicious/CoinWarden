from datetime import datetime, timezone

from core.config import Config
from core.api_client import BattleNetClient
from core.db import get_client, ensure_schema, insert_snapshots


def _rows_from_auctions(realm_slug: str, connected_realm_id: int, auctions: list[dict], fetched_at: str) -> list[tuple]:
    rows = []
    for auction in auctions:
        rows.append((
            realm_slug,
            connected_realm_id,
            auction["item"]["id"],
            auction["id"],
            auction.get("buyout"),
            auction.get("unit_price"),
            auction.get("quantity", 1),
            auction.get("time_left", "UNKNOWN"),
            fetched_at,
        ))
    return rows


def main():
    config = Config()
    client = BattleNetClient(config)
    db = get_client(config)
    ensure_schema(db)

    fetched_at = datetime.now(timezone.utc).isoformat()

    seen_connected_realm_ids = set()
    for realm_slug in config.realm_slugs:
        connected_realm_id = client.get_connected_realm_id(realm_slug)
        print(f"{realm_slug}: connected realm ID {connected_realm_id}")

        if connected_realm_id in seen_connected_realm_ids:
            print("  (already scanned as part of another realm in this cluster)")
            continue
        seen_connected_realm_ids.add(connected_realm_id)

        auctions = client.get_auctions_for_connected_realm(connected_realm_id).get("auctions", [])
        rows = _rows_from_auctions(realm_slug, connected_realm_id, auctions, fetched_at)
        insert_snapshots(db, rows)
        print(f"  wrote {len(rows)} listings to DB")

    db.close()


if __name__ == "__main__":
    main()
