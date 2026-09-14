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


def insert_item_snapshots(client: libsql_client.Client, rows: list[tuple]) -> None:
    if not rows:
        return

    statement = """
        INSERT INTO item_price_snapshots
            (realm_slug, connected_realm_id, item_id, min_unit_price, median_unit_price, listing_count, total_quantity, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    batch = [libsql_client.Statement(statement, row) for row in rows]

    for i in range(0, len(batch), 500):
        client.batch(batch[i : i + 500])
