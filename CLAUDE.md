# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MarineScraper** — Scrapes MarineTraffic.com using anti-detection browser automation across 34 ocean regions, counts ships via OpenCV inline, and stores results in PostgreSQL. Covers major shipping chokepoints across 5 zoom tiers (z9–z13).

## Key Commands

```bash
# Set up environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the full pipeline (scrape → inline detection → database)
python run.py

# Run the scraper directly
python scraper_global.py                   # All 34 regions
python scraper_global.py --save-images     # Also save tile images to disk
python scraper_global.py --regions N,S,P   # Specific regions only (single-letter codes)
python scraper_global.py --list-regions    # Print all region codes and names

# Diagnostics
python discover_map.py                     # One-shot JS introspection → map_discovery.json
python seer.py path/to/image.jpg           # Run OpenCV detection on a file (CLI mode)
```

## Architecture

```
scraper_global.py  →  seer.py (inline)  →  captures_log.json  →  update_database.py  →  PostgreSQL
  browser + tabs      in-memory OpenCV      counts + metadata       psycopg2 batch insert
```

- **`scraper_global.py`** — Main scraper. Launches Patchright (undetected Playwright fork) browsers with tab parallelism (`TABS_PER_BROWSER` × `MAX_BROWSERS`). Navigates MarineTraffic once per browser, then pans via Leaflet `setView()` for subsequent tiles. Calls `count_ships_from_bytes()` inline — no images written to disk by default. Also intercepts `get_data_json_4` API responses via Playwright's `page.on("response")` to capture per-vessel data (MMSI, name, flag, destination, position).
- **`seer.py`** — OpenCV ship counter. HSV color masking → contour detection → shape classification: triangles (3 vertices via `approxPolyDP`) = moving ships; circles (high circularity ratio) = stationary ships. Red = tankers, green = cargo. Exports `count_ships_from_bytes()` for in-memory use and `extract_marker_coords()` for lat/lon position extraction.
- **`grid.py`** — Web Mercator projection utilities. `get_tile_centers()` computes non-overlapping tile grids covering a polygon's bounding box. `generate_ocean_grid()` auto-tiles large bounding boxes. Snake/boustrophedon tile ordering.
- **`geo_profile.py`** — Resolves proxy IP geolocation via ip-api.com. Maps country codes to locale/Accept-Language/timezone for browser fingerprinting. Requires `DECODO_USERNAME`/`DECODO_PASSWORD` in `.env`.
- **`run.py`** — Orchestrator. Calls `scraper_global.py` as subprocess, then `update_database.process_log()`.
- **`update_database.py`** — PostgreSQL database layer. Reads `captures_log.json`, batch-inserts capture records into a `captures` table and per-vessel sightings into `vessel_positions` using psycopg2. Ship counts, tile stats, and marker coordinates are stored per capture with JSONB for nested data. Vessel identity (MMSI, name, flag, destination) is stored relationally. Auto-creates schema on first run. Idempotent via `ON CONFLICT`.
- **`discover_map.py`** — Dev/debug tool. Injects constructor hooks before page load to introspect how MarineTraffic stores its Leaflet map instance. Outputs to `map_discovery.json`.

## Region Codes

Single-letter keys passed to `--regions`. Examples: `N`/`S` = Suez North/South, `P` = Panama, `M` = Malacca, `H` = Hormuz. Run `--list-regions` for the full list. All polygon boundaries are overridable via env vars (e.g. `NORTH_POLYGON="lat,lon;lat,lon;..."`).

## Configuration (`.env`)

```
DECODO_USERNAME / DECODO_PASSWORD   # Proxy credentials (decodo.com, ports 10011–10025)
VIEWPORT_WIDTH / VIEWPORT_HEIGHT    # Default 7680×4320 (8K — maximizes area per tile)
TABS_PER_BROWSER                    # Default 4
MAX_BROWSERS                        # Default 4 (16 regions load concurrently)
SAVE_IMAGES                         # Default 0; set to 1 or use --save-images flag
SCREENSHOT_FORMAT / QUALITY         # Default jpeg / 85
SCRAPE_INTERVAL_MINUTES             # Default 60
JITTER_SECONDS                      # Default 300
DATABASE_URL                        # PostgreSQL connection string (e.g. postgresql://user:pass@localhost:5432/marinescraper)
```

## Anti-Detection

Patchright with `channel="chrome"` + `--headless=new`. Proxy rotation with geo-profile spoofing (timezone, locale, geolocation derived from proxy IP). UA rotation. Single `page.goto()` per browser + `setView()` panning avoids repeated Cloudflare challenges. Tab parallelism shares one Cloudflare pass across multiple regions.

## Notes

- `data/` directory is gitignored; it holds `captures_log.json` and saved images. The database is external (PostgreSQL).
- At 8K viewport, most regions fit in a single tile (57 total tiles across 34 regions).

## Learning & Discovery

- When you use a library, framework, technique, or import that I may not be familiar with, briefly introduce it before using it: what it is, why it's useful here, and how it works.
- Break down unfamiliar concepts step by step — from the high-level idea down to the specific usage in context.
- If there are common alternatives, mention them and explain why you chose this one.
- Keep explanations concise but complete enough to understand without external lookup.
