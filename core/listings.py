"""Listing-level diffing: turn consecutive hourly snapshots into sale events.

A listing that disappears while it still had at least two hours left cannot
have expired, so it was bought - or cancelled. If a cheaper listing of the same
variant showed up on the same realm in the same hour, the seller most likely
cancelled to undercut, and the event is recorded as a relist instead of a sale.
Vanishes from the SHORT/MEDIUM buckets are recorded as expiries; they may hide
real sales, but counting them would inflate velocity for dead items.
"""
import sqlite3
import time

# Listings below this are junk for a gold-making tool and dominate the counts.
MIN_TRACKED_UNIT_PRICE = 100 * 10_000

SAFE_BUCKETS = ("LONG", "VERY_LONG")


def _listing_rows(realm_slug: str, connected_realm_id: int, auctions: list[dict], variant_of, fetched_at: str) -> dict[int, tuple]:
    rows = {}
    for a in auctions:
        unit_price = a.get("unit_price")
        if unit_price is None and a.get("buyout") is not None:
            unit_price = a["buyout"] / (a.get("quantity", 1) or 1)
        if unit_price is None or unit_price < MIN_TRACKED_UNIT_PRICE:
            continue
        item = a["item"]
        rows[a["id"]] = (connected_realm_id, a["id"], realm_slug, item["id"], variant_of(item),
                         round(unit_price), a.get("quantity", 1) or 1, a.get("time_left", "UNKNOWN"), fetched_at, fetched_at)
    return rows


def diff_and_store(conn: sqlite3.Connection, realm_slug: str, connected_realm_id: int, auctions: list[dict],
                   variant_of, fetched_at: str) -> dict:
    t0 = time.monotonic()
    current = _listing_rows(realm_slug, connected_realm_id, auctions, variant_of, fetched_at)

    previous = {
        row[0]: row for row in conn.execute(
            "SELECT auction_id, item_id, variant, unit_price, quantity, time_left, first_seen "
            "FROM active_listings WHERE connected_realm_id = ?", (connected_realm_id,))
    }
    first_run = not previous

    # New listings this hour, grouped by variant, for the relist heuristic.
    new_by_variant: dict[tuple[int, str], list[int]] = {}
    for auction_id, row in current.items():
        if auction_id not in previous:
            new_by_variant.setdefault((row[3], row[4]), []).append(row[5])

    events = []
    for auction_id, (_, item_id, variant, unit_price, quantity, time_left, first_seen) in previous.items():
        if auction_id in current:
            continue
        if time_left in SAFE_BUCKETS:
            undercut = any(p < unit_price for p in new_by_variant.get((item_id, variant), ()))
            kind = "relist" if undercut else "sold"
        else:
            kind = "expired"
        events.append((connected_realm_id, auction_id, realm_slug, item_id, variant, unit_price, quantity,
                       time_left, first_seen, fetched_at, kind))

    with conn:
        if events:
            conn.executemany(
                "INSERT OR REPLACE INTO sale_events (connected_realm_id, auction_id, realm_slug, item_id, variant, "
                "unit_price, quantity, last_time_left, first_seen, vanished_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                events)
        conn.execute("DELETE FROM active_listings WHERE connected_realm_id = ?", (connected_realm_id,))
        conn.executemany(
            "INSERT INTO active_listings (connected_realm_id, auction_id, realm_slug, item_id, variant, unit_price, "
            "quantity, time_left, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [row[:8] + (previous[aid][6] if aid in previous else fetched_at, fetched_at) for aid, row in current.items()])

    counts = {"tracked": len(current), "sold": 0, "relist": 0, "expired": 0}
    for e in events:
        counts[e[-1]] += 1
    counts["seconds"] = round(time.monotonic() - t0, 2)
    counts["first_run"] = first_run
    return counts
