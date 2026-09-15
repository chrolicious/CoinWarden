"""Variant keys for auction listings.

A non-commodity listing is an item ID plus bonus lists (item-level track,
difficulty, suffix, socket, tertiary...) and modifiers (chosen secondary stats,
drop level). Two listings with the same item ID but different bonuses can differ
in price by an order of magnitude, so aggregates are keyed by item ID + variant.
"""
import json

import requests

# Modifier types that change what the item is (and therefore its price).
# 9 = drop level (level-scaled gear), 29/30 = the two secondary stats on
# random-stat gear. Other types are cosmetic or bookkeeping.
PRICE_RELEVANT_MODIFIERS = (9, 29, 30)

RAIDBOTS_BONUSES_URL = "https://www.raidbots.com/static/data/live/bonuses.json"


def variant_key(item: dict) -> str:
    bonus = sorted(item.get("bonus_lists") or [])
    mods = sorted(
        (m["type"], m["value"]) for m in item.get("modifiers") or []
        if m.get("type") in PRICE_RELEVANT_MODIFIERS
    )
    if not bonus and not mods:
        return ""
    parts = [":".join(str(b) for b in bonus)]
    parts += [f"{t}={v}" for t, v in mods]
    return "|".join(parts)


def bonus_ids(variant: str) -> list[int]:
    if not variant:
        return []
    head = variant.split("|", 1)[0]
    return [int(b) for b in head.split(":") if b]


# Only the fields the frontend uses to label a variant.
_KEEP = ("name", "stats", "tag", "socket", "level", "itemLevel", "levelOffset", "quality")


def fetch_bonus_table(timeout: int = 30) -> dict[int, dict] | None:
    try:
        resp = requests.get(RAIDBOTS_BONUSES_URL, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"bonus table: fetch failed ({e}), keeping cached copy", flush=True)
        return None
    table = {}
    for key, entry in resp.json().items():
        slim = {k: entry[k] for k in _KEEP if k in entry}
        if slim:
            table[int(key)] = slim
    return table


def serialize(table: dict[int, dict]) -> list[tuple[int, str]]:
    return [(bonus_id, json.dumps(data, separators=(",", ":"))) for bonus_id, data in table.items()]
