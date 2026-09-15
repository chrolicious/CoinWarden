import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_price_snapshots (
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    min_unit_price INTEGER NOT NULL,
    median_unit_price INTEGER NOT NULL,
    listing_count INTEGER NOT NULL,
    total_quantity INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (connected_realm_id, item_id, variant, fetched_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_item_snapshots_fetched_at
    ON item_price_snapshots (fetched_at);

CREATE INDEX IF NOT EXISTS idx_item_snapshots_item
    ON item_price_snapshots (item_id, variant, fetched_at);

CREATE TABLE IF NOT EXISTS daily_item_prices (
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    low_price INTEGER NOT NULL,
    typical_price INTEGER NOT NULL,
    high_price INTEGER NOT NULL,
    median_price INTEGER NOT NULL,
    avg_listing_count REAL NOT NULL,
    turnover_events INTEGER NOT NULL,
    snapshots INTEGER NOT NULL,
    PRIMARY KEY (connected_realm_id, item_id, variant, day)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_daily_item
    ON daily_item_prices (item_id, variant, day);

CREATE TABLE IF NOT EXISTS active_listings (
    connected_realm_id INTEGER NOT NULL,
    auction_id INTEGER NOT NULL,
    realm_slug TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    unit_price INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    time_left TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (connected_realm_id, auction_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sale_events (
    connected_realm_id INTEGER NOT NULL,
    auction_id INTEGER NOT NULL,
    realm_slug TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    unit_price INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    last_time_left TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    vanished_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (connected_realm_id, auction_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_sale_events_item
    ON sale_events (item_id, variant, vanished_at);

CREATE TABLE IF NOT EXISTS daily_sales (
    realm_slug TEXT NOT NULL,
    connected_realm_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    sold_count INTEGER NOT NULL,
    sold_quantity INTEGER NOT NULL,
    sold_min_price INTEGER,
    sold_median_price INTEGER,
    sold_max_price INTEGER,
    relist_count INTEGER NOT NULL,
    expired_count INTEGER NOT NULL,
    PRIMARY KEY (connected_realm_id, item_id, variant, day)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_daily_sales_item
    ON daily_sales (item_id, variant, day);

CREATE TABLE IF NOT EXISTS featured_items (
    item_id INTEGER NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    last_featured_at TEXT NOT NULL,
    PRIMARY KEY (item_id, variant)
);

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

CREATE TABLE IF NOT EXISTS bonus_data (
    bonus_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version < 2 and _table_exists(conn, "item_price_snapshots") and not _column_exists(conn, "item_price_snapshots", "variant"):
        print("db: migrating snapshots to variant-aware schema", flush=True)
        conn.executescript("""
            ALTER TABLE item_price_snapshots RENAME TO item_price_snapshots_v1;
            DROP INDEX IF EXISTS idx_item_snapshots_fetched_at;
            DROP INDEX IF EXISTS idx_item_snapshots_item;
            DROP TABLE IF EXISTS featured_items;
        """)
        conn.executescript(SCHEMA)
        conn.execute("""
            INSERT INTO item_price_snapshots
                (realm_slug, connected_realm_id, item_id, variant, min_unit_price, median_unit_price,
                 listing_count, total_quantity, fetched_at)
            SELECT realm_slug, connected_realm_id, item_id, '', min_unit_price, median_unit_price,
                   listing_count, total_quantity, fetched_at
            FROM item_price_snapshots_v1
        """)
        conn.execute("DROP TABLE item_price_snapshots_v1")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _migrate(conn)
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
                (realm_slug, connected_realm_id, item_id, variant, min_unit_price, median_unit_price,
                 listing_count, total_quantity, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    print(f"  wrote {len(rows)} rows in {time.monotonic() - t0:.2f}s", flush=True)


# Roll completed UTC days of hourly rows into one row per (realm, item, variant, day).
# typical_price is the median of the day's cheapest listings - the baseline the
# timing score compares against; turnover counts hour-to-hour listing-count drops.
ROLLUP_SQL = """
WITH hourly AS (
    SELECT realm_slug, connected_realm_id, item_id, variant, substr(fetched_at, 1, 10) AS day,
           min_unit_price, median_unit_price, listing_count, fetched_at,
           LAG(listing_count) OVER (PARTITION BY connected_realm_id, item_id, variant ORDER BY fetched_at) AS prev_count
    FROM item_price_snapshots
    WHERE fetched_at >= ? AND fetched_at < ?
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY connected_realm_id, item_id, variant, day ORDER BY min_unit_price) AS rn,
           COUNT(*) OVER (PARTITION BY connected_realm_id, item_id, variant, day) AS n
    FROM hourly
)
INSERT OR REPLACE INTO daily_item_prices
    (realm_slug, connected_realm_id, item_id, variant, day, low_price, typical_price, high_price,
     median_price, avg_listing_count, turnover_events, snapshots)
SELECT realm_slug, connected_realm_id, item_id, variant, day,
       MIN(min_unit_price),
       MAX(CASE WHEN rn = (n + 1) / 2 THEN min_unit_price END),
       MAX(min_unit_price),
       CAST(AVG(median_unit_price) AS INTEGER),
       AVG(listing_count),
       SUM(CASE WHEN prev_count IS NOT NULL AND listing_count < prev_count THEN 1 ELSE 0 END),
       MAX(n)
FROM ranked
GROUP BY connected_realm_id, item_id, variant, day
"""


def rollup_days(conn: sqlite3.Connection, day_from: str, day_to_exclusive: str) -> int:
    with conn:
        cur = conn.execute(ROLLUP_SQL, (day_from, day_to_exclusive))
    return cur.rowcount


# One row per (realm, item, variant, day) from the raw sale events. 'sold'
# events drive count and price stats; relists and expiries are kept as
# counts so a hot-looking item with heavy relisting can be spotted.
SALES_ROLLUP_SQL = """
WITH ev AS (
    SELECT realm_slug, connected_realm_id, item_id, variant, substr(vanished_at, 1, 10) AS day,
           unit_price, quantity, kind
    FROM sale_events
    WHERE vanished_at >= ? AND vanished_at < ?
),
sold AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY connected_realm_id, item_id, variant, day ORDER BY unit_price) AS rn,
              COUNT(*) OVER (PARTITION BY connected_realm_id, item_id, variant, day) AS n
    FROM ev WHERE kind = 'sold'
),
sold_agg AS (
    SELECT connected_realm_id, item_id, variant, day,
           COUNT(*) AS sold_count, SUM(quantity) AS sold_quantity,
           MIN(unit_price) AS sold_min, MAX(CASE WHEN rn = (n + 1) / 2 THEN unit_price END) AS sold_median,
           MAX(unit_price) AS sold_max
    FROM sold GROUP BY connected_realm_id, item_id, variant, day
),
other_agg AS (
    SELECT realm_slug, connected_realm_id, item_id, variant, day,
           SUM(kind = 'relist') AS relists, SUM(kind = 'expired') AS expired
    FROM ev GROUP BY connected_realm_id, item_id, variant, day
)
INSERT OR REPLACE INTO daily_sales
    (realm_slug, connected_realm_id, item_id, variant, day, sold_count, sold_quantity,
     sold_min_price, sold_median_price, sold_max_price, relist_count, expired_count)
SELECT o.realm_slug, o.connected_realm_id, o.item_id, o.variant, o.day,
       COALESCE(s.sold_count, 0), COALESCE(s.sold_quantity, 0), s.sold_min, s.sold_median, s.sold_max,
       o.relists, o.expired
FROM other_agg o
LEFT JOIN sold_agg s ON s.connected_realm_id = o.connected_realm_id AND s.item_id = o.item_id
                     AND s.variant = o.variant AND s.day = o.day
"""


def rollup_sales(conn: sqlite3.Connection, from_iso: str, to_iso_exclusive: str) -> None:
    with conn:
        conn.execute(SALES_ROLLUP_SQL, (from_iso, to_iso_exclusive))


def prune(conn: sqlite3.Connection, hourly_cutoff_iso: str, daily_cutoff_day: str,
          events_cutoff_iso: str) -> tuple[int, int, int]:
    with conn:
        hourly = conn.execute("DELETE FROM item_price_snapshots WHERE fetched_at < ?", (hourly_cutoff_iso,)).rowcount
        daily = conn.execute("DELETE FROM daily_item_prices WHERE day < ?", (daily_cutoff_day,)).rowcount
        conn.execute("DELETE FROM daily_sales WHERE day < ?", (daily_cutoff_day,))
        events = conn.execute("DELETE FROM sale_events WHERE vanished_at < ?", (events_cutoff_iso,)).rowcount
    return hourly, daily, events


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


def replace_bonus_data(conn: sqlite3.Connection, rows: list[tuple[int, str]]) -> None:
    with conn:
        conn.execute("DELETE FROM bonus_data")
        conn.executemany("INSERT INTO bonus_data (bonus_id, data) VALUES (?, ?)", rows)


def compact(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if pages and free / pages > 0.15:
        t0 = time.monotonic()
        conn.execute("VACUUM")
        print(f"db: vacuumed ({free}/{pages} free pages) in {time.monotonic() - t0:.1f}s", flush=True)
