import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core.api_client import BattleNetClient
from core.db import get_known_item_ids, upsert_items

# Blizzard allows 36k requests/hour; each item costs 2 calls. Capped so the
# initial ~30k-item backfill spreads across hourly runs instead of one burst.
MAX_NEW_ITEMS_PER_RUN = 1500
CONCURRENCY = 16


def _fetch_one(client: BattleNetClient, item_id: int, fetched_at: str) -> tuple:
    item = client.get_item(item_id)
    if item is None:
        return (item_id, None, None, None, None, None, None, None, None, fetched_at)

    icon_url = client.get_item_icon_url(item_id)
    return (
        item_id,
        item.get("name"),
        (item.get("quality") or {}).get("type"),
        (item.get("item_class") or {}).get("name"),
        (item.get("item_subclass") or {}).get("name"),
        (item.get("inventory_type") or {}).get("type"),
        item.get("level"),
        item.get("sell_price"),
        icon_url,
        fetched_at,
    )


def sync_item_metadata(client: BattleNetClient, db, seen_item_ids: set[int]) -> None:
    known = get_known_item_ids(db)
    missing = sorted(seen_item_ids - known)
    print(f"items: {len(known)} known, {len(missing)} missing", flush=True)
    if not missing:
        return

    to_fetch = missing[:MAX_NEW_ITEMS_PER_RUN]
    fetched_at = datetime.now(timezone.utc).isoformat()

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        rows = list(pool.map(lambda item_id: _fetch_one(client, item_id, fetched_at), to_fetch))
    print(f"  fetched {len(rows)} item records in {time.monotonic() - t0:.2f}s", flush=True)

    upsert_items(db, rows)
    print(f"  wrote {len(rows)} items ({len(missing) - len(to_fetch)} still pending for next run)", flush=True)
