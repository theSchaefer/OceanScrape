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
   capture → raw JSONL → ingest → postgres → api/dashboard
scraper_global.py  →  seer.py (inline OpenCV)  →  data/raw/runs/<id>/captures.jsonl  →  update_database.py  →  PostgreSQL  →  api.py  →  dashboard/
   browser workers      in memory detection         one file per run (no auto-ingest)     psycopg2 batch insert      FastAPI       Leaflet UI
```

Capture and ingest are **decoupled**. By default the scraper only writes raw data; loading a run into PostgreSQL is a separate `python update_database.py <path>` step. `run.py` chains both for a one-shot full pass.

1. **Scrape.** `scraper_global.py` launches a small pool of Patchright browser workers (an undetected Playwright fork). Workers process same-zoom batches from a global Web-Mercator tile manifest. Regions are debug filters only; persisted captures are tile-scoped.
2. **Detect.** Each rendered tile is passed in memory to `seer.py`, which uses HSV color masking and contour analysis in OpenCV to find ship markers. Triangles (three vertices via `approxPolyDP`) are classified as moving ships, circles (high circularity ratio) as stationary. Color separates tankers from cargo vessels.
3. **Project.** Marker pixel positions are converted to real world coordinates using a Web Mercator inverse projection in `grid.py`. The scraper reads MarineTraffic's mouse-position DOM control at the center of `#map_canvas` to anchor projection without requiring Leaflet access.
4. **Capture (raw).** Each run is written to its own `data/raw/runs/<run_id>/captures.jsonl` (one JSON object per line) and the `data/raw/runs/LATEST` pointer is updated. The scraper does **not** touch PostgreSQL by default — the raw file is the deliverable.
5. **Ingest.** `python update_database.py data/raw/runs/<run_id>/captures.jsonl` reads that run and batch inserts into `capture_tiles`, `tile_captures`, and `global_vessel_positions`. The insert is idempotent (`ON CONFLICT DO NOTHING`); on success an `ingested.json` status marker is written beside the raw file, which is never moved or rewritten. The old region tables remain as archive data, but new API responses use the global tables.
6. **Serve.** `api.py` exposes the database through a FastAPI service, and `dashboard/index.html` renders a live Leaflet map with time scrubbing, vessel type and motion filters, per region drill down, and aggregate charts (see the screenshot above).

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

python run.py                 # one full pipeline pass: scrape, detect, raw JSONL, ingest to DB
```

The two-step (decoupled) flow:

```bash
python scraper_global.py                    # capture only → data/raw/runs/<run_id>/captures.jsonl
python update_database.py data/raw/runs/<run_id>/captures.jsonl   # ingest that run into PostgreSQL
```

Other common invocations:

```bash
python scraper_global.py --ingest           # capture then ingest inline (skip the separate step)
python scraper_global.py --list-regions     # show every region code, zoom, and tier
python scraper_global.py --dry-run-grid     # print global tile counts without scraping
python scraper_global.py --list-tiles       # print selected tile ids
python scraper_global.py --tier=original    # only the 34 original chokepoint regions
python scraper_global.py --regions=N,S,P    # Suez North, Suez South, Panama only
python scraper_global.py --regions=BS --once --tile-ids=g_z9_r0_c0
                                           # single-run validation pass (raw only, no DB)
python update_database.py                    # legacy: ingest + archive data/captures_log.jsonl
python seer.py path/to/tile.jpg             # run detection on a single image (CLI mode)
python discover_map.py                      # one shot JS introspection of MarineTraffic
uvicorn api:app --reload                    # serve the dashboard API locally
```

## Distributed capture: control plane + workers

OceanScrape can keep running as a single server, **or** fan capture out to
remote worker machines over a private network. The split is deliberate:

* **Workers** are stateless capture nodes. They never touch PostgreSQL, never
  read the control plane's `.env`, and never ingest. A worker claims a batch of
  tile ids, captures them with the same `scraper_global` code, and uploads the
  raw JSONL back to the control plane.
* **The control plane** owns the queue, the leases, the uploaded artifacts, and
  (optionally) the eventual ingest. It is the only process that talks to the
  database.

Capture jobs live in a persistent queue (SQLite by default, or Postgres via
`WORKER_QUEUE_DSN=$DATABASE_URL`). Claiming is atomic and lease-based: a claimed
job is invisible to other workers until its lease expires, so a crashed worker's
batch is automatically reclaimed — no job is lost or double-captured.

### Single-server mode (unchanged)

Nothing above is required for a single box. The existing flows still work
exactly as before:

```bash
python run.py                               # full pipeline (scrape → raw → ingest)
python scraper_global.py --once             # one local capture run, raw only
python update_database.py data/raw/runs/<run_id>/captures.jsonl   # ingest a run
```

### Control plane + worker mode

On the **control plane** (the box with PostgreSQL):

```bash
# 1. Start the worker API (binds 127.0.0.1:8081 by default)
WORKER_API_TOKEN=$(openssl rand -hex 24) python run.py serve

# 2. Enqueue capture batches (in another shell, same WORKER_QUEUE_DSN)
python run.py enqueue --regions global --batch-size 12
python run.py enqueue --tier 1 --zoom 9 --batch-size 8
python run.py enqueue --due-only            # only tiles due per schedule
python run.py enqueue --regions global --dry-run   # preview without writing

# Or keep global waves running continuously (requires auto-ingest by default)
WORKER_API_AUTO_INGEST=1 python run.py continuous \
  --regions global --batch-size 12
```

On each **worker** host (needs the capture stack + its own proxy creds, but no
DB):

```bash
export WORKER_TOKEN=<the token from the control plane>
python run.py worker --server http://10.0.0.3:8081 --token-env WORKER_TOKEN --max-browsers 1
```

A worker process captures one batch at a time (one batch == one browser). To use
more browsers on a host, run several worker processes. Send `SIGINT`/`SIGTERM`
to stop gracefully after the current batch.

By default the control plane stores uploaded raw JSONL under
`data/raw/queue/<batch_id>.jsonl` and does **not** ingest (preserving the
raw/ingest separation). Set `WORKER_API_AUTO_INGEST=1` to ingest on upload via
the existing `update_database.ingest_file`, or ingest the artifacts later.

`run.py continuous` owns one non-overlapping wave at a time. It writes its
current enqueue id before creating jobs, resumes that wave after a process
restart, waits for unrelated pre-existing queue work, and enqueues the next
wave immediately after all batches are terminal. An exclusive lock prevents two
local orchestrators from producing duplicate waves. By default it refuses to
start without `WORKER_API_AUTO_INGEST=1` and stops before the next wave if any
batch or auto-ingest fails. `SIGINT`/`SIGTERM` prevents another wave from being
created but leaves the current jobs and artifacts intact.

Each uploaded batch is ingested immediately after completion. Every tile now
records a microsecond-precision UTC `captured_at` taken when its screenshot
starts; `ingested_at` remains the later database-write time. Tiles in a batch
therefore no longer share the browser/batch startup timestamp.

> **Tile-id consistency:** workers rebuild each batch's tile geometry from their
> own deterministic manifest, so every host must share the same
> `GLOBAL_GRID_BBOX`, `GLOBAL_GRID_DEFAULT_ZOOM`, `VIEWPORT_WIDTH/HEIGHT`.

### Hetzner private-network example

Bind the control plane on the private interface and point workers at it over the
private subnet (capture traffic and the token never traverse the public net):

```bash
# Control plane (10.0.0.3) — uses Postgres for the queue too
WORKER_API_HOST=10.0.0.3 WORKER_API_PORT=8081 \
WORKER_API_TOKEN=<shared-secret> WORKER_QUEUE_DSN=$DATABASE_URL \
python run.py serve

# Worker (10.0.0.4, 10.0.0.5, ...)
SERVER_URL=http://10.0.0.3:8081 WORKER_TOKEN=<shared-secret> \
python run.py worker --max-browsers 1
```

### Worker / control-plane configuration

```
# Control plane (worker API)
WORKER_API_TOKEN                    Single bearer token workers must present
WORKER_TOKENS                       Or a comma-separated set of accepted tokens
WORKER_API_HOST                     Bind address (default 127.0.0.1; e.g. 10.0.0.3)
WORKER_API_PORT                     Bind port (default 8081)
WORKER_QUEUE_DSN                    Queue store (default sqlite:///data/worker_queue.sqlite3;
                                    set to $DATABASE_URL to use Postgres)
WORKER_ARTIFACTS_DIR               Where uploaded raw JSONL is stored (default data/raw/queue)
WORKER_API_AUTO_INGEST             Default 0; set 1 to ingest artifacts on upload
WORKER_API_ALLOW_NO_AUTH           Default 0; set 1 to allow unauthenticated (dev only)
WORKER_LEASE_SECONDS               Default 600; lease/visibility timeout per batch
WORKER_MAX_ATTEMPTS                Default 3; claim attempts before a batch is failed
CONTINUOUS_POLL_SECONDS            Default 2; terminal-wave polling interval
CONTINUOUS_STATE_FILE              Restart-safe current-wave state JSON
CONTINUOUS_LOCK_FILE               Exclusive local orchestrator lock

# Worker host
SERVER_URL                          Control plane base URL (or pass --server)
WORKER_TOKEN                        Bearer token (referenced via --token-env)
WORKER_ID                           Optional stable worker id (default host-pid-rand)
WORKER_SCRATCH_DIR                  Transient per-batch JSONL dir (default data/worker_scratch)
```

Tokens are never logged and are sent only in the `Authorization: Bearer` header;
proxy credentials stay inside `scraper_global` and are likewise never emitted.

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
MARKER_DEDUP_EPS_DEG                Default 0.003; legacy/API-view dedup only;
                                    global tile raw storage preserves all owned markers
ENABLE_CROSS_ZOOM_QA                Default 1; diagnostic only for global tile capture
QA_SAMPLE_RATE / QA_MAX_SAMPLES     Default 0.10 / 3 sampled baseline tiles per region
LEAFLET_DIAGNOSTICS                 Default 0; set 1 to emit Leaflet/frame probes
MOUSE_DRAG_STEPS                    Default 3; intermediate mouse-moves per drag step (dominant pan cost at 8K — raise only if coverage gaps appear)
MAX_DRAG_PX                         Default 800; max single-drag distance, kept below Leaflet inertia threshold
URL_NAV_MAX_DRAG_STEPS              Default 24; above this many drag steps a viewport move uses a fresh URL load instead of dragging (far-jump fallback for sparse tiles)
TILES_WAIT_MS                       Default 5000; base-map readiness cap, early-exits via .leaflet-tile-loaded
AIS_WAIT_MS                         Default 3000; vessel-data readiness cap (get_data_json_4 network quiescence)
AIS_QUIET_MS                        Default 400; quiet window that marks vessel-data fetch complete
AIS_RENDER_SETTLE_MS               Default 250; post-fetch settle so markers paint before screenshot
AIS_FIRST_RESPONSE_GRACE_MS        Default 1200; bail out early if a pan triggers no vessel request (cached/empty area)
```

Panning uses mouse-drag only: MarineTraffic's Leaflet map instance is not
reachable from injected JS, so `setView` is unavailable. Because every
intermediate mouse move repaints the full ~33MP canvas at 8K, `MOUSE_DRAG_STEPS`
is the largest lever on scrape speed. Readiness is verified without the map
object: base tiles via the `.leaflet-tile-loaded` DOM class, and vessel markers
via quiescence of the `get_data_json_4` network responses (the marker canvas is
cross-origin tainted, so it cannot be pixel-sampled).

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
| `run.py` | Multi-mode entrypoint: full pipeline, plus `worker` / `enqueue` / `serve` subcommands. |
| `worker_queue.py` | Persistent capture-job queue (SQLite/Postgres): atomic claim, leases, retries. |
| `worker_api.py` | Control-plane FastAPI: token-auth claim/heartbeat/complete/fail + artifact upload. |
| `worker.py` | Distributed capture worker client (claim → capture → upload raw JSONL). |
| `worker_enqueue.py` | Turns tile-selection params into queued capture batches. |
| `discover_map.py` | Dev tool that introspects how MarineTraffic stores its Leaflet instance. |

## Disclaimer

This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the author(s) be liable for any claim, damages, or other liability arising from the use of this software.

This tool is intended for lawful use only. It is the sole responsibility of the user to ensure that their use of this software complies with all applicable laws, regulations, and the terms of service of any website or service they interact with. The author(s) assume no responsibility or liability for how this software is used, or for any consequences resulting from its use.

By using this software, you agree that you do so at your own risk.
