"""Sync the CoinWarden WoW addon's SavedVariables file to the dashboard.

Addons can't make HTTP calls, so this is the other half of the loop: parse
the Lua table the addon wrote on logout, push gold/auctions/events to the
Worker, and remember what's already been sent so re-runs only push what's
new. Run manually for now (`python -m core.addon_sync`); wiring it to a
Windows Scheduled Task is a later step once this is proven reliable.
"""
import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.lua_table import parse as parse_lua

load_dotenv()

DASHBOARD_URL = os.environ.get("COINWARDEN_DASHBOARD_URL", "https://coinwarden.epe-michel.workers.dev").rstrip("/")
SYNC_TOKEN = os.environ.get("COINWARDEN_SYNC_TOKEN")
CURSOR_PATH = Path(os.environ.get("COINWARDEN_DATA_DIR", "data")) / "addon_sync_cursor.json"

DEFAULT_SAVEDVARS_GLOB = str(
    Path("C:/Program Files (x86)/World of Warcraft/_retail_/WTF/Account/*/SavedVariables/CoinWarden.lua")
)


def _find_savedvars() -> Path:
    configured = os.environ.get("COINWARDEN_ADDON_SAVEDVARS")
    if configured:
        return Path(configured)
    matches = glob.glob(DEFAULT_SAVEDVARS_GLOB)
    if not matches:
        raise FileNotFoundError(
            f"No CoinWarden.lua found under the default WoW install path. "
            f"Set COINWARDEN_ADDON_SAVEDVARS in .env to the exact file path."
        )
    return Path(matches[0])


def _parse_savedvars(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    marker = "CoinWardenDB = "
    if marker not in text:
        raise ValueError(f"{path} doesn't contain a CoinWardenDB table - is the addon loaded and logged out at least once?")
    text = text.split(marker, 1)[1].rstrip().rstrip(";")
    return parse_lua(text)


def _epoch_to_iso(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _load_cursors() -> dict:
    if CURSOR_PATH.exists():
        return json.loads(CURSOR_PATH.read_text())
    return {}


def _save_cursors(cursors: dict) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(cursors, indent=2))


def _headers() -> dict:
    return {"x-ledger-token": SYNC_TOKEN, "content-type": "application/json"}


def push_state(character: str, char_data: dict) -> None:
    auctions = char_data.get("auctions") or {}
    body = {
        "character": character,
        "realm": char_data.get("realm"),
        "class": char_data.get("class"),
        "gold": char_data.get("gold"),
        "gold_updated_at": _epoch_to_iso(char_data.get("gold_updated_at")),
        "auctions": auctions,
        "auctions_updated_at": _epoch_to_iso(char_data.get("auctions_updated_at")),
    }
    resp = requests.put(f"{DASHBOARD_URL}/api/game/state", headers=_headers(), json=body, timeout=30)
    resp.raise_for_status()
    print(f"  state: gold={body['gold']}, {len(auctions)} owned auctions")


def push_events(character: str, events: list[dict], since_ts: int) -> int:
    new_events = [e for e in events if e.get("ts", 0) > since_ts]
    if not new_events:
        print(f"  events: none new (cursor at {since_ts})")
        return since_ts
    payload = [
        {
            "character": character,
            "ts": e["ts"],
            "type": e["type"],
            "item_name": e.get("item_name"),
            "counterparty": e.get("counterparty") or e.get("seller") or e.get("buyer"),
            "price_copper": e.get("price_copper") or e.get("total_bid"),
            "quantity": e.get("quantity"),
            "ah_cut": e.get("ah_cut"),
            "deposit": e.get("deposit"),
        }
        for e in new_events
    ]
    resp = requests.post(f"{DASHBOARD_URL}/api/game/events", headers=_headers(), json={"events": payload}, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    print(f"  events: sent {len(payload)}, server added {result.get('added')} new (total {result.get('total')})")
    return max(e["ts"] for e in new_events)


def main() -> None:
    if not SYNC_TOKEN:
        raise SystemExit("COINWARDEN_SYNC_TOKEN is not set in .env - same value as the Worker's LEDGER_WRITE_TOKEN.")

    savedvars_path = _find_savedvars()
    print(f"reading {savedvars_path}")
    data = _parse_savedvars(savedvars_path)
    characters = data.get("characters", {})
    cursors = _load_cursors()

    for character, char_data in characters.items():
        print(f"{character}:")
        push_state(character, char_data)
        events = char_data.get("events") or []
        since_ts = cursors.get(character, 0)
        cursors[character] = push_events(character, events, since_ts)

    _save_cursors(cursors)
    print("done")


if __name__ == "__main__":
    main()
