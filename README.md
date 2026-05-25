# OceanScrape

![OceanScrape dashboard](OceanScrape.png)

OceanScrape is a global maritime traffic monitoring pipeline. It periodically scrapes the public MarineTraffic.com live map across 79 ocean regions, detects individual ship markers from the rendered tiles using computer vision, projects each marker back into latitude and longitude, and stores the results in PostgreSQL for analysis and visualization through a web dashboard.

## Purpose

Commercial vessel tracking feeds are expensive and often limited by region or update frequency. OceanScrape was built to assemble a long running, self hosted record of global shipping activity by combining a hardened browser scraper with inline image analysis. The goal is not to replace AIS data, but to produce a continuous, queryable snapshot of where ships are, how many are moving versus stationary, and how traffic shifts over time across the world's most important shipping corridors.

## Goal

* Cover roughly 35 percent of the world's ocean surface across 79 regions, including every major chokepoint (Suez, Panama, Malacca, Hormuz, Bosporus, Gibraltar, and others) and the open ocean shipping lanes that connect them.
* Run on a schedule (default every 60 minutes with jitter) without being detected or rate limited.
* Produce structured, time series data: per region ship counts, per vessel positions, and historical aggregates suitable for plotting, alerting, or modeling.
* Surface that data through a live dashboard that lets you scrub through time, filter by vessel type and motion, and inspect any region in detail.

## How it works

```
scraper_global.py  →  seer.py (inline OpenCV)  →  captures_log.jsonl  →  update_database.py  →  PostgreSQL  →  api.py  →  dashboard/
   browser workers      in memory detection         counts + markers       psycopg2 batch insert       FastAPI       Leaflet UI
```

1. **Scrape.** `scraper_global.py` launches a small pool of Patchright browser workers (an undetected Playwright fork). Each worker visits MarineTraffic once, then pans the Leaflet map to every assigned region using `setView()` so the same Cloudflare pass is reused. Regions are pulled from a shared queue with work stealing and processed largest first to flatten tail latency.
2. **Detect.** Each rendered tile is passed in memory to `seer.py`, which uses HSV color masking and contour analysis in OpenCV to find ship markers. Triangles (three vertices via `approxPolyDP`) are classified as moving ships, circles (high circularity ratio) as stationary. Color separates tankers from cargo vessels.
3. **Project.** Marker pixel positions are converted to real world coordinates using a Web Mercator inverse projection in `grid.py`, so every detected ship has a latitude and longitude tied to its tile and timestamp.
4. **Persist.** `update_database.py` reads the JSONL capture log and batch inserts into two PostgreSQL tables: `captures` (per region snapshot with counts and metadata as JSONB) and `vessel_positions` (one row per detected marker, joined to its capture). Writes are idempotent through `ON CONFLICT`.
5. **Serve.** `api.py` exposes the database through a FastAPI service, and `dashboard/index.html` renders a live Leaflet map with time scrubbing, vessel type and motion filters, per region drill down, and aggregate charts (see the screenshot above).

## Key features

* **Anti detection by design.** Patchright with the real Chrome channel, headless new mode, rotating residential proxies via Decodo, and full geo profile spoofing (timezone, locale, geolocation, Accept Language) derived from the proxy IP.
* **Browser reuse.** One `page.goto()` per worker followed by Leaflet panning for all subsequent regions and tiles, which avoids repeated Cloudflare challenges.
* **High resolution capture.** Default viewport is 8K (7680 by 4320), so most regions fit in a single tile and detection runs once per region instead of stitching.
* **Zoom tiered coverage.** Five zoom levels (z9 through z13) are assigned per region: narrow canals at z13 for marker separation, open ocean lanes at z9 for area coverage.
* **CLI filters.** Run all regions, a single region, a tier, a zoom level, or any combination (`--tier=1 --zoom=9`, `--regions=N,S,P`, and so on).
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
python scraper_global.py --tier=original    # only the 34 original chokepoint regions
python scraper_global.py --regions=N,S,P    # Suez North, Suez South, Panama only
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
```

Region polygons are also overridable from `.env` (for example `NORTH_POLYGON="lat,lon;lat,lon;..."`) without touching code.

## Project structure

| File | Role |
| --- | --- |
| `scraper_global.py` | Main scraper. Browser pool, work stealing region queue, inline detection. |
| `seer.py` | OpenCV ship counter and marker extractor (HSV mask, contours, shape classification). |
| `grid.py` | Web Mercator projection helpers and tile grid generation. |
| `regions.py` | All 79 region polygons, zoom assignments, and tier classifications. |
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
