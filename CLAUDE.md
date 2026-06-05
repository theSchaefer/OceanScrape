# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MarineScraper** — Scrapes MarineTraffic.com using anti-detection browser automation across 79 ocean regions, counts ships via OpenCV inline, and stores results in PostgreSQL. Covers major shipping chokepoints and trade corridors across 5 zoom tiers (z9–z13), spanning ~35% of ocean surface.

## Key Commands

```bash
# Set up environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the full pipeline (scrape → raw JSONL → ingest → database)
python run.py

# Run the scraper directly (raw-only by default — no DB write)
python scraper_global.py                   # All 57 regions
python scraper_global.py --ingest          # Capture then ingest into PostgreSQL

# Ingest a raw run into PostgreSQL (separate step)
python update_database.py data/raw/runs/<run_id>/captures.jsonl
python update_database.py                   # Legacy: ingest+archive data/captures_log.jsonl
python scraper_global.py --save-images     # Also save tile images to disk
python scraper_global.py --regions N,S,P   # Specific regions only (single-letter codes)
python scraper_global.py --zoom=9          # Only zoom-level 9 regions
python scraper_global.py --tier=1          # Tier 1 expansion regions only
python scraper_global.py --tier=original   # Original 34 chokepoint regions only
python scraper_global.py --tier=1,2        # Tiers 1+2 combined
python scraper_global.py --list-regions    # Print all region codes and names

# Diagnostics
python discover_map.py                     # One-shot JS introspection → map_discovery.json
python seer.py path/to/image.jpg           # Run OpenCV detection on a file (CLI mode)
```

## Architecture

```
capture            →  raw JSONL                              →  ingest              →  postgres     →  api/dashboard
scraper_global.py  →  data/raw/runs/<run_id>/captures.jsonl   →  update_database.py  →  PostgreSQL   →  api.py + dashboard/
  browser workers      one file per run (no auto-ingest)         separate CLI step      psycopg2
```

Capture and ingest are **decoupled**. The scraper writes only raw data by default; a separate `update_database.py <path>` step loads a run into PostgreSQL. `run.py` chains both for a full pipeline pass.

- **`scraper_global.py`** — Main scraper. Launches Patchright (undetected Playwright fork) with `MAX_BROWSERS` concurrent browser workers. Each run writes its captures to `data/raw/runs/<run_id>/captures.jsonl` and updates the `data/raw/runs/LATEST` pointer; it does **not** ingest into PostgreSQL unless `--ingest` is passed. Calls `count_ships_from_bytes()` and `extract_marker_coords()` inline — no images written to disk by default. Marker pixel positions are converted to lat/lon via Web Mercator projection.
- **`seer.py`** — OpenCV ship counter. HSV color masking → contour detection → shape classification: triangles (3 vertices via `approxPolyDP`) = moving ships; circles (high circularity ratio) = stationary ships. Red = tankers, green = cargo. Exports `count_ships_from_bytes()` for in-memory use and `extract_marker_coords()` for lat/lon position extraction.
- **`grid.py`** — Web Mercator projection utilities. `get_tile_centers()` computes non-overlapping tile grids covering a polygon's bounding box. `generate_ocean_grid()` auto-tiles large bounding boxes. Snake/boustrophedon tile ordering.
- **`geo_profile.py`** — Resolves proxy IP geolocation via ip-api.com. Maps country codes to locale/Accept-Language/timezone for browser fingerprinting. Requires `DECODO_USERNAME`/`DECODO_PASSWORD` in `.env`.
- **`run.py`** — Orchestrator. Calls `scraper_global.py` as a subprocess, then ingests the just-written run via `update_database.ingest_file()` (located through the `data/raw/runs/LATEST` pointer).
- **`update_database.py`** — PostgreSQL database layer. `ingest_file(path)` reads a single run's `captures.jsonl` and batch-inserts it; it does **not** move or reset the raw file, and writes an `<name>.ingested.json` status marker beside it on success. `process_log()` is the legacy entry point for the shared `data/captures_log.jsonl` (archive + reset; falls back to `captures_log.json`). CLI: `python update_database.py [ingest] <path> [...]`. Both paths share the same insert logic (`_insert_entries`) — capture records go into `captures`/`tile_captures`, markers into `vessel_positions`/`global_vessel_positions`. Auto-creates schema on first run. Idempotent via `ON CONFLICT`, so re-ingesting a file is safe.
- **`discover_map.py`** — Dev/debug tool. Injects constructor hooks before page load to introspect how MarineTraffic stores its Leaflet map instance. Outputs to `map_discovery.json`.

## Region Codes

Short keys passed to `--regions`. Examples: `N`/`S` = Suez North/South, `P` = Panama, `M` = Malacca, `H` = Hormuz. Run `--list-regions` for the full list (shows code, zoom, tier, name). Tiers: `original` (34 chokepoints), `1` (major trade arteries), `2` (regionally critical), `3` (coverage fill). All polygon boundaries are overridable via env vars (e.g. `NORTH_POLYGON="lat,lon;lat,lon;..."`). Filters can be combined: `--tier=1 --zoom=9` selects only tier-1 regions at zoom 9.

## Configuration (`.env`)

```
DECODO_USERNAME / DECODO_PASSWORD   # Proxy credentials (decodo.com, ports 10011–10025)
VIEWPORT_WIDTH / VIEWPORT_HEIGHT    # Default 7680×4320 (8K — maximizes area per tile)
MAX_BROWSERS                        # Default 2 (concurrent browser workers; scale up to RAM limit)
SAVE_IMAGES                         # Default 0; set to 1 or use --save-images flag
SCREENSHOT_FORMAT / QUALITY         # Default jpeg / 85
SCRAPE_INTERVAL_MINUTES             # Default 60
JITTER_SECONDS                      # Default 300
DATABASE_URL                        # PostgreSQL connection string (e.g. postgresql://user:pass@localhost:5432/marinescraper)
```

## Anti-Detection

Patchright with `channel="chrome"` + `--headless=new`. Proxy rotation with geo-profile spoofing (timezone, locale, geolocation derived from proxy IP). UA rotation. Single `page.goto()` per browser worker + `setView()` panning for all subsequent regions avoids repeated Cloudflare challenges. Each browser worker processes many regions sequentially via work-stealing, reusing one Cloudflare pass.

## Backend / API

- The dashboard backend lives in `api.py` (FastAPI), served behind nginx in production; the frontend is `dashboard/index.html`.
- When adding or changing an endpoint, **edit `api.py` directly** — never deliver a route as a chat snippet only. A feature is not done until the backend route exists in the file *and* the frontend (`dashboard/index.html`) is wired to call it.
- Verify the full loop before declaring done: start the server, hit each new route (curl / browser), and confirm the frontend receives valid data — no 404 (missing/unmounted route) and no 500.

## Scraper Architecture

- MarineTraffic's Leaflet map object is **not exposed on `window`** — it's bundled inside a closure. Do not rely on direct JS injection of the map (e.g. `window.L` / a global map handle).
- Use **network / tile-readiness waits** and **multi-strategy hooks** (constructor hooks injected before page load, as in `discover_map.py`) to detect map state, rather than assuming a global is reachable.
- Marker pixel → lat/lon conversion is sensitive to projection, device-pixel-ratio (DPR), and zoom. When markers are misplaced, check that math first instead of patching coordinates.

## Error Handling

- Prefer **hard failures over silent soft-fallback captures**. If a run precondition (e.g. `center_offset`) is not met, abort the capture rather than recording it — bad data in the DB is worse than a missing row.

## Database / SQL

- In any `SELECT DISTINCT` query, **every `ORDER BY` expression must also appear in the SELECT list**, or Postgres raises an error (this caused recurring 500s during timeline scrubbing).

## Notes

- `data/` directory is gitignored; raw runs live under `data/raw/runs/<run_id>/captures.jsonl` (plus an `ingested.json` marker once loaded). The legacy shared log `data/captures_log.jsonl` is still read by `process_log()` for back-compat. The database is external (PostgreSQL).
- At 8K viewport, most regions fit in a single tile across 57 regions.

## Learning & Discovery

- When you use a library, framework, technique, or import that I may not be familiar with, briefly introduce it before using it: what it is, why it's useful here, and how it works.
- Break down unfamiliar concepts step by step — from the high-level idea down to the specific usage in context.
- If there are common alternatives, mention them and explain why you chose this one.
- Keep explanations concise but complete enough to understand without external lookup.
