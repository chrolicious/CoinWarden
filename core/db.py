import libsql_client

from core.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    auction_id INTEGER NOT NULL,
    buyout INTEGER,
    unit_price INTEGER,
    quantity INTEGER NOT NULL,
    time_left TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_item_realm_time
    ON auction_snapshots (connected_realm_id, item_id, fetched_at);

CREATE INDEX IF NOT EXISTS idx_snapshots_auction
    ON auction_snapshots (connected_realm_id, auction_id, fetched_at);
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


def insert_snapshots(client: libsql_client.Client, rows: list[tuple]) -> None:
    if not rows:
        return

    statement = """
        INSERT INTO auction_snapshots
            (realm_slug, connected_realm_id, item_id, auction_id, buyout, unit_price, quantity, time_left, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    batch = [libsql_client.Statement(statement, row) for row in rows]

    for i in range(0, len(batch), 500):
        client.batch(batch[i : i + 500])
