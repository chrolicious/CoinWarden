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

Hourly pipeline live (GitHub Actions cron → Turso): per-item price aggregates for 5 EU realms plus item metadata.

`python -m core.scoring --help` — cross-realm spread opportunities from the latest snapshot. All thresholds are query-time parameters; raw snapshots are never filtered at ingest.

Known limitation: without turnover history, the sell side cannot distinguish a real market price from an aspirational listing that never sells. A turnover signal (cheapest listing disappearing before it could expire) is planned once ~a week of snapshots exists.

Next: timing-flip score, dashboard (GitHub Pages), ledger.
