# CoinWarden

Companion app + WoW addon for scanning connected-realm (non-commodity) and region-wide (commodity) auction house data, tracking price history, and surfacing buy/sell recommendations. No auto-buy/auto-sell — all execution stays manual in-game.

## Structure

- `core/` — Python companion app (data collection, analysis)
- `addon/` — WoW addon (Lua) that reads companion app output and overlays recommendations in the AH UI. Not built yet.

## How it runs

Hourly GitHub Actions job (`.github/workflows/scan.yml`):

1. Download the SQLite database (gzipped) from Cloudflare R2.
2. Fetch non-commodity auctions for each configured realm from the Battle.net API and store one aggregate row per (realm, item): min and median unit price, listing count, total quantity.
3. Fetch metadata (name, quality, class, icon) for item IDs not seen before, up to 1,500 per run.
4. Prune snapshots older than the retention window (14 days).
5. Run the scoring queries and publish static JSON to R2 for the frontend (`latest/spreads.json`, `latest/timing.json`, `items.json`, `history/<item_id>.json`).
6. Upload the database back to R2.

Nothing runs on a personal machine and the frontend needs no database credentials.

## Setup

1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, fill in:
   - `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET` from https://develop.battle.net/ (create a Client, client-credentials flow)
   - `BNET_REGION` (e.g. `eu`, `us`)
   - `WOW_REALM_SLUGS` (comma-separated, lowercase, hyphenated, e.g. `silvermoon,tarren-mill`)
   - `R2_*` optional; without them the pipeline runs in local mode (database in `data/`, published JSON in `data/public/`).
4. `python -m core.main` — one full pipeline run.
5. `python -m core.scoring spreads` / `python -m core.scoring timing` — run the scoring queries against the local database with your own thresholds.

## Scoring

- **Cross-realm spread**: buy at the cheapest listing on the cheapest realm, sell by undercutting the cheapest listing on the best other realm, net of the 5% AH cut. Items move between realms via the Warband bank.
- **Timing flip**: current cheapest listing vs the item's own trailing-window p25/p50/p75 on the same realm, with hour-to-hour listing-count drops as a liquidity gate.

All thresholds are query-time parameters; raw snapshots are never filtered at ingest. Published JSON uses permissive thresholds so the frontend can filter client-side.

Known limitation: snapshot data cannot distinguish a real market price from an aspirational listing that never sells. A turnover signal is planned once enough history exists.

## Next

Dashboard (Cloudflare Pages), ledger, holdings/P&L, goal tracker.
