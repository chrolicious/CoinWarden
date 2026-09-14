# CoinWarden

Companion app + WoW addon for scanning connected-realm (non-commodity) and region-wide (commodity) auction house data, tracking price history, and surfacing buy/sell recommendations. No auto-buy/auto-sell — all execution stays manual in-game.

## Structure

- `core/` — Python companion app (data collection, analysis)
- `addon/` — WoW addon (Lua) that reads companion app output and overlays recommendations in the AH UI. Not built yet.

## Setup

1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, fill in:
   - `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET` from https://develop.battle.net/ (create a Client, client-credentials flow)
   - `BNET_REGION` (e.g. `eu`, `us`)
   - `WOW_REALM_SLUG` (lowercase, hyphenated realm name, e.g. `silvermoon`)
4. `python -m core.main` — sanity check: fetches your connected realm ID and current non-commodity auction count.

## Status

Scaffold only. Next: persist snapshots to DB, compute historical medians, flag underpriced listings.
