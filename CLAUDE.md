# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**boatBloat** — A global maritime chokepoint monitor. Scrapes MarineTraffic.com using anti-detection browser automation across 34 ocean regions (5 zoom tiers), counts ships via OpenCV computer vision inline, stores results in SQLite, and displays economic impact estimates on a PHP dashboard. Covers ~16% of global ocean area including all major shipping lanes and chokepoints.

## Key Commands

```bash
# Activate virtual environment
cd boatBloat && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the full pipeline (scrape → inline detection → database)
python run.py

# Run individual components
python scraper_global.py                  # Global scraper (34 regions, tab parallelism)
python scraper_global.py --save-images    # Also save tile images to disk
python scraper_global.py --regions N,S,H  # Run specific regions only
python scraper_global.py --list-regions   # Show all defined regions
python scraper_patchright_pan.py          # Legacy scraper (8 regions, one browser per region)
python seer.py                            # OpenCV ship counter (CLI mode)
python update_database.py                 # Write results to SQLite from captures log

# Serve the web dashboard (requires PHP)
cd web && php -S localhost:8000
```

## Architecture

### Current pipeline (scraper_global.py)

```
scraper_global.py  →  (inline seer.py)  →  captures_log.json  →  update_database.py  →  web/get_latest.php
  (browser + tabs)    (in-memory OpenCV)     (counts + metadata)    (SQLite writes)        (dashboard API)
```

Ship detection happens **inline** during capture — `count_ships_from_bytes()` runs OpenCV on the screenshot bytes in memory. By default, no images are saved to disk (use `--save-images` flag for debugging). The captures log JSON contains per-tile tanker/cargo counts and the database is updated from that.

### Legacy pipeline (scraper_patchright_pan.py)

```
scraper_patchright_pan.py  →  seer.py  →  update_database.py  →  web/get_latest.php
    (browser scraping)       (OpenCV)     (SQLite writes)         (dashboard API)
```

### Key modules

- **`scraper_global.py`** — Main scraper. 34 regions across 5 zoom tiers (z9-z13). Uses tab-based parallelism (multiple regions per browser, `TABS_PER_BROWSER` tabs × `MAX_BROWSERS` concurrent browsers). Inline OpenCV detection. JPEG screenshots. Smart AIS marker detection replaces fixed sleeps.
- **`scraper_patchright_pan.py`** — Legacy scraper (8 regions, one browser per region, fixed zoom 13). Still functional but superseded by `scraper_global.py`.
- **`grid.py`** — Mercator projection utilities. Converts lat/lon ↔ pixel coordinates. Computes tile grids with snake/boustrophedon ordering. Includes `generate_ocean_grid()` for auto-tiling large bounding boxes and `tile_area_km2()` for capacity planning.
- **`geo_profile.py`** — Resolves proxy IP geolocation via ip-api.com. Maps country codes to locale/language settings for browser fingerprinting.
- **`seer.py`** — OpenCV-based ship detection. Detects two marker shapes: **circles** (stationary ships) and **triangles** (moving ships), both in red (tanker) and green (cargo) colors. Uses HSV color masking → contour detection → shape classification via `cv2.approxPolyDP` (3 vertices = triangle/moving) and circularity ratio (high circularity = circle/stationary). Returns dict with `stationary_tankers`, `moving_tankers`, `stationary_cargos`, `moving_cargos`. Provides `count_ships_from_bytes()` for in-memory detection and `extract_marker_coords()` for lat/lon position extraction with `"motion": "stationary"|"moving"` field.
- **`run.py`** — Pipeline orchestrator. Runs `scraper_global.py` then `update_database.py`.
- **`update_database.py`** — Reads captures log JSON and inserts into SQLite. Supports both inline detection counts (from `scraper_global.py`) and legacy OpenCV-on-disk mode.
- **`web/index.php`** — Dashboard frontend (Tailwind CSS, TradingView chart embed). Shows per-region vessel cards with stationary/moving breakdown for all chokepoints.
- **`web/get_latest.php`** — JSON API. Groups by `region` column, returns per-region tanker/cargo counts with moving/stationary breakdown + economic model outputs. Stationary ships are weighted higher in the economic model (indicate congestion).

## Region Definitions (scraper_global.py)

34 regions organized by zoom tier:

| Zoom | Regions | Use case |
|------|---------|----------|
| **13** | Suez N/S, Panama, Malacca, Bosporus | Narrow canals — individual ships must be distinguishable |
| **12** | Hormuz, Bab al-Mandab, Gibraltar, Dover, Sunda, Lombok, Singapore | Dense chokepoints |
| **11** | Taiwan, Korea, Danish, Sicily, Yucatan | Wide straits, moderate traffic |
| **10** | South China Sea, Red Sea, Persian Gulf, Gulf of Aden, East China Sea, Mozambique, Cape of Good Hope, Java Sea, Yellow Sea | Regional corridors |
| **9** | North Atlantic E/W, Mediterranean E/W, Arabian Sea, Bay of Bengal, Western Pacific, Indian Ocean | Open ocean shipping lanes |

Each region has: `polygon` (lat/lon bounds), `zoom` (per-region), `name` (human-readable). All are env-var overridable via `_parse_polygon()`.

## Configuration

Scraper config in `boatBloat/.env`:
- `VIEWPORT_WIDTH`/`VIEWPORT_HEIGHT` (default 7680×4320) — 8K viewport for maximum area per tile
- `ZOOM_LEVEL` (default 13) — Fallback only; `scraper_global.py` uses per-region zoom
- `TABS_PER_BROWSER` (default 4) — Regions loaded as tabs in one browser
- `MAX_BROWSERS` (default 4) — Concurrent browser processes
- `SAVE_IMAGES` (default 0) — Set to 1 or use `--save-images` flag to save tiles to disk
- `SCREENSHOT_FORMAT` (default jpeg), `SCREENSHOT_QUALITY` (default 85)
- `SCRAPE_INTERVAL_MINUTES` (default 60), `JITTER_SECONDS` (default 300)
- Region polygons: `NORTH_POLYGON`, `SOUTH_POLYGON`, etc. (semicolon-separated lat,lon pairs)

## Database Schema (SQLite — history.db)

Single `history` table: `image` (BLOB, nullable), `is_north` (BOOLEAN, legacy), `region` (TEXT), `tankers`/`cargos` (INTEGER, total counts), `moving_tankers`/`moving_cargos` (INTEGER, moving-only subset), `date_time`, `run_timestamp`, `tile_row`/`tile_col`, `center_lat`/`center_lon`, `zoom`, `status`, `file_size_kb`.

Stationary counts are derived: `stationary_tankers = tankers - moving_tankers`. The `region` column replaced the binary `is_north` flag. Old rows are backfilled via `ALTER TABLE` migration in `_ensure_table()`. The `moving_tankers`/`moving_cargos` columns are also auto-migrated (default 0 for pre-existing rows).

## Anti-Detection Strategy

Patchright (undetected Playwright fork) with `channel="chrome"` + `--headless=new`. Proxy rotation (decodo.com, ports 10011-10025). Geo-profile spoofing (timezone, locale, geolocation from proxy IP). UA rotation. Leaflet map hooks to disable inertia. Single-load + `setView()` panning avoids repeated `page.goto()` and Cloudflare checks. Tab parallelism shares one Cloudflare pass across multiple regions in the same browser.

## Performance Notes

- At 7680×4320 viewport, most regions fit in 1 tile (57 total tiles across 34 regions)
- Tab parallelism: 4 tabs/browser × 4 browsers = 16 regions loading concurrently
- Smart AIS detection (`_wait_for_ais_markers`) replaces fixed 500ms sleeps — detects overlay canvas content, falls back after timeout
- JPEG output is ~5× smaller and ~2× faster to encode than PNG
- Inline detection avoids writing images to disk (default) — total storage per run is ~40 KB
- `generate_ocean_grid()` in `grid.py` can auto-tile arbitrary bounding boxes for expansion

## Learning & Discovery

- When you use a library, framework, technique, or import that I may not be familiar with,
  briefly introduce it before using it: what it is, why it's useful here, and how it works.
- Break down unfamiliar concepts step by step — from the high-level idea down to the
  specific usage in context.
- If there are common alternatives, mention them and explain why you chose this one.
- Keep explanations concise but complete enough to understand without external lookup.
