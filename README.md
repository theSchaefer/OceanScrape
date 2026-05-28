# OceanScrape

![OceanScrape dashboard](OceanScrape.png)

OceanScrape is a global maritime traffic monitoring pipeline. It periodically scrapes the public MarineTraffic.com live map through a deterministic Web-Mercator tile grid, detects individual ship markers from the rendered tiles using computer vision, projects each marker back into latitude and longitude, and stores the results in PostgreSQL for analysis and visualization through a web dashboard.

## Purpose

Commercial vessel tracking feeds are expensive and often limited by region or update frequency. OceanScrape was built to assemble a long running, self hosted record of global shipping activity by combining a hardened browser scraper with inline image analysis. The goal is not to replace AIS data, but to produce a continuous, queryable snapshot of where ships are, how many are moving versus stationary, and how traffic shifts over time across the world's most important shipping corridors.

## Goal

* Cover the configured shipping-world grid (`lat -60..75`, `lon -180..180` by default), with higher zoom tiles seeded from the historical chokepoint/corridor regions.
* Run on a schedule (default every 60 minutes with jitter) without being detected or rate limited.
* Produce structured, time series data: per tile captures, global vessel positions, and region aggregates computed after capture by point-in-polygon.
* Surface that data through a live dashboard that lets you scrub through time, filter by vessel type and motion, and inspect any region in detail.

## How it works

```
scraper_global.py  →  seer.py (inline OpenCV)  →  captures_log.jsonl  →  update_database.py  →  PostgreSQL  →  api.py  →  dashboard/
   browser workers      in memory detection         counts + markers       psycopg2 batch insert       FastAPI       Leaflet UI
```

1. **Scrape.** `scraper_global.py` launches a small pool of Patchright browser workers (an undetected Playwright fork). Workers process same-zoom batches from a global Web-Mercator tile manifest. Regions are debug filters only; persisted captures are tile-scoped.
2. **Detect.** Each rendered tile is passed in memory to `seer.py`, which uses HSV color masking and contour analysis in OpenCV to find ship markers. Triangles (three vertices via `approxPolyDP`) are classified as moving ships, circles (high circularity ratio) as stationary. Color separates tankers from cargo vessels.
3. **Project.** Marker pixel positions are converted to real world coordinates using a Web Mercator inverse projection in `grid.py`. The scraper reads MarineTraffic's mouse-position DOM control at the center of `#map_canvas` to anchor projection without requiring Leaflet access.
4. **Persist.** `update_database.py` reads the JSONL capture log and batch inserts into `capture_tiles`, `tile_captures`, and `global_vessel_positions`. The old region tables remain as archive data, but new API responses use the global tables.
5. **Serve.** `api.py` exposes the database through a FastAPI service, and `dashboard/index.html` renders a live Leaflet map with time scrubbing, vessel type and motion filters, per region drill down, and aggregate charts (see the screenshot above).

## Key features

* **Anti detection by design.** Patchright with the real Chrome channel, headless new mode, rotating residential proxies via Decodo, and full geo profile spoofing (timezone, locale, geolocation, Accept Language) derived from the proxy IP.
* **Global tile capture.** Captures are keyed by deterministic `tile_id` values (`g_z{zoom}_r{row}_c{col}`) from Web-Mercator pixel rows and columns.
* **Exclusive marker ownership.** A marker is accepted only by its deterministic owner tile. Higher zoom seeded tiles win over lower zoom world tiles; ties resolve by nearest tile center and stable tile id.
* **High resolution capture.** Default viewport is 8K (7680 by 4320), so most regions use only a small deterministic tile grid.
* **Static crowdedness zooms.** Region zoom is derived from `low`, `medium`, or `high` crowdedness; default production mapping is z9, z10, and z12.
* **QA status.** Cross-zoom QA is now diagnostic-only for global tile capture. Sustainable zoom/split decisions still need a dedicated QA redesign.
* **CLI filters.** Run the whole grid, or debug-select tiles by historical region, tier, or zoom (`--tier=1 --zoom=9`, `--regions=N,S,P`, and so on).
* **Inline detection.** Tiles are analyzed in memory by default; images are only written to disk when `--save-images` is set.
* **Dashboard with history.** The frontend supports time scrubbing across the full database, marker deduplication in overlapping regions, tile boundary visualization, vessel type and motion filters, and CSV export of positions.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate     # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure .env with proxy credentials and DATABASE_URL (see Configuration)

python run.py                 # one full pipeline pass: scrape, detect, write to DB
```

Other common invocations:

```bash
python scraper_global.py --list-regions     # show every region code, zoom, and tier
python scraper_global.py --dry-run-grid     # print global tile counts without scraping
python scraper_global.py --list-tiles       # print selected tile ids
python scraper_global.py --tier=original    # only the 34 original chokepoint regions
python scraper_global.py --regions=N,S,P    # Suez North, Suez South, Panama only
python scraper_global.py --regions=BS --once --no-ingest
                                           # validation pass, no DB insert
python seer.py path/to/tile.jpg             # run detection on a single image (CLI mode)
python discover_map.py                      # one shot JS introspection of MarineTraffic
uvicorn api:app --reload                    # serve the dashboard API locally
```

## Configuration (`.env`)

```
DECODO_USERNAME / DECODO_PASSWORD   Proxy credentials (decodo.com, ports 10011 to 10025)
VIEWPORT_WIDTH / VIEWPORT_HEIGHT    Default 7680 by 4320 (8K, maximizes area per tile)
MAX_BROWSERS                        Default 2 concurrent workers; scale up to RAM limit
SAVE_IMAGES                         Default 0; set to 1 or use --save-images
SCREENSHOT_FORMAT / QUALITY         Default jpeg / 85
SCRAPE_INTERVAL_MINUTES             Default 60
JITTER_SECONDS                      Default 300
DATABASE_URL                        e.g. postgresql://user:pass@localhost:5432/marinescraper
USE_BBOX_TILING                     Default 1; bbox-first tile coverage
BBOX_OVERLAP_PX                     Default 128 screenshot-pixel overlap between tiles
GLOBAL_GRID_BBOX                    Default -60,-180,75,180 (min_lat,min_lon,max_lat,max_lon)
GLOBAL_GRID_DEFAULT_ZOOM            Default 9 for the shipping-world base grid
GLOBAL_TILE_BATCH_SIZE              Default 12 same-zoom tiles per browser worker
TILE_ACCEPT_BUFFER_PX               Default 8 owner-acceptance buffer in Web-Mercator pixels
RESPECT_TILE_SCHEDULE               Default 1; select only enabled tiles whose schedule is due
MARKER_DEDUP_EPS_DEG                Default 0.003; collapse near-duplicate markers
ENABLE_CROSS_ZOOM_QA                Default 1; diagnostic only for global tile capture
QA_SAMPLE_RATE / QA_MAX_SAMPLES     Default 0.10 / 3 sampled baseline tiles per region
USE_SETVIEW_OPTIMIZATION            Default 0; set 1 to allow optional Leaflet setView panning
LEAFLET_DIAGNOSTICS                 Default 0; set 1 to emit Leaflet/frame probes
```

Projection offset is derived from MarineTraffic's
`.leaflet-control-mouseposition` DOM element. The scraper moves the mouse to
the center of the map canvas, parses the precise `(lat, lon)` line, then hides
cursor/hover UI before capture.

Region polygons are also overridable from `.env` (for example `NORTH_POLYGON="lat,lon;lat,lon;..."`) without touching code.

## Project structure

| File | Role |
| --- | --- |
| `scraper_global.py` | Main scraper. Global tile-batch browser workers, drag traversal, inline detection. |
| `seer.py` | OpenCV ship counter and marker extractor (HSV mask, contours, shape classification). |
| `global_tile_grid.py` | Global Web-Mercator tile manifest, tile ownership, and GeoJSON helpers. |
| `grid.py` | Web Mercator projection helpers, bbox tiling, and legacy polygon grids. |
| `regions.py` | All 79 region definitions plus normalized bbox/crowdedness loading. |
| `geo_profile.py` | Proxy IP geolocation lookup and locale, timezone, language mapping. |
| `update_database.py` | PostgreSQL schema bootstrap and batch insert from `captures_log.jsonl`. |
| `api.py` | FastAPI service that powers the dashboard. |
| `dashboard/index.html` | Leaflet based frontend with time scrubbing and filters. |
| `run.py` | Orchestrator: scrape, then update database. |
| `discover_map.py` | Dev tool that introspects how MarineTraffic stores its Leaflet instance. |

## Disclaimer

This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the author(s) be liable for any claim, damages, or other liability arising from the use of this software.

This tool is intended for lawful use only. It is the sole responsibility of the user to ensure that their use of this software complies with all applicable laws, regulations, and the terms of service of any website or service they interact with. The author(s) assume no responsibility or liability for how this software is used, or for any consequences resulting from its use.

By using this software, you agree that you do so at your own risk.
