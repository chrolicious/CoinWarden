import time

import libsql_client

from core.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    min_unit_price INTEGER NOT NULL,
    median_unit_price INTEGER NOT NULL,
    listing_count INTEGER NOT NULL,
    total_quantity INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_item_snapshots_lookup
    ON item_price_snapshots (connected_realm_id, item_id, fetched_at);

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

DROP TABLE IF EXISTS auction_snapshots;
"""


def get_client(config: Config) -> libsql_client.Client:
    url = config.turso_database_url.replace("libsql://", "https://", 1)
    return libsql_client.create_client_sync(
        url=url,
        auth_token=config.turso_auth_token,
    )


def ensure_schema(client: libsql_client.Client) -> None:
    for statement in SCHEMA.strip().split(";"):
        statement = statement.strip()
        if statement:
            client.execute(statement)


# Runner-to-Turso latency dominates write time, so minimise HTTP round trips:
# many rows per INSERT statement, many statements per batch request.
ROWS_PER_STATEMENT = 250
STATEMENTS_PER_BATCH = 20

SNAPSHOT_COLUMNS = 8


def _multi_row_insert(rows: list[tuple]) -> libsql_client.Statement:
    placeholders = ", ".join(["(" + ", ".join(["?"] * SNAPSHOT_COLUMNS) + ")"] * len(rows))
    sql = (
        "INSERT INTO item_price_snapshots "
        "(realm_slug, connected_realm_id, item_id, min_unit_price, median_unit_price, "
        "listing_count, total_quantity, fetched_at) VALUES " + placeholders
    )
    args = [value for row in rows for value in row]
    return libsql_client.Statement(sql, args)


def insert_item_snapshots(client: libsql_client.Client, rows: list[tuple]) -> None:
    if not rows:
        return

    statements = [
        _multi_row_insert(rows[i : i + ROWS_PER_STATEMENT])
        for i in range(0, len(rows), ROWS_PER_STATEMENT)
    ]

    total_batches = (len(statements) + STATEMENTS_PER_BATCH - 1) // STATEMENTS_PER_BATCH
    for batch_num, i in enumerate(range(0, len(statements), STATEMENTS_PER_BATCH), start=1):
        t0 = time.monotonic()
        client.batch(statements[i : i + STATEMENTS_PER_BATCH])
        print(f"    batch {batch_num}/{total_batches} in {time.monotonic() - t0:.2f}s", flush=True)


def get_known_item_ids(client: libsql_client.Client) -> set[int]:
    result = client.execute("SELECT item_id FROM items")
    return {row[0] for row in result.rows}


def upsert_items(client: libsql_client.Client, rows: list[tuple]) -> None:
    if not rows:
        return

    statement = """
        INSERT INTO items
            (item_id, name, quality, item_class, item_subclass, inventory_type, item_level, vendor_sell_price, icon_url, fetched_at)
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
    """
    batch = [libsql_client.Statement(statement, row) for row in rows]
    for i in range(0, len(batch), 500):
        client.batch(batch[i : i + 500])
