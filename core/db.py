import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_price_snapshots (
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    min_unit_price INTEGER NOT NULL,
    median_unit_price INTEGER NOT NULL,
    listing_count INTEGER NOT NULL,
    total_quantity INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (connected_realm_id, item_id, fetched_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_item_snapshots_fetched_at
    ON item_price_snapshots (fetched_at);

CREATE INDEX IF NOT EXISTS idx_item_snapshots_item
    ON item_price_snapshots (item_id, fetched_at);

CREATE TABLE IF NOT EXISTS items (
    item_id INTEGER PRIMARY KEY,
    name TEXT,
    quality TEXT,
    item_class TEXT,
    item_subclass TEXT,
    inventory_type TEXT,
    item_level INTEGER,
    vendor_sell_price INTEGER,
    icon_url TEXT,
    fetched_at TEXT NOT NULL
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def insert_item_snapshots(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    t0 = time.monotonic()
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO item_price_snapshots
                (realm_slug, connected_realm_id, item_id, min_unit_price, median_unit_price,
                 listing_count, total_quantity, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    print(f"  wrote {len(rows)} rows in {time.monotonic() - t0:.2f}s", flush=True)


def prune_snapshots(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    with conn:
        cur = conn.execute("DELETE FROM item_price_snapshots WHERE fetched_at < ?", (cutoff_iso,))
    return cur.rowcount


def get_known_item_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT item_id FROM items")}


def upsert_items(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn:
        conn.executemany(
            """
            INSERT INTO items
                (item_id, name, quality, item_class, item_subclass, inventory_type,
                 item_level, vendor_sell_price, icon_url, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                name = excluded.name,
                quality = excluded.quality,
                item_class = excluded.item_class,
                item_subclass = excluded.item_subclass,
                inventory_type = excluded.inventory_type,
                item_level = excluded.item_level,
                vendor_sell_price = excluded.vendor_sell_price,
                icon_url = excluded.icon_url,
                fetched_at = excluded.fetched_at
            """,
            rows,
        )


def compact(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
