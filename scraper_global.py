#!/bin/python3
"""Patchright scraper — global maritime chokepoint monitor.

Single-region-per-worker model: each region is an atomic task processed in
a fresh browser/context.  Up to MAX_BROWSERS regions run in parallel, but
no worker ever transitions between regions.  This eliminates state drift
(map zoom/center, UI overlays, vessel filter, projection offset) that the
previous work-stealing model accumulated across stolen regions.  Supports
per-region zoom levels, inline OpenCV ship detection, and JPEG output for
minimal storage.

Each run writes raw captures to its own file —
``data/raw/runs/<run_id>/captures.jsonl`` — and updates ``data/raw/runs/LATEST``.
By default the scraper does NOT ingest into PostgreSQL; ingest is a separate
step (``python update_database.py <path>``). Pass ``--ingest`` to ingest inline.

Usage:
  python scraper_global.py                  # Capture all tiles (raw only, no DB)
  python scraper_global.py --ingest         # Capture then ingest into PostgreSQL
  python scraper_global.py --save-images    # Also save tile images
  python scraper_global.py --regions N,S,H  # Run specific regions only
  python scraper_global.py --zoom=9         # Run only zoom-level 9 regions
  python scraper_global.py --tier=1         # Tier 1 (major trade arteries)
  python scraper_global.py --tier=original  # Original 34 chokepoint regions
  python scraper_global.py --tier=1,2       # Tiers 1+2 combined
  python scraper_global.py --list-regions    # Show all defined regions
"""

import base64
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import schedule as sched
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from patchright.sync_api import sync_playwright

from debug_map_probe import run_map_probe, write_frame_scan
from geo_profile import GeoProfile, resolve_all_proxies
from global_tile_grid import (
    GlobalTileIndex,
    build_global_tile_manifest,
    manifest_summary,
    parse_global_bbox,
    tile_intersects_polygon,
    tile_scan_key,
)
from grid import (
    get_tile_centers, get_bbox_tile_centers, polygon_to_pixel_coords,
    tile_id as make_tile_id, _point_in_polygon,
    lat_to_pixel_y, lon_to_pixel_x, pixel_x_to_lon, pixel_y_to_lat,
)
from regions import REGIONS, REGION_TIERS, load_bbox_regions
from update_database import get_due_tile_ids, ingest_file

load_dotenv()

# --- Configuration -----------------------------------------------------------

DEFAULT_ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "13"))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "3840"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "2160"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR_PATCHRIGHT_PAN", "./data/captures")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "120"))
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "300"))

# Max concurrent browser workers. In single-region-per-worker mode this caps
# how many regions run in parallel — each worker owns one browser for one
# region, then exits.
MAX_BROWSERS = int(os.getenv("MAX_BROWSERS", "2"))
# Save images to disk (default: only counts are kept)
SAVE_IMAGES = os.getenv("SAVE_IMAGES", "0") == "1" or "--save-images" in sys.argv
# Screenshot format: jpeg is ~5x smaller and ~2x faster to encode than png
SCREENSHOT_FORMAT = os.getenv("SCREENSHOT_FORMAT", "jpeg")
SCREENSHOT_QUALITY = int(os.getenv("SCREENSHOT_QUALITY", "70"))
# Max single-drag distance (px) to avoid Leaflet inertia overshoot. The map
# object is not JS-injectable, so inertia cannot be disabled programmatically;
# 800px keeps each drag step below the inertia threshold. Env-configurable for
# tuning only.
MAX_DRAG_PX = int(os.getenv("MAX_DRAG_PX", "800"))
# Intermediate mouse-move samples per drag step. At an 8K viewport every
# intermediate move forces a full ~33MP Leaflet repaint, so this is the single
# biggest cost driver of panning. Keep small (3) to slash repaints; raise if a
# faster drag causes coverage gaps. (Was hardcoded 20.)
MOUSE_DRAG_STEPS = int(os.getenv("MOUSE_DRAG_STEPS", "3"))
# Far-jump threshold. A viewport move that would need more than this many
# mouse-drag steps is performed as a fresh URL load (page.goto centered on the
# target) instead of dragging. A neighbouring 8K z12 tile is only ~10 steps
# away, so 24 still allows local hops while preventing the 300-500 step drags
# that made sparse-z12 tiles cost thousands of seconds of pure navigation.
URL_NAV_MAX_DRAG_STEPS = int(os.getenv("URL_NAV_MAX_DRAG_STEPS", "24"))
# Readiness-wait caps (ms). Both waits early-exit on a real signal; the cap only
# bounds genuinely-not-ready / empty cases.
TILES_WAIT_MS = int(os.getenv("TILES_WAIT_MS", "5000"))
AIS_WAIT_MS = int(os.getenv("AIS_WAIT_MS", "3000"))
# Vessel data (get_data_json_4) network quiescence window + post-render settle.
AIS_QUIET_MS = int(os.getenv("AIS_QUIET_MS", "400"))
AIS_RENDER_SETTLE_MS = int(os.getenv("AIS_RENDER_SETTLE_MS", "250"))
# If a pan triggers no vessel request within this window (area cached/empty),
# stop waiting instead of running out the full AIS_WAIT_MS cap.
AIS_FIRST_RESPONSE_GRACE_MS = int(os.getenv("AIS_FIRST_RESPONSE_GRACE_MS", "1200"))
LEAFLET_DIAGNOSTICS = os.getenv("LEAFLET_DIAGNOSTICS", "0") == "1"
USE_BBOX_TILING = os.getenv("USE_BBOX_TILING", "1") == "1"
BBOX_OVERLAP_PX = int(os.getenv("BBOX_OVERLAP_PX", "128"))
GLOBAL_GRID_BBOX = parse_global_bbox(os.getenv("GLOBAL_GRID_BBOX"))
GLOBAL_GRID_DEFAULT_ZOOM = int(os.getenv("GLOBAL_GRID_DEFAULT_ZOOM", "9"))
GLOBAL_TILE_BATCH_SIZE = int(os.getenv("GLOBAL_TILE_BATCH_SIZE", "12"))
GLOBAL_TILE_EXCLUDE_IDS = frozenset(
    tile_id
    for tile_id in re.split(r"[\s,]+", os.getenv("GLOBAL_TILE_EXCLUDE_IDS", ""))
    if tile_id
)
TILE_ACCEPT_BUFFER_PX = int(os.getenv("TILE_ACCEPT_BUFFER_PX", "8"))
RESPECT_TILE_SCHEDULE = os.getenv("RESPECT_TILE_SCHEDULE", "1") == "1"
ENABLE_CROSS_ZOOM_QA = os.getenv("ENABLE_CROSS_ZOOM_QA", "1") == "1"
QA_SAMPLE_RATE = float(os.getenv("QA_SAMPLE_RATE", "0.10"))
QA_MIN_SAMPLES = int(os.getenv("QA_MIN_SAMPLES", "1"))
QA_MAX_SAMPLES = int(os.getenv("QA_MAX_SAMPLES", "3"))
QA_TOTAL_RATIO_THRESHOLD = float(os.getenv("QA_TOTAL_RATIO_THRESHOLD", "1.35"))
QA_TYPE_RATIO_THRESHOLD = float(os.getenv("QA_TYPE_RATIO_THRESHOLD", "1.50"))
QA_ABS_DELTA_THRESHOLD = int(os.getenv("QA_ABS_DELTA_THRESHOLD", "5"))
ACTIVE_REGIONS = load_bbox_regions(use_bbox_tiling=USE_BBOX_TILING)
GLOBAL_TILE_MANIFEST = build_global_tile_manifest(
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    global_bbox=GLOBAL_GRID_BBOX,
    default_zoom=GLOBAL_GRID_DEFAULT_ZOOM,
    schedule_minutes=SCRAPE_INTERVAL_MINUTES,
)
GLOBAL_TILE_INDEX = GlobalTileIndex(
    GLOBAL_TILE_MANIFEST,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    global_bbox=GLOBAL_GRID_BBOX,
    accept_buffer_px=TILE_ACCEPT_BUFFER_PX,
)

# Crash retry settings
MAX_REGION_RETRIES = 2
RETRY_BACKOFF_BASE = 5  # seconds

# Suspect-empty-batch heuristic. A "hotspot" tile is one seeded from a
# chokepoint region (priority > 0 / non-empty seed_regions) — somewhere we
# expect ships. If a batch overlapping such tiles comes back with ~no raw
# markers, the AIS session was likely empty (burned proxy / blocked exit IP)
# rather than genuinely empty ocean, so it is flagged as ``suspect_empty_batch``.
SUSPECT_EMPTY_RAW_THRESHOLD = int(os.getenv("SUSPECT_EMPTY_RAW_THRESHOLD", "1"))
SUSPECT_EMPTY_MIN_HOTSPOTS = int(os.getenv("SUSPECT_EMPTY_MIN_HOTSPOTS", "1"))
SUSPECT_EMPTY_ZOOM9_MIN_LAT = float(
    os.getenv("SUSPECT_EMPTY_ZOOM9_MIN_LAT", "-50")
)
SUSPECT_EMPTY_ZOOM9_MAX_LAT = float(
    os.getenv("SUSPECT_EMPTY_ZOOM9_MAX_LAT", "70")
)
# Optional, bounded retry of a suspect batch with a fresh browser + fresh proxy.
# OFF by default: when enabled, per-tile logging is deferred so only the winning
# attempt is persisted (the DB upserts ON CONFLICT (tile_id, captured_at) DO
# NOTHING, so re-logging the same run timestamp would otherwise keep the empty
# capture). Deferring trades away mid-batch crash-resilience, hence opt-in.
SUSPECT_EMPTY_RETRY = os.getenv("SUSPECT_EMPTY_RETRY", "0") == "1"
SUSPECT_EMPTY_MAX_RETRIES = int(os.getenv("SUSPECT_EMPTY_MAX_RETRIES", "1"))
SUSPECT_EMPTY_REQUEUE_EXHAUSTED = (
    os.getenv("SUSPECT_EMPTY_REQUEUE_EXHAUSTED", "0") == "1"
)

# Error substrings that indicate a browser/driver crash (retryable)
_CRASH_PATTERNS = (
    "target crashed", "epipe", "target closed", "browser closed",
    "connection closed", "protocol error", "page.evaluate",
    "mouse.move", "browser has been closed", "crashed",
)


def _is_crash_error(exc: Exception) -> bool:
    """Return True if the exception looks like a Patchright/Chromium crash."""
    msg = str(exc).lower()
    return any(p in msg for p in _CRASH_PATTERNS)


def _suspect_empty_reason(tile_batch, hotspot_tiles, attempt):
    """Classify successful near-empty batches that merit a fresh session.

    Zoom-9 global tiles are not region-seeded, so the hotspot-only rule misses
    empty AIS sessions across otherwise busy ocean lanes. Limit the fallback
    to a configurable latitude belt to avoid retrying predictably empty polar
    and far-southern batches.
    """
    if (
        attempt["setup_failed"]
        or attempt["ok_tiles"] <= 0
        or attempt["raw_total"] > SUSPECT_EMPTY_RAW_THRESHOLD
    ):
        return None
    if hotspot_tiles >= SUSPECT_EMPTY_MIN_HOTSPOTS:
        return "hotspot"
    if tile_batch and int(tile_batch[0]["zoom"]) == 9:
        if any(
            SUSPECT_EMPTY_ZOOM9_MIN_LAT
            <= float(tile["center_lat"])
            <= SUSPECT_EMPTY_ZOOM9_MAX_LAT
            for tile in tile_batch
        ):
            return "zoom9_active_latitude"
    return None


def _should_requeue_exhausted_suspect(best):
    """Return whether a still-suspect winning attempt needs a queue retry."""
    return bool(
        SUSPECT_EMPTY_RETRY
        and SUSPECT_EMPTY_REQUEUE_EXHAUSTED
        and best
        and best.get("suspect")
    )


# --- Tile grid cache ----------------------------------------------------------
# Polygons, zoom levels, and viewport dims are constant across scrape cycles,
# so tile grids only need to be computed once per region.

_tile_grid_cache = {}


def _get_tile_grid(region_name, config):
    """Return (tiles, grid_info) for a region, computing only on first call."""
    cache_key = (
        region_name,
        config.get("zoom"),
        VIEWPORT_WIDTH,
        VIEWPORT_HEIGHT,
        USE_BBOX_TILING,
        BBOX_OVERLAP_PX,
    )
    if cache_key not in _tile_grid_cache:
        if USE_BBOX_TILING and config.get("bbox"):
            _tile_grid_cache[cache_key] = get_bbox_tile_centers(
                config["bbox"],
                config["zoom"],
                VIEWPORT_WIDTH,
                VIEWPORT_HEIGHT,
                overlap_px=BBOX_OVERLAP_PX,
                region_code=region_name,
            )
        else:
            _tile_grid_cache[cache_key] = get_tile_centers(
                config["polygon"], config["zoom"], VIEWPORT_WIDTH, VIEWPORT_HEIGHT
            )
    return _tile_grid_cache[cache_key]


def _region_center(config):
    """Return the startup center for bbox-first or legacy polygon regions."""
    bbox = config.get("bbox")
    if USE_BBOX_TILING and bbox:
        return (
            (bbox["min_lat"] + bbox["max_lat"]) / 2,
            (bbox["min_lon"] + bbox["max_lon"]) / 2,
        )
    return _polygon_center(config["polygon"])


def _unpack_tile(tile, region_name=None, zoom=None):
    """Accept legacy 4-tuples and bbox 5-tuples with deterministic tile ids."""
    row, col, lat, lon = tile[:4]
    tid = tile[4] if len(tile) > 4 else make_tile_id(region_name or "tile", zoom or 0, row, col)
    return row, col, lat, lon, tid


# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_frame_scan_lock = threading.Lock()
_frame_scan_emitted = set()


def _emit_frame_scan(page, timestamp_str, region_name, reason, worker_id=None):
    """Best-effort diagnostic dump; never changes scraper control flow."""
    worker = worker_id or threading.current_thread().name
    key = (id(page), timestamp_str, region_name, reason)
    with _frame_scan_lock:
        if key in _frame_scan_emitted:
            return None
        _frame_scan_emitted.add(key)

    try:
        path = write_frame_scan(
            page,
            timestamp_str=timestamp_str,
            region_name=region_name,
            reason=reason,
        )
        logger.warning(
            "[worker=%s region=%s] frame_scan reason=%s path=%s",
            worker, region_name, reason, path,
        )
        return path
    except Exception as exc:
        logger.warning(
            "[worker=%s region=%s] frame_scan failed reason=%s error=%s",
            worker, region_name, reason, exc,
        )
        return None

# --- Proxies ------------------------------------------------------------------

proxies = []
for i in range(10001, 10011):
    proxy = {
        "server": f"http://isp.decodo.com:{i}",
        "username": os.getenv("DECODO_USERNAME"),
        "password": os.getenv("DECODO_PASSWORD"),
    }
    proxies.append(proxy)

# Geo profiles resolved at startup
geo_profiles: dict[str, GeoProfile] = {}


def _proxy_log_label(proxy: dict, geo: GeoProfile) -> str:
    """Compact label tracing which proxy endpoint / exit-IP a worker uses.

    Essential for spotting IPs that have been exhausted (rate-limited / burned):
    correlate failing captures back to a specific port and exit IP. The port is
    the sticky-session / dedicated-IP selector; exit_ip is resolved at startup."""
    exit_ip = geo.exit_ip or "unresolved"
    return f"{proxy['server']} -> exit_ip={exit_ip} [{geo.country_code}/{geo.city}]"


def _pick_batch_proxy(exclude_servers=()):
    """Pick a proxy + its resolved geo profile for one batch attempt.

    On retries, ``exclude_servers`` lets us avoid re-using the same (possibly
    burned) endpoint so a fresh attempt gets a genuinely different exit IP."""
    candidates = [p for p in proxies if p["server"] not in exclude_servers]
    proxy = random.choice(candidates or proxies)
    geo = geo_profiles.get(proxy["server"])
    if geo is None or not geo.exit_ip:
        raise RuntimeError(
            f"proxy geo profile unavailable for {proxy['server']}; "
            "refusing to use a mismatched fallback identity"
        )
    return proxy, geo

# --- Helpers ------------------------------------------------------------------


def build_url(lat, lon, zoom):
    return (
        f"https://www.marinetraffic.com/en/ais/home"
        f"/centerx:{lon}/centery:{lat}/zoom:{zoom}"
    )


def _chrome_user_agent(browser_version):
    """Build Chrome's reduced UA from the installed browser's actual major."""
    major = str(browser_version).split(".", 1)[0]
    if not major.isdigit():
        raise ValueError(f"invalid Chrome version: {browser_version!r}")
    if sys.platform == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("win"):
        platform_token = "Windows NT 10.0; Win64; x64"
    else:
        platform_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _polygon_center(polygon):
    """Return (lat, lon) center of a polygon."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)


def dismiss_cookie_banner(page, timeout_ms=None):
    """Click the cookie consent 'Accept' button, waiting for it to appear.

    The MarineTraffic consent banner is injected asynchronously, often a couple
    of seconds *after* the map tiles render. A single immediate check therefore
    misses it: Playwright's ``is_visible()`` returns the *current* state and does
    not wait (the ``timeout`` kwarg is effectively a no-op here). When the check
    runs too early the banner stays up, overlays the map-type and vessel-filter
    controls, and the downstream setup steps fail with "shipTypeAccordion
    missing" / "no visible selector matched". So we poll until the banner shows
    up or the deadline passes. Returns True if a banner was clicked.
    """
    if timeout_ms is None:
        timeout_ms = _COOKIE_BANNER_WAIT_MS
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('accept')",
        "button:has-text('AGREE')",
        "button:has-text('Agree')",
        "button:has-text('OK')",
        "#onetrust-accept-btn-handler",
        ".onetrust-accept-btn-handler",
        "button[id*='accept']",
        "button[class*='accept']",
        "button[class*='consent']",
    ]
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    logger.info("  Dismissed cookie banner via: %s", sel)
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def hide_ui_overlays(page):
    """Hide MarineTraffic UI elements, leaving only the map canvas."""
    page.evaluate("""
    () => {
        const selectors = [
            '[class*="sidebar"]', '[class*="Sidebar"]',
            '[class*="menu"]', '[class*="Menu"]',
            '[class*="navbar"]', '[class*="Navbar"]',
            '[class*="controls"]', '[class*="Controls"]',
            '[class*="toolbar"]', '[class*="Toolbar"]',
            '[class*="search"]', '[class*="Search"]',
            '[class*="panel"]', '[class*="Panel"]',
            '[class*="overlay"]', '[class*="Overlay"]',
            '[class*="popup"]', '[class*="Popup"]',
            '[class*="banner"]', '[class*="Banner"]',
            '[class*="zoom"]', '[class*="Zoom"]',
            '[class*="attribution"]', '[class*="logo"]',
            '[class*="divider"]', '[class*="Divider"]',
            '[class*="drawer"]', '[class*="Drawer"]',
            '[class*="rail"]', '[class*="Rail"]',
            '[class*="tab"]', '[class*="Tab"]',
            '[class*="filter"]', '[class*="Filter"]',
            '[class*="legend"]', '[class*="Legend"]',
            '[class*="widget"]', '[class*="Widget"]',
            '[class*="button"]', '[class*="Button"]',
            '[class*="btn"]',
            '[id*="menu"]', '[id*="sidebar"]', '[id*="panel"]',
            '[role="dialog"]', '[role="toolbar"]', '[role="navigation"]',
            'header', 'nav', 'footer',
            '.leaflet-control', '.mapboxgl-ctrl'
        ];
        const elements = document.querySelectorAll(selectors.join(', '));
        for (const el of elements) {
            if (el.matches && el.matches(
                '.leaflet-control-mouseposition, .leaflet-control-mouseposition-dark'
            )) continue;
            if (el.querySelector && el.querySelector(
                '.leaflet-control-mouseposition, .leaflet-control-mouseposition-dark'
            )) continue;
            if (el.tagName.toLowerCase() === 'canvas') continue;
            if (el.querySelector('canvas')) continue;
            if (el.closest('.leaflet-pane')) continue;
            el.style.setProperty('display', 'none', 'important');
        }

        const mc = document.getElementById('map_canvas');
        if (mc && mc.parentElement) {
            for (const sib of mc.parentElement.children) {
                if (sib !== mc && !sib.contains(mc)) {
                    sib.style.setProperty('display', 'none', 'important');
                }
            }
        }

        document.querySelectorAll('*').forEach(el => {
            if (el.tagName.toLowerCase() === 'canvas') return;
            const s = getComputedStyle(el);
            if (s.borderWidth !== '0px' || s.boxShadow !== 'none' || s.outline !== '') {
                el.style.setProperty('border', 'none', 'important');
                el.style.setProperty('box-shadow', 'none', 'important');
                el.style.setProperty('outline', 'none', 'important');
            }
        });
    }
    """)


def install_capture_visibility_css(page):
    """Hide cursor/hover chrome without disabling mouseposition DOM updates."""
    page.evaluate("""
    () => {
        const id = 'marinescraper-capture-visibility';
        let style = document.getElementById(id);
        if (!style) {
            style = document.createElement('style');
            style.id = id;
            document.head.appendChild(style);
        }
        style.textContent = `
            #map_canvas, #map_canvas * { cursor: none !important; }
            .leaflet-control-mouseposition,
            .leaflet-control-mouseposition-dark {
                opacity: 0 !important;
                pointer-events: none !important;
                color: transparent !important;
                text-shadow: none !important;
            }
            .leaflet-popup,
            .leaflet-tooltip,
            [class*="tooltip"],
            [class*="Tooltip"],
            [class*="popover"],
            [class*="Popover"] {
                display: none !important;
                visibility: hidden !important;
                opacity: 0 !important;
            }
        `;
    }
    """)


def hide_hover_artifacts(page):
    """Best-effort cleanup after moving the mouse over the map."""
    try:
        page.evaluate("""
        () => {
            for (const el of document.querySelectorAll(
                '.leaflet-popup, .leaflet-tooltip, [class*="tooltip"], ' +
                '[class*="Tooltip"], [class*="popover"], [class*="Popover"]'
            )) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('opacity', '0', 'important');
            }
        }
        """)
    except Exception:
        pass


# --- Vessel-type filter (Option A: UI click) ---------------------------------
# Uncheck non-cargo/tanker categories in MarineTraffic's filter panel so the
# OpenCV pipeline sees fewer overlapping markers (and therefore fewer
# contour-merge undercounts). Applied once per worker after Cloudflare/cookie
# setup; filter state then persists across all regions that reuse the page.

_DROP_VESSEL_LABELS = (
    "passenger", "high-speed", "high speed", "tug", "special craft",
    "fishing", "pleasure", "sailing", "navigation aid", "unspecified",
    "other", "unknown",
)
_KEEP_VESSEL_LABELS = (
    "cargo", "cargo vessels", "oil tanker", "tanker", "tankers",
)
_MT_SHIP_TYPE_CHECKBOXES = {
    # MarineTraffic ship type ids observed in the map filter panel.
    # Keep only cargo vessels and tankers; hide every other AIS category.
    "0": ("unspecified ships", False),
    "1": ("navigation aids", False),
    "2": ("fishing", False),
    "3": ("tugs/special craft", False),
    "4": ("high speed craft", False),
    "6": ("passenger vessels", False),
    "7": ("cargo vessels", True),
    "8": ("tankers", True),
    "9": ("pleasure craft", False),
}
_DARK_MAP_RESOURCE_HINTS = (
    # Observed in map_discovery.json as the active MarineTraffic base style.
    "mapbox-official/clmoxc5z401zg01quhmvh97xj",
    # Current MarineTraffic dark Mapbox style observed in the live DOM.
    "mapbox-official/clmowmvem022a01r76lwl43e1",
    # Static asset requests carry bd:1 when the dark base is active.
    "/bd:1",
    "dark-v",
    "dark_matter",
)

# How long the exact-DOM vessel filter waits for #shipTypeAccordion to appear
# after clicking the filter trigger, in milliseconds. The accordion is rendered
# asynchronously; 2500ms was too short and caused intermittent
# "shipTypeAccordion missing" failures. 7500ms made the exact-DOM path reliable
# in testing (once the cookie banner is out of the way). Tune here, not in the
# embedded JS.
_FILTER_ACCORDION_WAIT_MS = 5000

# The cookie consent banner is injected a few seconds *after* the map tiles
# render. dismiss_cookie_banner polls up to this long for it to appear.
_COOKIE_BANNER_WAIT_MS = 8000

# How long the exact-DOM dark-map setter waits for the dark option to appear in
# the #MapType panel after clicking the map-type button, in milliseconds. The
# panel container persists empty in the DOM and populates asynchronously, so
# this waits for the option itself, not just the container.
_MAP_TYPE_PANEL_WAIT_MS = 6000


def _legacy_set_vessel_filter_evaluate_unused(page):
    """Open MarineTraffic's vessel-type filter and uncheck everything except
    cargo and tankers. Best-effort — never raises; logs detailed status so a
    silent regression (e.g. filter UI selectors changed) is visible.
    """
    # page.evaluate's second argument is forwarded as the JS function's only
    # parameter — cleaner than f-string interpolation because we don't have
    # to double-escape JS braces. The dict is JSON-serialized by Playwright
    # and destructured by the async arrow on the JS side.
    try:
        result = page.__legacy_evaluate_disabled(
            """
            async ({dropLabels, keepLabels}) => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));

                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                };

                const textOf = (el) => {
                    if (!el) return '';
                    return ((el.textContent || '') + ' ' +
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('title') || '') + ' ' +
                            (el.getAttribute('data-tooltip') || '')).toLowerCase();
                };

                const isChecked = (input) =>
                    input.checked !== undefined
                        ? input.checked
                        : input.getAttribute('aria-checked') === 'true';

                const allVesselLabels = [...dropLabels, ...keepLabels];

                // Open the filter panel. Match exact "Filter"/"Filters" text
                // or aria-label only — broader matches hit unrelated UI.
                const triggers = [...document.querySelectorAll(
                    'button, [role="button"], a, [tabindex]'
                )].filter(el => {
                    if (!visible(el)) return false;
                    const txt = (el.textContent || '').trim().toLowerCase();
                    if (['filter', 'filters', 'vessel filter',
                         'filter vessels'].includes(txt)) return true;
                    const aria = ((el.getAttribute('aria-label') || '') + ' ' +
                                  (el.getAttribute('title') || '')).toLowerCase().trim();
                    return /^(filter[s]?|vessel filter|filter vessel[s]?)$/.test(aria);
                });

                const tried = [];
                for (const t of triggers.slice(0, 3)) {
                    try {
                        t.click();
                        await sleep(350);
                        tried.push((t.tagName + '.' +
                            (t.className || '').toString().slice(0, 40)).slice(0, 60));
                    } catch(e) {}
                }
                await sleep(150);

                const inputs = [...document.querySelectorAll(
                    'input[type="checkbox"], input[type="radio"], ' +
                    '[role="checkbox"], [role="switch"]'
                )].filter(visible);

                const found = new Set();
                const dropped = new Set();
                const kept = new Set();
                const errors = [];

                for (const input of inputs) {
                    // Walk ancestors until we hit one whose text mentions
                    // any vessel label — that's the row/label container.
                    // Stops us from picking up panel-header text that lists
                    // every category at once.
                    let labelText = '';
                    let el = input;
                    for (let i = 0; i < 5 && el; i++) {
                        const t = textOf(el);
                        if (allVesselLabels.some(l => t.includes(l))) {
                            labelText = t; break;
                        }
                        el = el.parentElement;
                    }
                    if (!labelText && input.id) {
                        const lbl = document.querySelector(
                            'label[for="' + CSS.escape(input.id) + '"]');
                        if (lbl) {
                            const t = textOf(lbl);
                            if (allVesselLabels.some(l => t.includes(l))) labelText = t;
                        }
                    }
                    if (!labelText) continue;

                    const drop = dropLabels.find(l => labelText.includes(l));
                    const keep = keepLabels.find(l => labelText.includes(l));

                    if (drop && !keep) {
                        found.add(drop);
                        if (isChecked(input)) {
                            try {
                                input.click();
                                await sleep(40);
                                if (isChecked(input) && input.id) {
                                    const lbl = document.querySelector(
                                        'label[for="' + CSS.escape(input.id) + '"]');
                                    if (lbl) { lbl.click(); await sleep(40); }
                                }
                                if (!isChecked(input)) dropped.add(drop);
                                else errors.push(drop + ': did not toggle');
                            } catch(e) {
                                errors.push(drop + ': ' + e.message);
                            }
                        }
                    } else if (keep) {
                        kept.add(keep);
                        if (!isChecked(input)) {
                            try { input.click(); } catch(e) {}
                        }
                    }
                }

                return {
                    tried_triggers: tried,
                    total_inputs: inputs.length,
                    found_types: [...found],
                    dropped_types: [...dropped],
                    kept_types: [...kept],
                    errors: errors,
                };
            }
            """,
            {"dropLabels": list(_DROP_VESSEL_LABELS),
             "keepLabels": list(_KEEP_VESSEL_LABELS)},
        )
    except Exception as e:
        logger.warning("  Vessel filter: page.evaluate failed: %s", e)
        return False

    if not result:
        logger.warning("  Vessel filter: no result returned")
        return False

    if result.get("dropped_types"):
        logger.info(
            "  Vessel filter applied: dropped=%s kept=%s "
            "(triggers=%s, %d inputs scanned)",
            result["dropped_types"], result.get("kept_types", []),
            result.get("tried_triggers", []), result.get("total_inputs", 0),
        )
        return True

    logger.warning(
        "  Vessel filter: nothing changed — triggers=%s, found_types=%s, "
        "total_inputs=%d, errors=%s",
        result.get("tried_triggers", []),
        result.get("found_types", []),
        result.get("total_inputs", 0),
        result.get("errors", []),
    )
    return False


def _locator_is_visible(locator, timeout_ms=600):
    try:
        return locator.count() > 0 and locator.first.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _click_first_visible(page, selectors, action_name, timeout_ms=1200,
                         pause_s=0.35, required=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                loc.click(timeout=timeout_ms)
                logger.info("  %s via: %s", action_name, sel)
                time.sleep(pause_s)
                return sel
        except Exception:
            continue
    if required:
        logger.warning("  %s: no visible selector matched", action_name)
    return None


def _check_state(locator):
    try:
        return locator.is_checked(timeout=800)
    except Exception:
        try:
            return locator.get_attribute("aria-checked") == "true"
        except Exception:
            return None


def _set_checkable_by_label(page, label, desired):
    pattern = re.compile(r"\b" + re.escape(label).replace(r"\ ", r"\s+") + r"\b",
                         re.I)
    candidates = [
        page.get_by_role("checkbox", name=pattern),
        page.get_by_role("switch", name=pattern),
        page.get_by_label(pattern),
    ]

    for group in candidates:
        try:
            count = min(group.count(), 5)
        except Exception:
            count = 0
        for i in range(count):
            loc = group.nth(i)
            if not _locator_is_visible(loc):
                continue
            before = _check_state(loc)
            if before is None:
                continue
            if before != desired:
                loc.click(timeout=1200)
                time.sleep(0.15)
            after = _check_state(loc)
            return {
                "found": True,
                "changed": before != after,
                "ok": after == desired,
                "before": before,
                "after": after,
            }

    return {"found": False, "changed": False, "ok": False,
            "before": None, "after": None}


def _page_has_dark_theme(page):
    dark_words = ("dark", "night")
    for sel in ("html", "body"):
        try:
            loc = page.locator(sel).first
            values = [
                loc.get_attribute("class") or "",
                loc.get_attribute("data-theme") or "",
                loc.get_attribute("data-color-scheme") or "",
            ]
            if any(word in " ".join(values).lower() for word in dark_words):
                return True
        except Exception:
            continue
    return False


def _page_has_dark_map_resources(page):
    """Read-only probe for the dark map layer seen in map_discovery.json."""
    try:
        return page.evaluate(
            """
            (hints) => {
                const urls = [];
                try {
                    urls.push(...performance.getEntriesByType('resource')
                        .map(e => e.name || ''));
                } catch (e) {}
                for (const img of document.querySelectorAll('#map_canvas img')) {
                    urls.push(img.currentSrc || img.src || '');
                }
                const joined = urls.join('\\n').toLowerCase();
                return hints.some(h => joined.includes(h.toLowerCase()));
            }
            """,
            list(_DARK_MAP_RESOURCE_HINTS),
        )
    except Exception:
        return False


def _page_has_dark_map_dom(page):
    """Read-only probe for dark-map classes when resources are unavailable."""
    try:
        return page.evaluate(
            """
            () => Boolean(
                document.querySelector(
                    '.leaflet-control-mouseposition-dark, ' +
                    '#map_canvas .leaflet-control-mouseposition-dark'
                )
            )
            """
        )
    except Exception:
        return False


def _click_dark_map_option_after_open(page):
    """Open the MarineTraffic map type panel and select a dark base map."""
    if _click_first_visible(
        page,
        [
            "#mapButton",
            "button#mapButton",
            "button[aria-label='Map type']",
            "[aria-label='Map type']",
        ],
        "Opened map type",
        timeout_ms=1200,
        pause_s=0.45,
    ):
        direct_selectors = [
            # Stable hooks from the live MarineTraffic map-type panel (#MapType):
            # the dark base map is a MUI radio carrying the semantic color class
            # "MuiRadio-colorDarkMode"; its label is the 2nd entry in the panel.
            # The css-* classes in the full DOM path are MUI-hashed and unstable,
            # so we deliberately do not match on them.
            "#MapType label:has(.MuiRadio-colorDarkMode)",
            "label:has(.MuiRadio-colorDarkMode)",
            "#MapType label:nth-child(2)",
            "button:has-text('Dark')",
            "label:has-text('Dark')",
            "[role='button']:has-text('Dark')",
            "[role='menuitem']:has-text('Dark')",
            "[role='option']:has-text('Dark')",
            "text=/^\\s*Dark( mode| map)?\\s*$/i",
            "[aria-label*='Dark' i]",
            "[title*='Dark' i]",
            "input[value*='dark' i]",
        ]
        if _click_first_visible(
            page, direct_selectors, "Dark map option",
            timeout_ms=1000, pause_s=0.75,
        ):
            return True

        try:
            result = page.evaluate(
                """
                async () => {
                    const sleep = ms => new Promise(r => setTimeout(r, ms));
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const textOf = (el) => [
                        el.textContent || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('value') || '',
                        el.getAttribute('data-value') || '',
                    ].join(' ').toLowerCase();

                    const candidates = [
                        ...document.querySelectorAll(
                            'button, [role="button"], [role="menuitem"], ' +
                            '[role="option"], label, li, input'
                        )
                    ].filter(visible);

                    for (const el of candidates) {
                        const t = textOf(el);
                        if (!/\\b(dark|night)\\b/.test(t)) continue;
                        if (el.matches('input')) {
                            const label = el.closest('label') ||
                                document.querySelector(
                                    'label[for="' + CSS.escape(el.id || '') + '"]'
                                );
                            (label || el).click();
                        } else {
                            el.click();
                        }
                        await sleep(650);
                        return { clicked: true, text: t.slice(0, 80) };
                    }
                    return { clicked: false };
                }
                """
            )
            if result and result.get("clicked"):
                logger.info("  Dark map option via DOM scan: %s",
                            result.get("text"))
                return True
        except Exception as e:
            logger.debug("  Dark map DOM scan failed: %s", e)

    return False


def _set_marine_traffic_dark_map(page):
    """Select the dark base map by operating directly in the #MapType panel DOM.

    Mirrors the reliable exact-DOM approach used for the vessel filter. The
    map-type panel is a simplebar-virtualized scroller, so is_visible()/click
    gating misses its entries; working in-DOM avoids that. The dark base map is
    a MUI radio whose enclosing <span> carries the semantic color class
    'MuiRadio-colorDarkMode' (observed in the live DOM); we fall back to the 2nd
    label in the panel. Returns True only if the radio ends up checked. On
    failure it logs DOM counts so the selector can be refined without guessing.
    """
    try:
        result = page.evaluate(
            """
            async ({ waitMs }) => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));

                const findSpan = () => {
                    const p = document.querySelector('#MapType');
                    return p ? p.querySelector('.MuiRadio-colorDarkMode') : null;
                };

                // 'MuiRadio-colorDarkMode' sits on the radio's <span>, not the
                // <input>. The #MapType container persists in the DOM but only
                // populates its options once opened, so wait for the dark
                // *option* to appear (not just the empty panel) and click the
                // map-type opener if it isn't there yet.
                let span = findSpan();
                if (!span) {
                    const opener = document.querySelector(
                        '#mapButton, button#mapButton, ' +
                        'button[aria-label="Map type"], [aria-label="Map type"]'
                    );
                    if (opener) { opener.click(); await sleep(450); }
                    const deadline = Date.now() + waitMs;
                    while (!span && Date.now() < deadline) {
                        span = findSpan();
                        if (!span) await sleep(100);
                    }
                }

                const panel = document.querySelector('#MapType');
                if (!panel) return { ok: false, reason: 'MapType panel missing' };

                // Resolve span -> input -> enclosing label; fall back to the 2nd
                // label (observed position of the dark option) if the span is
                // absent but the panel populated.
                let input = span ? span.querySelector('input') : null;
                let label = (input && input.closest('label')) ||
                            (span && span.closest('label'));
                if (!label) {
                    const labels = panel.querySelectorAll('label');
                    if (labels.length >= 2) {
                        label = labels[1];
                        if (!input) input = label.querySelector('input');
                    }
                }
                if (!label && !input) {
                    return {
                        ok: false,
                        reason: 'dark option missing',
                        radios: panel.querySelectorAll('input[type="radio"]').length,
                        labels: panel.querySelectorAll('label').length,
                    };
                }

                if (input && input.checked) {
                    return { ok: true, already: true };
                }

                (label || input).click();
                await sleep(350);
                if (input && !input.checked) {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'checked').set;
                    setter.call(input, true);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    await sleep(200);
                }
                return {
                    ok: input ? input.checked : true,
                    already: false,
                    via_label: Boolean(label),
                };
            }
            """,
            {"waitMs": _MAP_TYPE_PANEL_WAIT_MS},
        )
    except Exception as e:
        logger.debug("  Dark map exact-DOM path failed: %s", e)
        return False

    if not result:
        return False
    if result.get("ok"):
        logger.info("  Dark map applied via exact DOM (already=%s via_label=%s)",
                    result.get("already"), result.get("via_label"))
        return True
    logger.warning(
        "  Dark map exact-DOM path failed: reason=%s radios=%s labels=%s",
        result.get("reason"), result.get("radios"), result.get("labels"),
    )
    return False


def set_dark_mode(page):
    """Enable MarineTraffic dark map style using UI clicks only."""
    if _page_has_dark_theme(page):
        logger.info("  Dark mode already active")
        return True
    if _page_has_dark_map_resources(page):
        logger.info("  Dark map layer already active")
        return True
    if _page_has_dark_map_dom(page):
        logger.info("  Dark map UI already active")
        return True

    if _set_marine_traffic_dark_map(page):
        return True

    if _click_dark_map_option_after_open(page):
        if (_page_has_dark_theme(page) or _page_has_dark_map_resources(page)
                or _page_has_dark_map_dom(page)):
            logger.info("  Dark map applied")
            return True

    direct_selectors = [
        # See _click_dark_map_option_after_open: match the dark base map via its
        # stable MUI hooks (#MapType / MuiRadio-colorDarkMode) before text.
        "#MapType label:has(.MuiRadio-colorDarkMode)",
        "label:has(.MuiRadio-colorDarkMode)",
        "#MapType label:nth-child(2)",
        "button:has-text('Dark')",
        "label:has-text('Dark')",
        "[role='button']:has-text('Dark')",
        "[role='menuitem']:has-text('Dark')",
        "[role='option']:has-text('Dark')",
        "text=/^\\s*Dark( mode| map)?\\s*$/i",
        "[aria-label*='Dark' i]",
        "[title*='Dark' i]",
    ]
    if _click_first_visible(page, direct_selectors, "Dark mode"):
        return True

    opener_selectors = [
        "#mapButton",
        "button#mapButton",
        "button[aria-label='Map type']",
        "button:has-text('Map style')",
        "button:has-text('Map Style')",
        "button:has-text('Map type')",
        "button:has-text('Layers')",
        "button:has-text('Settings')",
        "[role='button']:has-text('Map type')",
        "[role='button']:has-text('Map style')",
        "[role='button']:has-text('Layers')",
        "[aria-label*='map style' i]",
        "[aria-label*='map type' i]",
        "[aria-label*='layers' i]",
        "[aria-label*='settings' i]",
        "[title*='map style' i]",
        "[title*='map type' i]",
        "[title*='layers' i]",
        "[title*='settings' i]",
    ]
    tried = []
    for opener in opener_selectors:
        clicked = _click_first_visible(
            page, [opener], "Opened map style/menu",
            timeout_ms=700, pause_s=0.35)
        if not clicked:
            continue
        tried.append(clicked)
        if _click_first_visible(page, direct_selectors, "Dark mode",
                                timeout_ms=1000):
            return True

    if _page_has_dark_theme(page):
        logger.info("  Dark mode active after menu interaction")
        return True
    if _page_has_dark_map_resources(page):
        logger.info("  Dark map layer active after menu interaction")
        return True
    if _page_has_dark_map_dom(page):
        logger.info("  Dark map UI active after menu interaction")
        return True

    logger.info("  Dark mode control not found (openers=%s)", tried)
    return False


def _set_marine_traffic_ship_type_filter(page):
    """Use stable MarineTraffic ids from the filter DOM when available."""
    desired = {
        name: checked for name, (_label, checked) in _MT_SHIP_TYPE_CHECKBOXES.items()
    }

    try:
        result = page.evaluate(
            """
            async ({ desired, waitMs }) => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                };

                let accordion = document.querySelector('#shipTypeAccordion');
                if (!accordion) {
                    const trigger = document.querySelector(
                        '#filtersButton, button[aria-label="Vessel filters"], ' +
                        '[aria-label="Vessel filters"]'
                    );
                    if (trigger) {
                        trigger.click();
                        await sleep(450);
                    }
                    const deadline = Date.now() + waitMs;
                    while (!accordion && Date.now() < deadline) {
                        accordion = document.querySelector('#shipTypeAccordion');
                        if (!accordion) await sleep(100);
                    }
                }
                if (!accordion) {
                    return { ok: false, reason: 'shipTypeAccordion missing' };
                }

                const summary = accordion.querySelector('button[aria-expanded]');
                if (summary && summary.getAttribute('aria-expanded') === 'false') {
                    summary.click();
                    await sleep(300);
                }

                const setter =
                    Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'checked'
                    ).set;
                const changed = [];
                const states = {};
                const missing = [];
                const errors = [];

                async function setInput(input, want) {
                    if (input.checked === want) return false;
                    const targets = [
                        input,
                        input.parentElement,
                        input.closest('label'),
                        input.closest('button'),
                    ].filter(Boolean);
                    const uniqueTargets = [...new Set(targets)];

                    for (const target of uniqueTargets) {
                        try {
                            target.click();
                            await sleep(90);
                            if (input.checked === want) return true;
                        } catch (e) {}
                    }

                    try {
                        setter.call(input, want);
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        await sleep(90);
                    } catch (e) {
                        errors.push((input.name || '?') + ': ' + e.message);
                    }
                    return input.checked === want;
                }

                for (const [name, want] of Object.entries(desired)) {
                    const selector = 'input[type="checkbox"][name="' +
                        CSS.escape(name) + '"]';
                    const input = accordion.querySelector(selector);
                    if (!input) {
                        missing.push(name);
                        continue;
                    }
                    if (input.disabled) {
                        errors.push(name + ': disabled');
                        states[name] = input.checked;
                        continue;
                    }
                    const before = input.checked;
                    await setInput(input, want);
                    states[name] = input.checked;
                    if (before !== input.checked) changed.push(name);
                }

                const keptOk = states['7'] === true && states['8'] === true;
                const droppedOk = Object.keys(desired)
                    .filter(name => name !== '7' && name !== '8')
                    .every(name => states[name] === false);

                return {
                    ok: keptOk && droppedOk && missing.length === 0 && errors.length === 0,
                    kept_ok: keptOk,
                    dropped_ok: droppedOk,
                    changed,
                    states,
                    missing,
                    errors,
                    visible: visible(accordion),
                };
            }
            """,
            {"desired": desired, "waitMs": _FILTER_ACCORDION_WAIT_MS},
        )
    except Exception as e:
        logger.debug("  Vessel filter exact DOM path failed: %s", e)
        return False

    if not result:
        return False

    if result.get("ok"):
        kept = [
            _MT_SHIP_TYPE_CHECKBOXES[name][0]
            for name, checked in desired.items() if checked
        ]
        disabled = [
            _MT_SHIP_TYPE_CHECKBOXES[name][0]
            for name, checked in desired.items() if not checked
        ]
        logger.info(
            "  Vessel filter applied via exact DOM: kept=%s disabled=%s changed=%s",
            kept, disabled, result.get("changed", []),
        )
        return True

    logger.warning(
        "  Vessel filter exact DOM path failed: reason=%s kept_ok=%s "
        "dropped_ok=%s missing=%s errors=%s states=%s",
        result.get("reason"),
        result.get("kept_ok"),
        result.get("dropped_ok"),
        result.get("missing"),
        result.get("errors"),
        result.get("states"),
    )
    return False


def set_vessel_filter(page):
    """Keep only cargo and oil tanker/tanker vessel types using UI clicks."""
    # A consent banner that surfaced late (after the initial dismiss, e.g. during
    # dark-mode setup) overlays the filter trigger and #shipTypeAccordion and
    # produces "shipTypeAccordion missing" / "no visible selector matched". Clear
    # any straggler first; cheap no-op when it was already dismissed.
    dismiss_cookie_banner(page, timeout_ms=1500)
    if _set_marine_traffic_ship_type_filter(page):
        return True

    trigger_selectors = [
        "#filtersButton",
        "button#filtersButton",
        "button[aria-label='Vessel filters']",
        "button:has-text('Filter')",
        "button:has-text('Filters')",
        "[role='button']:has-text('Filter')",
        "[role='button']:has-text('Filters')",
        "[aria-label='Filter']",
        "[aria-label='Filters']",
        "[aria-label*='vessel filter' i]",
        "[title='Filter']",
        "[title='Filters']",
    ]
    trigger = _click_first_visible(page, trigger_selectors, "Opened vessel filter",
                                   required=True)
    if not trigger:
        return False

    _click_first_visible(page, [
        "button:has-text('Vessel Type')",
        "button:has-text('Vessel type')",
        "[role='button']:has-text('Vessel Type')",
        "[role='tab']:has-text('Vessel Type')",
        "text=/^\\s*Vessel types?\\s*$/i",
    ], "Opened vessel type filter", timeout_ms=700)

    kept = []
    keep_errors = []
    for label in _KEEP_VESSEL_LABELS:
        state = _set_checkable_by_label(page, label, True)
        if state["found"] and state["ok"]:
            kept.append(label)
        elif label in ("cargo", "oil tanker"):
            keep_errors.append(label)

    found_drop = []
    disabled = []
    disable_errors = []
    for label in _DROP_VESSEL_LABELS:
        state = _set_checkable_by_label(page, label, False)
        if not state["found"]:
            continue
        found_drop.append(label)
        if state["ok"]:
            disabled.append(label)
        else:
            disable_errors.append(label)

    cargo_ok = "cargo" in kept
    tanker_ok = "oil tanker" in kept or "tanker" in kept
    ok = cargo_ok and tanker_ok and bool(found_drop) and not disable_errors
    if ok:
        logger.info("  Vessel filter applied: kept=%s disabled=%s trigger=%s",
                    kept, disabled, trigger)
        return True

    logger.warning(
        "  Vessel filter failed: kept=%s keep_errors=%s found_drop=%s "
        "disable_errors=%s trigger=%s",
        kept, keep_errors, found_drop, disable_errors, trigger,
    )
    return False


def _leaflet_tiles_ready(page):
    """True when the Leaflet base-map <img> tiles are present and all loaded.

    Reads the DOM tile state (``.leaflet-tile`` / ``.leaflet-tile-loaded``)
    instead of sampling a cross-origin-tainted ``<canvas>``. This works without
    the (un-capturable) Leaflet map object: Leaflet adds the
    ``leaflet-tile-loaded`` class to each tile ``<img>`` once its image finishes
    decoding, and strips it from tiles it is (re)loading. Returns False on any
    evaluate error so callers keep waiting.
    """
    try:
        return bool(page.evaluate("""
        () => {
            const tiles = document.querySelectorAll('.leaflet-tile');
            if (!tiles.length) return false;
            const loaded = document.querySelectorAll('.leaflet-tile-loaded').length;
            const pending = document.querySelectorAll(
                '.leaflet-tile:not(.leaflet-tile-loaded)').length;
            return loaded > 0 && pending === 0;
        }
        """))
    except Exception:
        return False


def wait_for_map_tiles(page, timeout_ms=8000):
    """Wait for the Leaflet base-map tiles to finish loading (early-exit)."""
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        if _leaflet_tiles_ready(page):
            logger.info("  Map tiles rendered")
            return
        time.sleep(0.1)
    logger.warning("  wait_for_map_tiles: timed out after %dms", timeout_ms)


def _inject_map_hooks(page):
    """Capture the Leaflet map instance without making capture depend on it."""
    page.add_init_script("""
    (function() {
        if (window.__mtMapHookInstalled) return;
        window.__mtMapHookInstalled = true;
        window.__mtMap = null;
        window.__mtMapSource = null;

        const MAP_METHODS = ['setView', 'getCenter', 'getZoom',
                             'latLngToContainerPoint', 'getContainer'];

        function isMap(obj) {
            if (!obj || typeof obj !== 'object') return false;
            for (let i = 0; i < MAP_METHODS.length; i++) {
                if (typeof obj[MAP_METHODS[i]] !== 'function') return false;
            }
            return true;
        }

        function captureMap(obj, source) {
            if (window.__mtMap || !isMap(obj)) return false;
            try {
                if (obj.options) {
                    obj.options.inertia = false;
                    obj.options.inertiaDeceleration = 99999;
                    obj.options.inertiaMaxSpeed = 0;
                }
            } catch (e) {}
            window.__mtMap = obj;
            window.__mtMapSource = source;
            return true;
        }

        function patchLeaflet(L) {
            if (!L || !L.Map || !L.Map.prototype) return;
            try {
                if (L.Map.mergeOptions) {
                    L.Map.mergeOptions({
                        inertia: false,
                        inertiaDeceleration: 99999,
                        inertiaMaxSpeed: 0,
                    });
                }
                if (!L.Map.prototype.__mtInitPatched) {
                    const origInit = L.Map.prototype.initialize;
                    L.Map.prototype.initialize = function() {
                        origInit.apply(this, arguments);
                        captureMap(this, 'L.Map.initialize');
                    };
                    L.Map.prototype.__mtInitPatched = true;
                }
            } catch (e) {}
        }

        let _L = window.L;
        if (_L) patchLeaflet(_L);
        try {
            Object.defineProperty(window, 'L', {
                get: function() { return _L; },
                set: function(v) { _L = v; patchLeaflet(v); },
                configurable: true,
            });
        } catch (e) {}

        try {
            const origBind = Function.prototype.bind;
            if (!origBind.__mtMapBindPatched) {
                const patchedBind = function() {
                    if (!window.__mtMap && arguments.length > 0) {
                        try { captureMap(arguments[0], 'Function.bind'); } catch (e) {}
                    }
                    return origBind.apply(this, arguments);
                };
                patchedBind.__mtMapBindPatched = true;
                Function.prototype.bind = patchedBind;
            }
        } catch (e) {}

        function scanForMap() {
            if (window.__mtMap) return true;
            try {
                const containers = document.querySelectorAll('#map_canvas, .leaflet-container');
                for (const c of containers) {
                    const targets = [c];
                    let p = c.parentElement;
                    for (let d = 0; d < 6 && p; d++) {
                        targets.push(p);
                        p = p.parentElement;
                    }
                    for (const child of c.querySelectorAll('*')) targets.push(child);

                    for (const el of targets) {
                        for (const k of Object.getOwnPropertyNames(el)) {
                            try {
                                if (captureMap(el[k], 'dom:' + k)) return true;
                            } catch (e) {}
                        }
                        for (const s of Object.getOwnPropertySymbols(el)) {
                            try {
                                if (captureMap(el[s], 'dom-sym:' + s.toString())) return true;
                            } catch (e) {}
                        }
                    }
                }
            } catch (e) {}
            return false;
        }
        window.__mtScanForMap = scanForMap;

        function installObserver() {
            try {
                const mo = new MutationObserver(function() {
                    if (window.__mtMap) {
                        mo.disconnect();
                        return;
                    }
                    if (document.querySelector('.leaflet-pane, .leaflet-container')) {
                        if (scanForMap()) mo.disconnect();
                    }
                });
                mo.observe(document.documentElement, { childList: true, subtree: true });
            } catch (e) {}
        }
        if (document.documentElement) {
            installObserver();
        } else {
            document.addEventListener('DOMContentLoaded', installObserver, { once: true });
        }
    })();
    """)


# --- Cloudflare handling ------------------------------------------------------


def _is_cloudflare_blocked(page) -> bool:
    try:
        title = page.title().lower()
        if "blocked" in title or "attention required" in title:
            return True
        body = page.text_content("body") or ""
        if "sorry, you have been blocked" in body.lower():
            return True
    except Exception:
        pass
    return False


def _wait_for_cloudflare(page, timeout_s: int = 15) -> bool:
    """Wait for a Cloudflare JS challenge to resolve."""
    try:
        title = page.title().lower()
        body_text = (page.text_content("body") or "").lower()
    except Exception:
        return False

    is_challenge = (
        "just a moment" in title
        or "checking your browser" in body_text
        or "cloudflare" in title
    )
    if not is_challenge:
        return False

    logger.info("  Cloudflare challenge detected, waiting...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            title = page.title().lower()
            if "just a moment" not in title and "cloudflare" not in title:
                return True
        except Exception:
            break
    return False


# --- Smart AIS marker detection -----------------------------------------------


def _wait_for_ais_markers(page, vessel_state=None, since_ts=None,
                          timeout_ms=None):
    """Wait for vessel data to arrive after a pan, via network quiescence.

    The Leaflet map object is not injectable and the marker canvas is
    cross-origin tainted, so there is no reliable DOM signal that markers
    finished rendering. Instead we observe the network: MarineTraffic fetches
    vessel positions per map tile from ``…/getData/get_data_json_4/…`` whenever
    the viewport changes. ``vessel_state`` is a per-page dict updated by a
    ``page.on("response")`` handler (registered in the worker) holding
    ``{"last_ts": perf_counter, "count": int}``.

    Strategy: wait until at least one vessel response has arrived *since*
    ``since_ts`` (the moment the pan was issued) and the endpoint has then been
    quiet for ``AIS_QUIET_MS``; finally add a short ``AIS_RENDER_SETTLE_MS`` so
    the markers are painted before the screenshot. ``AIS_WAIT_MS`` caps the wait
    so genuinely-empty areas (no requests) don't hang.

    IMPORTANT: the poll uses ``page.wait_for_timeout`` rather than
    ``time.sleep`` — only the former pumps the Playwright event loop, so the
    ``response`` events actually fire on this (sync) thread.

    Returns True if vessel responses were observed, False on cap-timeout.
    """
    timeout_ms = AIS_WAIT_MS if timeout_ms is None else timeout_ms

    if vessel_state is None:
        # No network tracking (e.g. legacy region path): fall back to a short
        # fixed settle rather than the old broken canvas probe.
        page.wait_for_timeout(min(timeout_ms, 800))
        return False

    since_ts = time.perf_counter() if since_ts is None else since_ts
    start = time.perf_counter()
    deadline = start + timeout_ms / 1000.0
    quiet_s = AIS_QUIET_MS / 1000.0
    grace_s = AIS_FIRST_RESPONSE_GRACE_MS / 1000.0
    saw_response = False

    while time.perf_counter() < deadline:
        page.wait_for_timeout(50)  # pumps event loop so on("response") fires
        last_ts = vessel_state.get("last_ts", 0.0)
        if last_ts >= since_ts:
            saw_response = True
            if (time.perf_counter() - last_ts) >= quiet_s:
                break
        elif (time.perf_counter() - start) >= grace_s:
            # No vessel request fired for this pan within the grace window —
            # the area's data is cached or simply empty; stop waiting.
            break

    if saw_response and AIS_RENDER_SETTLE_MS:
        page.wait_for_timeout(AIS_RENDER_SETTLE_MS)
    return saw_response


# --- Map panning (mouse-drag + url-load) -------------------------------------


def _get_map_dimensions(page):
    dims = page.evaluate("""
    () => {
        const mc = document.getElementById('map_canvas');
        if (!mc) return null;
        const rect = mc.getBoundingClientRect();
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    }
    """)
    if dims and dims["width"] > 0 and dims["height"] > 0:
        return dims
    return {"x": 0, "y": 0, "width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}


def _get_map_center_offset(page):
    """Query the actual pixel position of the map center within #map_canvas.

    Leaflet's setView() centres the map inside its own container, which may be
    offset from #map_canvas due to UI chrome (sidebars, headers).  Playwright's
    element.screenshot() captures at *device-pixel* resolution, which can
    differ from the CSS-pixel dimensions returned by getBoundingClientRect().
    This helper returns the map-centre pixel in CSS coords plus the device
    pixel ratio so callers can convert to image-pixel space.

    Returns ``{"center_x": float, "center_y": float, "map_lat": float,
    "map_lng": float, "map_zoom": float, "dpr": float}`` or ``None`` if the
    Leaflet map instance isn't available. ``map_lat``/``map_lng``/``map_zoom``
    are the actual current map state read from Leaflet — use these (not the
    requested setView target) as the projection inputs so marker positions
    remain correct even if MarineTraffic rounds or clamps setView internally.
    """
    return page.evaluate("""
    () => {
        const map = window.__mtMap;
        if (!map) return null;
        try {
            const center = map.getCenter();
            const centerPt = map.latLngToContainerPoint(center);
            const mapEl = map.getContainer();
            const canvas = document.getElementById('map_canvas');
            if (!mapEl || !canvas) return null;
            const mapRect = mapEl.getBoundingClientRect();
            const canvasRect = canvas.getBoundingClientRect();
            return {
                center_x: centerPt.x + (mapRect.x - canvasRect.x),
                center_y: centerPt.y + (mapRect.y - canvasRect.y),
                map_lat: center.lat,
                map_lng: center.lng,
                map_zoom: map.getZoom(),
                dpr: window.devicePixelRatio || 1,
                source: window.__mtMapSource || null
            };
        } catch (e) {
            return null;
        }
    }
    """)


def _plan_navigation(cur_lat, cur_lon, target_lat, target_lon, zoom):
    """Decide how to move the viewport from one center to another.

    Pure function (no page I/O) so the mouse-drag vs. url-load decision is
    unit-testable. Returns a dict::

        {"mode": "mouse-drag" | "url-load",
         "drag_x": float, "drag_y": float,   # pixel vector to drag the map
         "steps_needed": int}                # mouse-drag steps that *would* run

    ``mode`` is ``url-load`` when dragging would need more than
    ``URL_NAV_MAX_DRAG_STEPS`` steps (a far jump); otherwise ``mouse-drag``.
    ``steps_needed == 0`` means the target is already centered (no move).
    """
    total_pixels = 256 * (2 ** zoom)
    dx = (target_lon - cur_lon) * total_pixels / 360.0
    dy = lat_to_pixel_y(target_lat, zoom) - lat_to_pixel_y(cur_lat, zoom)
    drag_x = -dx
    drag_y = -dy

    if abs(drag_x) < 1 and abs(drag_y) < 1:
        return {
            "mode": "mouse-drag",
            "drag_x": drag_x,
            "drag_y": drag_y,
            "steps_needed": 0,
        }

    steps_needed = max(
        1,
        int(abs(drag_x) / MAX_DRAG_PX) + (1 if abs(drag_x) % MAX_DRAG_PX > 0 else 0),
        int(abs(drag_y) / MAX_DRAG_PX) + (1 if abs(drag_y) % MAX_DRAG_PX > 0 else 0),
    )
    mode = "url-load" if steps_needed > URL_NAV_MAX_DRAG_STEPS else "mouse-drag"
    return {
        "mode": mode,
        "drag_x": drag_x,
        "drag_y": drag_y,
        "steps_needed": steps_needed,
    }


def _pan_map(page, cur_lat, cur_lon, target_lat, target_lon, zoom,
             map_center=None, region_name=None,
             timestamp_str=None, worker_id=None, timings=None,
             vessel_state=None):
    """Move the map viewport to ``(target_lat, target_lon)``.

    Production capture uses two navigation modes (Leaflet ``setView`` JS
    injection was removed — MarineTraffic bundles the map object inside a
    closure, so it is not reliably reachable):

      * ``mouse-drag`` — near viewports: drag the Leaflet map in place.
      * ``url-load``   — far viewports: reload the page centered on the target.
        Dragging a sparse, far-apart tile would otherwise need hundreds of 8K
        mouse-drag steps (the dominant per-tile cost in earlier runs). After the
        reload the same setup as the initial batch load is reapplied (Cloudflare,
        map render, dark map, vessel filter, capture CSS).

    The mode is chosen by :func:`_plan_navigation`, gated on
    ``URL_NAV_MAX_DRAG_STEPS``.

    ``vessel_state`` is the per-page network-tracking dict (see
    :func:`_wait_for_ais_markers`); when provided, the AIS wait keys off the
    vessel-data responses triggered by this navigation instead of a blind sleep.

    Returns the navigation mode used: ``"mouse-drag"`` or ``"url-load"``.
    """

    timings = timings if timings is not None else {}
    nav_started = time.perf_counter()

    plan = _plan_navigation(cur_lat, cur_lon, target_lat, target_lon, zoom)
    drag_x = plan["drag_x"]
    drag_y = plan["drag_y"]
    steps_needed = plan["steps_needed"]

    # Already centered — nothing to move.
    if steps_needed == 0:
        timings["tiles_wait_s"] = 0.0
        timings["ais_wait_s"] = 0.0
        timings["nav_total_s"] = time.perf_counter() - nav_started
        return "mouse-drag"

    if plan["mode"] == "url-load":
        logger.info(
            "  Navigating via url-load: to=%s dx=%.0f dy=%.0f steps_would_be=%d "
            "threshold=%d target=(%.5f, %.5f) z%d",
            region_name or "?", drag_x, drag_y, steps_needed,
            URL_NAV_MAX_DRAG_STEPS, target_lat, target_lon, zoom,
        )
        page.goto(
            build_url(target_lat, target_lon, zoom),
            wait_until="domcontentloaded",
        )
        if not _wait_for_cloudflare(page) and _is_cloudflare_blocked(page):
            raise RuntimeError("url-load cloudflare block")
        filter_ok = _prepare_map_after_url_navigation(page, region_name or "url-load")
        if not filter_ok:
            # Hard-fail rather than capture with the wrong (all-types) filter:
            # a bad row is worse than a missing one. The caller logs the tile as
            # an error and moves on.
            raise RuntimeError(
                f"url-load setup failed: vessel filter unavailable ({region_name})"
            )
        wait_started = time.perf_counter()
        tiles_ready = _wait_for_tiles_after_pan(page)
        timings["tiles_wait_s"] = time.perf_counter() - wait_started
        timings["tiles_ready"] = bool(tiles_ready)
        wait_started = time.perf_counter()
        ais_ready = _wait_for_ais_markers(
            page, vessel_state=vessel_state, since_ts=nav_started)
        timings["ais_wait_s"] = time.perf_counter() - wait_started
        timings["ais_ready"] = bool(ais_ready)
        timings["nav_total_s"] = time.perf_counter() - nav_started
        return "url-load"

    # Near viewport — drag the map in place.
    if map_center:
        cx, cy = map_center
    else:
        cx = VIEWPORT_WIDTH // 2
        cy = VIEWPORT_HEIGHT // 2

    step_dx = drag_x / steps_needed
    step_dy = drag_y / steps_needed

    for _ in range(steps_needed):
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + step_dx, cy + step_dy, steps=MOUSE_DRAG_STEPS)
        page.mouse.up()
        if steps_needed > 1:
            time.sleep(0.05)

    logger.info(
        "  Panned via mouse-drag: to=%s dx=%.0f dy=%.0f steps=%d threshold=%d",
        region_name or "?", drag_x, drag_y, steps_needed, URL_NAV_MAX_DRAG_STEPS,
    )
    time.sleep(0.05)
    wait_started = time.perf_counter()
    tiles_ready = _wait_for_tiles_after_pan(page)
    timings["tiles_wait_s"] = time.perf_counter() - wait_started
    timings["tiles_ready"] = bool(tiles_ready)

    wait_started = time.perf_counter()
    ais_ready = _wait_for_ais_markers(
        page, vessel_state=vessel_state, since_ts=nav_started)
    timings["ais_wait_s"] = time.perf_counter() - wait_started
    timings["ais_ready"] = bool(ais_ready)
    timings["nav_total_s"] = time.perf_counter() - nav_started
    return "mouse-drag"


def _wait_for_tiles_after_pan(page, timeout_ms=None):
    """Wait for Leaflet base-map tiles to re-render after a pan (early-exit).

    Uses the DOM tile-loaded state via :func:`_leaflet_tiles_ready`. A short
    initial settle lets Leaflet create the new (pending) tile elements before we
    test for "no pending tiles", otherwise we could see the still-loaded *old*
    tiles and return prematurely.
    """
    if timeout_ms is None:
        timeout_ms = TILES_WAIT_MS
    deadline = time.time() + (timeout_ms / 1000)
    time.sleep(0.15)
    while time.time() < deadline:
        if _leaflet_tiles_ready(page):
            return True
        time.sleep(0.05)
    return False


# --- Inline ship detection ----------------------------------------------------


def _detect_ships_inline(img_bytes, center_lat, center_lon, zoom,
                         viewport_w, viewport_h, center_offset=None):
    """Run OpenCV ship detection in-memory.

    Returns (counts_dict, markers_list, image_shape):
      - counts: {"stationary_tankers": int, "moving_tankers": int,
                 "stationary_cargos": int, "moving_cargos": int}
      - markers: [{"lat": float, "lon": float, "type": str, "motion": str}, ...]
      - image_shape: (height, width) of the decoded screenshot.
    """
    from seer import detect_ships_from_bytes
    return detect_ships_from_bytes(img_bytes, center_lat, center_lon, zoom,
                                   viewport_w, viewport_h,
                                   center_offset=center_offset)


def _filter_markers_to_polygon(markers, polygon):
    """Keep only markers whose lat/lon falls inside the region polygon."""
    filtered = [m for m in markers
                if _point_in_polygon(m["lat"], m["lon"], polygon)]
    return _count_markers_by_type(filtered), filtered


def _count_markers_by_type(markers):
    """Return seer-compatible counts for an already filtered marker list."""
    counts = {
        "stationary_tankers": 0, "moving_tankers": 0,
        "stationary_cargos": 0, "moving_cargos": 0,
    }
    for m in markers:
        key = ("moving_" if m["motion"] == "moving" else "stationary_") + \
              ("tankers" if m["type"] == "tanker" else "cargos")
        counts[key] += 1
    return counts


def _parse_mouseposition_text(text):
    """Parse MarineTraffic mouseposition text into (lat, lon)."""
    if not text:
        return None
    raw = str(text).replace("\xa0", " ").strip()
    paren = re.search(
        r"\(\s*([+-]?\d+(?:[.,]\d+)?)\s*,\s*([+-]?\d+(?:[.,]\d+)?)\s*\)",
        raw,
    )
    if paren:
        return (
            float(paren.group(1).replace(",", ".")),
            float(paren.group(2).replace(",", ".")),
        )

    for line in reversed(raw.splitlines()):
        if "," not in line or "°" in line:
            continue
        match = re.search(
            r"([+-]?\d+(?:[.,]\d+)?)\s*,\s*([+-]?\d+(?:[.,]\d+)?)",
            line,
        )
        if match:
            return (
                float(match.group(1).replace(",", ".")),
                float(match.group(2).replace(",", ".")),
            )
    return None


def _haversine_m(lat1, lon1, lat2, lon2):
    radius_m = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _projection_delta(req_lat, req_lon, obs_lat, obs_lon, zoom):
    req_x = lon_to_pixel_x(req_lon, zoom)
    req_y = lat_to_pixel_y(req_lat, zoom)
    obs_x = lon_to_pixel_x(obs_lon, zoom)
    obs_y = lat_to_pixel_y(obs_lat, zoom)
    return {
        "delta_lat": obs_lat - req_lat,
        "delta_lon": obs_lon - req_lon,
        "dx_px": obs_x - req_x,
        "dy_px": obs_y - req_y,
        "meters": _haversine_m(req_lat, req_lon, obs_lat, obs_lon),
    }


def _read_mouseposition_dom(page):
    """Read the mouseposition control from the top frame or child frames."""
    script = """
    () => {
        const selectors = [
            '#map_canvas > div.leaflet-control-container > ' +
                'div.leaflet-bottom.leaflet-right > ' +
                'div.leaflet-control.leaflet-control-mouseposition-dark',
            '#map_canvas .leaflet-control.leaflet-control-mouseposition-dark',
            '.leaflet-control.leaflet-control-mouseposition-dark',
            '.leaflet-control-mouseposition-dark',
            '.leaflet-control-mouseposition.leaflet-control',
            '.leaflet-control-mouseposition'
        ];
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (!el) continue;
            const rect = el.getBoundingClientRect();
            return {
                selector,
                raw: el.innerText || el.textContent || '',
                visible: rect.width > 0 && rect.height > 0,
                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                href: location.href,
                origin: location.origin
            };
        }
        return null;
    }
    """
    frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
    for frame in frames:
        try:
            result = frame.evaluate(script)
        except Exception:
            continue
        if result:
            parsed = _parse_mouseposition_text(result.get("raw"))
            result["parsed_lat"] = parsed[0] if parsed else None
            result["parsed_lon"] = parsed[1] if parsed else None
            result["frame_url"] = getattr(frame, "url", None)
            return result
    return None


def _mouseposition_center_offset(map_width, map_height, obs_lat, obs_lon, zoom):
    return {
        "center_x": map_width / 2,
        "center_y": map_height / 2,
        "map_lat": obs_lat,
        "map_lng": obs_lon,
        "map_zoom": zoom,
        "dpr": 1.0,
        "source": "mouseposition-dom",
    }


def _read_mouseposition_anchor(
    page,
    map_dims,
    req_lat,
    req_lon,
    zoom,
    region_name,
    tile_id=None,
    timeout_ms=1200,
):
    """Move mouse to map center, read DOM lat/lon, then move away."""
    map_width = float(map_dims["width"])
    map_height = float(map_dims["height"])
    center_x = float(map_dims["x"]) + map_width / 2
    center_y = float(map_dims["y"]) + map_height / 2
    deadline = time.time() + timeout_ms / 1000
    last = None

    page.mouse.move(center_x, center_y)
    time.sleep(0.08)
    while time.time() < deadline:
        last = _read_mouseposition_dom(page)
        if last and last.get("parsed_lat") is not None and last.get("parsed_lon") is not None:
            break
        time.sleep(0.08)

    safe_x = max(1, int(float(map_dims["x"]) + 4))
    safe_y = max(1, int(float(map_dims["y"]) + 4))
    try:
        page.mouse.move(safe_x, safe_y)
    except Exception:
        pass
    hide_hover_artifacts(page)

    if not last or last.get("parsed_lat") is None or last.get("parsed_lon") is None:
        return {
            "available": False,
            "source": "mouseposition-dom",
            "reason": "mouseposition_unavailable",
            "raw": last.get("raw") if last else None,
            "selector": last.get("selector") if last else None,
        }

    obs_lat = float(last["parsed_lat"])
    obs_lon = float(last["parsed_lon"])
    delta = _projection_delta(req_lat, req_lon, obs_lat, obs_lon, zoom)
    return {
        "available": True,
        "source": "mouseposition-dom",
        "reason": "ok",
        "selector": last.get("selector"),
        "raw": last.get("raw"),
        "frame_url": last.get("frame_url"),
        "href": last.get("href"),
        "origin": last.get("origin"),
        "visible": last.get("visible"),
        "tile_id": tile_id,
        "req_lat": req_lat,
        "req_lon": req_lon,
        "obs_lat": obs_lat,
        "obs_lon": obs_lon,
        "delta_lat": delta["delta_lat"],
        "delta_lon": delta["delta_lon"],
        "dx_px": delta["dx_px"],
        "dy_px": delta["dy_px"],
        "meters": delta["meters"],
        "center_x": map_width / 2,
        "center_y": map_height / 2,
        "dpr": 1.0,
    }


def _counts_compact(counts):
    tankers = int(counts.get("stationary_tankers", 0)) + int(counts.get("moving_tankers", 0))
    cargos = int(counts.get("stationary_cargos", 0)) + int(counts.get("moving_cargos", 0))
    return {
        "total": tankers + cargos,
        "tankers": tankers,
        "cargos": cargos,
        "moving_tankers": int(counts.get("moving_tankers", 0)),
        "moving_cargos": int(counts.get("moving_cargos", 0)),
    }


def _add_counts(target, counts):
    for key, value in _counts_compact(counts).items():
        target[key] = target.get(key, 0) + value


def _nav_mode_summary(nav_counts):
    modes = [m for m in ("mouse-drag", "url-load") if nav_counts.get(m, 0)]
    if len(modes) > 1:
        return "mixed"
    if modes:
        return modes[0]
    return "mouse-drag"


def _projection_mode_summary(tile_detections):
    sources = {
        (tile.get("proj") or {}).get("source")
        for tile in tile_detections
        if (tile.get("proj") or {}).get("source")
    }
    if not sources:
        return "unknown"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _write_json_artifact(kind, timestamp_str, region_name, payload):
    out_dir = Path("./data") / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{timestamp_str}_{region_name}.json"
    payload["path"] = str(path)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return str(path)


def _select_qa_tiles(tiles, region_name, zoom):
    if not ENABLE_CROSS_ZOOM_QA or not tiles:
        return []
    scored = []
    for tile in tiles:
        row, col, lat, lon, tid = _unpack_tile(tile, region_name, zoom)
        digest = hashlib.sha1(tid.encode("utf-8")).hexdigest()
        score = int(digest[:12], 16) / float(0xFFFFFFFFFFFF)
        scored.append((score, (row, col, lat, lon, tid)))
    selected = [tile for score, tile in scored if score <= QA_SAMPLE_RATE]
    if len(selected) < QA_MIN_SAMPLES:
        selected = [tile for _, tile in sorted(scored)[:QA_MIN_SAMPLES]]
    if QA_MAX_SAMPLES > 0:
        selected = selected[:QA_MAX_SAMPLES]
    return selected


def _qa_subtile_centers(center_lat, center_lon, base_zoom, qa_zoom, map_width, map_height):
    center_x = lon_to_pixel_x(center_lon, qa_zoom)
    center_y = lat_to_pixel_y(center_lat, qa_zoom)
    offsets = [
        (-map_width / 2, -map_height / 2),
        (map_width / 2, -map_height / 2),
        (map_width / 2, map_height / 2),
        (-map_width / 2, map_height / 2),
    ]
    subtiles = []
    for idx, (dx, dy) in enumerate(offsets):
        subtiles.append({
            "idx": idx,
            "lat": pixel_y_to_lat(center_y + dy, qa_zoom),
            "lon": pixel_x_to_lon(center_x + dx, qa_zoom),
        })
    return subtiles


def _prepare_map_after_url_navigation(page, region_name):
    wait_for_map_tiles(page)
    dismiss_cookie_banner(page)
    set_dark_mode(page)
    filter_ok = set_vessel_filter(page)
    hide_ui_overlays(page)
    install_capture_visibility_css(page)
    if not filter_ok:
        logger.warning("Region %s QA: vessel filter unavailable after zoom reload", region_name)
    return filter_ok


def _run_cross_zoom_qa(
    page,
    region_name,
    config,
    timestamp_str,
    tiles,
    baseline_by_tile_id,
    map_dims,
    nav_mode_used,
    projection_mode,
):
    """Recapture sampled baseline tiles at zoom+1 and write a QA artifact."""
    zoom = int(config["zoom"])
    qa_zoom = zoom + 1
    nav_counts = {"mouse-drag": 0, "url-load": 0}
    samples = []
    qa_flags = []

    payload = {
        "enabled": ENABLE_CROSS_ZOOM_QA,
        "region": region_name,
        "region_name": config.get("name", region_name),
        "timestamp": timestamp_str,
        "nav_mode": nav_mode_used,
        "projection_mode": projection_mode,
        "zoom_used": zoom,
        "qa_zoom": qa_zoom,
        "sample_rate": QA_SAMPLE_RATE,
        "thresholds": {
            "total_ratio": QA_TOTAL_RATIO_THRESHOLD,
            "type_ratio": QA_TYPE_RATIO_THRESHOLD,
            "abs_delta": QA_ABS_DELTA_THRESHOLD,
        },
        "samples": samples,
        "qa_flags": qa_flags,
        "qa_confidence": 1.0,
    }

    selected = _select_qa_tiles(tiles, region_name, zoom)
    payload["selected_tiles"] = [t[4] for t in selected]
    if not ENABLE_CROSS_ZOOM_QA:
        payload["reason"] = "ENABLE_CROSS_ZOOM_QA=0"
        payload["path"] = _write_json_artifact("qa", timestamp_str, region_name, payload)
        return payload
    if not selected:
        payload["reason"] = "no_tiles_selected"
        payload["path"] = _write_json_artifact("qa", timestamp_str, region_name, payload)
        return payload

    map_width = int(map_dims["width"])
    map_height = int(map_dims["height"])
    map_cx = int(map_dims["x"]) + map_width // 2
    map_cy = int(map_dims["y"]) + map_height // 2
    map_locator = page.locator("#map_canvas")

    for row, col, lat, lon, tid in selected:
        baseline = baseline_by_tile_id.get(tid, {})
        baseline_counts = {
            "total": int(baseline.get("tankers", 0)) + int(baseline.get("cargos", 0)),
            "tankers": int(baseline.get("tankers", 0)),
            "cargos": int(baseline.get("cargos", 0)),
            "moving_tankers": int(baseline.get("moving_tankers", 0)),
            "moving_cargos": int(baseline.get("moving_cargos", 0)),
        }
        high_counts = {
            "total": 0,
            "tankers": 0,
            "cargos": 0,
            "moving_tankers": 0,
            "moving_cargos": 0,
        }
        sample_flags = []
        subtile_results = []
        subtiles = _qa_subtile_centers(
            lat, lon, zoom, qa_zoom, map_width, map_height
        )

        try:
            first = subtiles[0]
            page.goto(build_url(first["lat"], first["lon"], qa_zoom), wait_until="domcontentloaded")
            _prepare_map_after_url_navigation(page, region_name)
            cur_lat, cur_lon = first["lat"], first["lon"]

            for subtile in subtiles:
                if subtile["idx"] > 0:
                    nav_mode = _pan_map(
                        page,
                        cur_lat,
                        cur_lon,
                        subtile["lat"],
                        subtile["lon"],
                        qa_zoom,
                        map_center=(map_cx, map_cy),
                        region_name=region_name,
                        timestamp_str=timestamp_str,
                    )
                    nav_counts[nav_mode] = nav_counts.get(nav_mode, 0) + 1
                    cur_lat, cur_lon = subtile["lat"], subtile["lon"]

                screenshot_args = {"type": SCREENSHOT_FORMAT}
                if SCREENSHOT_FORMAT == "jpeg":
                    screenshot_args["quality"] = SCREENSHOT_QUALITY
                img_bytes = map_locator.screenshot(**screenshot_args)
                det, _markers, _shape = _detect_ships_inline(
                    img_bytes,
                    subtile["lat"],
                    subtile["lon"],
                    qa_zoom,
                    map_width,
                    map_height,
                    center_offset=None,
                )
                compact = _counts_compact(det)
                _add_counts(high_counts, det)
                subtile_results.append({
                    "idx": subtile["idx"],
                    "center_lat": subtile["lat"],
                    "center_lon": subtile["lon"],
                    "counts": compact,
                })
        except Exception as exc:
            sample_flags.append("qa_capture_failed")
            if "qa_capture_failed" not in qa_flags:
                qa_flags.append("qa_capture_failed")
            subtile_results.append({"error": str(exc)})

        metrics = {}
        for key, ratio_threshold in (
            ("total", QA_TOTAL_RATIO_THRESHOLD),
            ("tankers", QA_TYPE_RATIO_THRESHOLD),
            ("cargos", QA_TYPE_RATIO_THRESHOLD),
        ):
            base = baseline_counts[key]
            high = high_counts[key]
            delta = high - base
            ratio = high / max(base, 1)
            metrics[key] = {
                "baseline": base,
                "zoom_plus_1": high,
                "delta": delta,
                "ratio": round(ratio, 3),
            }
            if delta >= QA_ABS_DELTA_THRESHOLD and ratio >= ratio_threshold:
                flag = f"under_resolved_{key}"
                sample_flags.append(flag)
                if flag not in qa_flags:
                    qa_flags.append(flag)

        samples.append({
            "tile_id": tid,
            "tile": [row, col],
            "baseline_center": {"lat": lat, "lon": lon},
            "baseline_counts": baseline_counts,
            "zoom_plus_1_counts": high_counts,
            "metrics": metrics,
            "flags": sample_flags,
            "subtiles": subtile_results,
        })

    flagged_samples = sum(1 for sample in samples if sample["flags"])
    payload["qa_flags"] = qa_flags
    payload["qa_confidence"] = round(
        max(0.0, 1.0 - flagged_samples / max(len(samples), 1)),
        3,
    )
    payload["decision"] = {
        "under_resolved": any(flag.startswith("under_resolved") for flag in qa_flags),
        "rerun_higher_zoom": any(flag.startswith("under_resolved") for flag in qa_flags),
        "low_confidence_tag": payload["qa_confidence"] < 0.75,
    }
    payload["qa_nav_mode"] = _nav_mode_summary(nav_counts)
    payload["qa_nav_counts"] = nav_counts
    payload["path"] = _write_json_artifact("qa", timestamp_str, region_name, payload)
    logger.info(
        "Region %s QA: path=%s confidence=%.3f flags=%s",
        region_name,
        payload["path"],
        payload["qa_confidence"],
        qa_flags or [],
    )
    return payload


def _write_failure_qa_artifact(timestamp_str, region_name, config, reason, error):
    payload = {
        "enabled": ENABLE_CROSS_ZOOM_QA,
        "region": region_name,
        "region_name": config.get("name", region_name),
        "timestamp": timestamp_str,
        "nav_mode": None,
        "projection_mode": None,
        "zoom_used": config.get("zoom"),
        "qa_zoom": (config.get("zoom") + 1) if config.get("zoom") is not None else None,
        "samples": [],
        "qa_flags": [reason],
        "qa_confidence": 0.0,
        "decision": {
            "under_resolved": False,
            "rerun_higher_zoom": False,
            "low_confidence_tag": True,
        },
        "error": str(error),
    }
    payload["path"] = _write_json_artifact("qa", timestamp_str, region_name, payload)
    return payload


# --- Leaflet map discovery ----------------------------------------------------


def _discover_leaflet_map(page):
    """Try to find the Leaflet map instance if the init hook missed it."""
    return page.evaluate("""
    () => {
        if (window.__mtMap) return true;
        if (typeof window.__mtScanForMap === 'function'
            && window.__mtScanForMap()) {
            return true;
        }

        const methods = ['setView', 'getCenter', 'getZoom',
                         'latLngToContainerPoint', 'getContainer'];
        const isMap = (obj) => {
            if (!obj || typeof obj !== 'object') return false;
            for (const m of methods) {
                if (typeof obj[m] !== 'function') return false;
            }
            return true;
        };
        const capture = (obj, source) => {
            if (!isMap(obj)) return false;
            try {
                if (obj.options) {
                    obj.options.inertia = false;
                    obj.options.inertiaDeceleration = 99999;
                    obj.options.inertiaMaxSpeed = 0;
                }
            } catch (e) {}
            window.__mtMap = obj;
            window.__mtMapSource = source;
            return true;
        };

        const containers = document.querySelectorAll('#map_canvas, .leaflet-container');
        for (const c of containers) {
            const targets = [c];
            let p = c.parentElement;
            for (let d = 0; d < 6 && p; d++) {
                targets.push(p);
                p = p.parentElement;
            }
            for (const child of c.querySelectorAll('*')) targets.push(child);

            for (const el of targets) {
                for (const key of Object.getOwnPropertyNames(el)) {
                    try {
                        if (capture(el[key], 'discover:' + key)) return true;
                    } catch (e) {}
                }
                for (const sym of Object.getOwnPropertySymbols(el)) {
                    try {
                        if (capture(el[sym], 'discover-sym:' + sym.toString())) {
                            return true;
                        }
                    } catch (e) {}
                }
            }
        }
        return false;
    }
    """)


def _wait_for_map_center_offset(page, timeout_ms=5000):
    """Best-effort wait for a Leaflet center offset; never hard-fails."""
    deadline = time.time() + (timeout_ms / 1000)
    last_offset = None
    while time.time() < deadline:
        try:
            _discover_leaflet_map(page)
            last_offset = _get_map_center_offset(page)
            if last_offset:
                return last_offset
        except Exception:
            pass
        time.sleep(0.15)
    return last_offset


# --- Global tile capture ------------------------------------------------------


def _tile_result_counts(markers):
    counts = _count_markers_by_type(markers)
    return {
        "tankers": counts["stationary_tankers"] + counts["moving_tankers"],
        "cargos": counts["stationary_cargos"] + counts["moving_cargos"],
        "moving_tankers": counts["moving_tankers"],
        "moving_cargos": counts["moving_cargos"],
    }


def _global_tile_qa_flags():
    flags = ["qa_redesign_required"]
    if ENABLE_CROSS_ZOOM_QA:
        flags.append("cross_zoom_qa_disabled_for_global_grid")
    return flags


def _owned_markers_for_tile(tile_id, markers):
    """Apply deterministic tile ownership without spatially merging markers."""
    return GLOBAL_TILE_INDEX.filter_markers_for_tile(tile_id, markers)


def _save_tile_image(tile, timestamp_str, img_bytes):
    if not SAVE_IMAGES:
        return "", 0.0
    out_dir = Path(CAPTURES_DIR) / "tiles" / f"z{tile['zoom']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if SCREENSHOT_FORMAT == "jpeg" else "png"
    output_path = out_dir / f"{tile['tile_id']}_{timestamp_str}.{ext}"
    with open(output_path, "wb") as f:
        f.write(img_bytes)
    return str(output_path), output_path.stat().st_size / 1024


def _utc_capture_timestamp():
    """Return a UTC timestamp precise enough for per-tile capture identity."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f")


def _capture_global_tile_batch(tile_batch, timestamp_str, page, map_dims,
                               vessel_state=None, defer_log=False):
    """Capture one same-zoom batch from the global tile manifest.

    ``vessel_state`` is the per-page network-tracking dict updated by the
    worker's ``page.on("response")`` handler; it lets the per-tile AIS wait key
    off real vessel-data responses (see :func:`_wait_for_ais_markers`).

    When ``defer_log`` is True, per-tile captures are buffered instead of being
    written to ``captures_log.jsonl`` immediately; the buffer is returned in the
    batch stats so the caller can flush only the winning attempt of a retried
    batch (avoids persisting an empty capture that a re-log could not overwrite).

    Returns ``(results, batch_stats)`` where ``batch_stats`` carries per-batch
    marker totals (raw/accepted/rejected), tile ok/failed counts, the nav
    summary, and the deferred log buffer (``None`` when ``defer_log`` is False).
    """
    batch_started = time.perf_counter()
    map_width = int(map_dims["width"])
    map_height = int(map_dims["height"])
    map_cx = int(map_dims["x"]) + map_width // 2
    map_cy = int(map_dims["y"]) + map_height // 2
    map_locator = page.locator("#map_canvas")
    install_capture_visibility_css(page)

    nav_counts = {"mouse-drag": 0, "url-load": 0}
    mouseposition_stats = {"ok": 0, "fallback": 0}
    projection_fallback_logged = False
    leaflet_diag_logged = False
    results = {}
    batch_timing_totals = {}

    # Deferred-logging sink: buffer (args, kwargs) for _log_tile_json so a
    # retried batch only persists the kept attempt. None => write immediately.
    log_buffer = [] if defer_log else None

    def _emit_tile_log(*args, **kwargs):
        if log_buffer is not None:
            log_buffer.append((args, kwargs))
        else:
            _log_tile_json(*args, **kwargs)

    first = tile_batch[0]
    current_lat = first["center_lat"]
    current_lon = first["center_lon"]
    batch_zoom = int(first["zoom"])

    canvas_state = page.evaluate("""
    () => {
        const mc = document.getElementById('map_canvas');
        if (!mc) return 'canvas_missing';
        const rect = mc.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return 'canvas_zero_size';
        const overlay = document.querySelector('.leaflet-overlay-pane');
        if (!overlay) return 'overlay_pane_missing';
        return 'ok';
    }
    """)
    if canvas_state != "ok":
        logger.warning("Global tile batch z%d: map not ready - %s",
                       batch_zoom, canvas_state)

    for i, tile in enumerate(tile_batch):
        tile_started = time.perf_counter()
        timing = {
            "nav_total_s": 0.0,
            "tiles_wait_s": 0.0,
            "ais_wait_s": 0.0,
            "mouseposition_s": 0.0,
            "screenshot_s": 0.0,
            "opencv_s": 0.0,
        }
        nav_mode = "initial"
        tid = tile["tile_id"]
        row = int(tile["row"])
        col = int(tile["col"])
        zoom = int(tile["zoom"])
        lat = float(tile["center_lat"])
        lon = float(tile["center_lon"])

        if i > 0:
            nav_mode = _pan_map(
                page,
                current_lat,
                current_lon,
                lat,
                lon,
                zoom,
                map_center=(map_cx, map_cy),
                region_name=tid,
                timestamp_str=timestamp_str,
                timings=timing,
                vessel_state=vessel_state,
            )
            nav_counts[nav_mode] = nav_counts.get(nav_mode, 0) + 1
            current_lat, current_lon = lat, lon
        else:
            # First tile: vessel data was already requested during setup
            # (including the vessel-filter re-fetch). since_ts=0 means "any
            # response already counts", so this returns as soon as the endpoint
            # is quiet — immediately if the data has already settled.
            wait_started = time.perf_counter()
            _wait_for_ais_markers(page, vessel_state=vessel_state, since_ts=0.0)
            timing["ais_wait_s"] = time.perf_counter() - wait_started

        try:
            pre_capture_center_offset = None
            if LEAFLET_DIAGNOSTICS:
                pre_capture_center_offset = _wait_for_map_center_offset(
                    page, timeout_ms=250
                )

            leaflet_center_offset = None
            if LEAFLET_DIAGNOSTICS:
                try:
                    leaflet_center_offset = (
                        _get_map_center_offset(page) or pre_capture_center_offset
                    )
                except Exception:
                    leaflet_center_offset = pre_capture_center_offset
                if not leaflet_center_offset and not leaflet_diag_logged:
                    scan_path = _emit_frame_scan(
                        page, timestamp_str, tid, "center_offset_capture_unavailable"
                    )
                    logger.warning(
                        "Tile %s: Leaflet center_offset unavailable; "
                        "continuing with mouseposition/requested-center projection; "
                        "frame_scan=%s",
                        tid,
                        scan_path,
                    )
                    leaflet_diag_logged = True

            center_offset = None
            projection_source = "requested-center"
            proj_lat = lat
            proj_lon = lon
            section_started = time.perf_counter()
            mouse_anchor = _read_mouseposition_anchor(
                page, map_dims, lat, lon, zoom, tid, tile_id=tid
            )
            timing["mouseposition_s"] = time.perf_counter() - section_started
            if mouse_anchor.get("available"):
                proj_lat = mouse_anchor["obs_lat"]
                proj_lon = mouse_anchor["obs_lon"]
                center_offset = _mouseposition_center_offset(
                    map_width, map_height, proj_lat, proj_lon, zoom
                )
                projection_source = "mouseposition-dom"
                mouseposition_stats["ok"] += 1
                logger.info(
                    "  Tile %s (%d,%d): mouseposition obs=(%.6f, %.6f) "
                    "req=(%.6f, %.6f) delta=(%.6f, %.6f; %.0fm) px=(%.1f, %.1f)",
                    tid,
                    row,
                    col,
                    mouse_anchor["obs_lat"],
                    mouse_anchor["obs_lon"],
                    lat,
                    lon,
                    mouse_anchor["delta_lat"],
                    mouse_anchor["delta_lon"],
                    mouse_anchor["meters"],
                    mouse_anchor["dx_px"],
                    mouse_anchor["dy_px"],
                )
            else:
                mouseposition_stats["fallback"] += 1
                if not projection_fallback_logged:
                    logger.warning(
                        "Global tile capture: mouseposition DOM unavailable (%s); "
                        "using requested tile centers",
                        mouse_anchor.get("reason"),
                    )
                    projection_fallback_logged = True

            screenshot_args = {"type": SCREENSHOT_FORMAT}
            if SCREENSHOT_FORMAT == "jpeg":
                screenshot_args["quality"] = SCREENSHOT_QUALITY
            # Timestamp the actual image acquisition, not the browser/batch
            # startup. A 12-tile batch can span several minutes.
            tile_timestamp_str = _utc_capture_timestamp()
            section_started = time.perf_counter()
            img_bytes = map_locator.screenshot(**screenshot_args)
            timing["screenshot_s"] = time.perf_counter() - section_started

            if projection_source != "mouseposition-dom" and not projection_fallback_logged:
                logger.warning(
                    "Global tile capture: projection fallback active; "
                    "using requested tile centers"
                )
                projection_fallback_logged = True

            section_started = time.perf_counter()
            det, raw_markers, img_shape = _detect_ships_inline(
                img_bytes,
                proj_lat,
                proj_lon,
                zoom,
                map_width,
                map_height,
                center_offset=center_offset,
            )
            timing["opencv_s"] = time.perf_counter() - section_started
            accepted_markers, rejected_markers = _owned_markers_for_tile(
                tid, raw_markers
            )
            counts = _tile_result_counts(accepted_markers)
            tile_det = {
                "tile_id": tid,
                "tile": [row, col],
                "center_lat": lat,
                "center_lon": lon,
                "zoom": zoom,
                "tiling_mode": "global_web_mercator",
                "raw_markers": len(raw_markers),
                "accepted_markers": len(accepted_markers),
                "rejected_markers": rejected_markers,
                "tankers": counts["tankers"],
                "cargos": counts["cargos"],
                "moving_tankers": counts["moving_tankers"],
                "moving_cargos": counts["moving_cargos"],
                "markers": accepted_markers,
                "proj": {
                    "req_lat": lat,
                    "req_lon": lon,
                    "req_zoom": zoom,
                    "source": projection_source,
                    "obs_lat": mouse_anchor.get("obs_lat"),
                    "obs_lon": mouse_anchor.get("obs_lon"),
                    "delta_lat": mouse_anchor.get("delta_lat"),
                    "delta_lon": mouse_anchor.get("delta_lon"),
                    "dx_px": mouse_anchor.get("dx_px"),
                    "dy_px": mouse_anchor.get("dy_px"),
                    "meters": mouse_anchor.get("meters"),
                    "mouseposition_raw": mouse_anchor.get("raw"),
                    "mouseposition_selector": mouse_anchor.get("selector"),
                    "mouseposition_frame": mouse_anchor.get("frame_url"),
                    "dpr": center_offset.get("dpr") if center_offset else None,
                    "img_h": int(img_shape[0]) if img_shape else None,
                    "img_w": int(img_shape[1]) if img_shape else None,
                    "center_x": center_offset.get("center_x") if center_offset else None,
                    "center_y": center_offset.get("center_y") if center_offset else None,
                },
            }
            if leaflet_center_offset:
                from seer import _debug_center_check
                _debug_center_check(lat, lon, leaflet_center_offset, row, col, logger)
                act_zoom = leaflet_center_offset.get("map_zoom")
                if act_zoom is not None and abs(act_zoom - zoom) > 1e-6:
                    logger.warning(
                        "  Tile %s (%d,%d): setZoom drift! requested %s, actual %s",
                        tid, row, col, zoom, act_zoom,
                    )

            saved_path, file_size_kb = _save_tile_image(
                tile, tile_timestamp_str, img_bytes
            )
            nav_mode_used = _nav_mode_summary(nav_counts)
            projection_mode = _projection_mode_summary([tile_det])
            _emit_tile_log(
                tile_timestamp_str,
                tile,
                saved_path,
                "success",
                file_size_kb,
                counts,
                detections=[tile_det],
                markers=accepted_markers,
                nav_mode=nav_mode_used,
                projection_mode=projection_mode,
                qa_flags=_global_tile_qa_flags(),
                qa_confidence=None,
            )
            timing["total_s"] = time.perf_counter() - tile_started
            for key, value in timing.items():
                if key.endswith("_s") and isinstance(value, (int, float)):
                    batch_timing_totals[key] = (
                        batch_timing_totals.get(key, 0.0) + float(value)
                    )
            results[tid] = {
                **counts,
                "tile_id": tid,
                "tiles_ok": 1,
                "tiles_failed": 0,
                "detections": [tile_det],
                "nav": dict(nav_counts),
                "nav_mode": nav_mode_used,
                "projection_mode": projection_mode,
                "mouseposition": dict(mouseposition_stats),
            }
            logger.info(
                "  Tile %s (%d,%d) [%d/%d]: %d tankers (%d mov), "
                "%d cargo (%d mov), accepted=%d rejected=%d projection=%s",
                tid,
                row,
                col,
                i + 1,
                len(tile_batch),
                counts["tankers"],
                counts["moving_tankers"],
                counts["cargos"],
                counts["moving_cargos"],
                len(accepted_markers),
                rejected_markers,
                projection_source,
            )
            logger.info(
                "TIMING global_tile tile=%s z=%d step=%d/%d total=%.3fs "
                "nav=%.3fs mode=%s tiles_wait=%.3fs ais_wait=%.3fs "
                "mouseposition=%.3fs screenshot=%.3fs opencv=%.3fs "
                "raw=%d accepted=%d rejected=%d",
                tid,
                zoom,
                i + 1,
                len(tile_batch),
                timing.get("total_s", 0.0),
                timing.get("nav_total_s", 0.0),
                nav_mode,
                timing.get("tiles_wait_s", 0.0),
                timing.get("ais_wait_s", 0.0),
                timing.get("mouseposition_s", 0.0),
                timing.get("screenshot_s", 0.0),
                timing.get("opencv_s", 0.0),
                len(raw_markers),
                len(accepted_markers),
                rejected_markers,
            )
        except Exception as exc:
            timing["total_s"] = time.perf_counter() - tile_started
            for key, value in timing.items():
                if key.endswith("_s") and isinstance(value, (int, float)):
                    batch_timing_totals[key] = (
                        batch_timing_totals.get(key, 0.0) + float(value)
                    )
            logger.error("  Tile %s (%d,%d) failed: %s", tid, row, col, exc)
            logger.info(
                "TIMING global_tile tile=%s z=%d step=%d/%d total=%.3fs "
                "failed_after=%.3fs nav=%.3fs mode=%s tiles_wait=%.3fs "
                "ais_wait=%.3fs mouseposition=%.3fs screenshot=%.3fs "
                "opencv=%.3fs",
                tid,
                zoom,
                i + 1,
                len(tile_batch),
                timing.get("total_s", 0.0),
                timing.get("total_s", 0.0),
                timing.get("nav_total_s", 0.0),
                nav_mode,
                timing.get("tiles_wait_s", 0.0),
                timing.get("ais_wait_s", 0.0),
                timing.get("mouseposition_s", 0.0),
                timing.get("screenshot_s", 0.0),
                timing.get("opencv_s", 0.0),
            )
            _emit_tile_log(
                _utc_capture_timestamp(),
                tile,
                "",
                "error",
                0.0,
                {"tankers": 0, "cargos": 0, "moving_tankers": 0, "moving_cargos": 0},
                detections=[{"tile_id": tid, "tile": [row, col], "error": str(exc)}],
                markers=[],
                nav_mode=_nav_mode_summary(nav_counts),
                projection_mode="unknown",
                qa_flags=["capture_failed", *_global_tile_qa_flags()],
                qa_confidence=0.0,
            )
            results[tid] = {
                "tile_id": tid,
                "tankers": 0,
                "cargos": 0,
                "moving_tankers": 0,
                "moving_cargos": 0,
                "tiles_ok": 0,
                "tiles_failed": 1,
            }

    batch_elapsed = time.perf_counter() - batch_started
    logger.info(
        "Global tile batch z%d summary: tiles=%d nav=%s mouseposition_ok=%d "
        "mouseposition_fallback=%d",
        batch_zoom,
        len(tile_batch),
        nav_counts,
        mouseposition_stats["ok"],
        mouseposition_stats["fallback"],
    )
    logger.info(
        "TIMING global_tile_batch z=%d tiles=%d elapsed=%.3fs avg_tile=%.3fs "
        "nav=%.3fs tiles_wait=%.3fs ais_wait=%.3fs "
        "mouseposition=%.3fs screenshot=%.3fs opencv=%.3fs",
        batch_zoom,
        len(tile_batch),
        batch_elapsed,
        batch_elapsed / len(tile_batch) if tile_batch else 0.0,
        batch_timing_totals.get("nav_total_s", 0.0),
        batch_timing_totals.get("tiles_wait_s", 0.0),
        batch_timing_totals.get("ais_wait_s", 0.0),
        batch_timing_totals.get("mouseposition_s", 0.0),
        batch_timing_totals.get("screenshot_s", 0.0),
        batch_timing_totals.get("opencv_s", 0.0),
    )

    # Aggregate per-batch marker totals + tile outcomes from the per-tile
    # detections, so the worker can log a marker summary and run the
    # suspect-empty-batch heuristic without re-deriving counts.
    raw_total = accepted_total = rejected_total = 0
    ok_tiles = failed_tiles = 0
    for tile_result in results.values():
        ok_tiles += int(tile_result.get("tiles_ok", 0) or 0)
        failed_tiles += int(tile_result.get("tiles_failed", 0) or 0)
        for det in (tile_result.get("detections") or []):
            raw_total += int(det.get("raw_markers", 0) or 0)
            accepted_total += int(det.get("accepted_markers", 0) or 0)
            rejected_total += int(det.get("rejected_markers", 0) or 0)

    batch_stats = {
        "raw_total": raw_total,
        "accepted_total": accepted_total,
        "rejected_total": rejected_total,
        "ok_tiles": ok_tiles,
        "failed_tiles": failed_tiles,
        "nav_counts": dict(nav_counts),
        "log_buffer": log_buffer,
    }
    return results, batch_stats


def capture_tile_batch_worker(tile_batch, timestamp_str):
    """Process one same-zoom global tile batch in a dedicated browser.

    Lifecycle guarantee: each call opens its **own** Patchright browser,
    context, and page — ``1 worker call = 1 browser = 1 context = 1 page =
    1 batch``. Nothing is reused across batches, so a burned proxy / empty AIS
    session is contained to a single batch (and the global retry path also
    re-enters here with a fresh browser).

    When ``SUSPECT_EMPTY_RETRY`` is enabled and a batch overlapping known
    hotspots returns ~no raw markers, it is re-run (bounded) with a fresh
    browser + fresh proxy. Per-tile logging is deferred so only the best
    attempt is persisted — re-logging the same run timestamp would otherwise be
    swallowed by ``ON CONFLICT (tile_id, captured_at) DO NOTHING`` and keep the
    empty capture.
    """
    worker_id = threading.current_thread().name
    tile_batch = list(tile_batch)
    first = tile_batch[0]
    zoom = int(first["zoom"])
    batch_label = f"z{zoom}:{first['tile_id']}+{len(tile_batch) - 1}"
    # Hotspot tiles are seeded from chokepoint regions (priority>0/seed_regions)
    # — places we expect ships. Used by the suspect-empty heuristic below.
    hotspot_tiles = sum(
        1 for t in tile_batch
        if int(t.get("priority", 0) or 0) > 0 or t.get("seed_regions")
    )

    defer_log = SUSPECT_EMPTY_RETRY
    max_attempts = 1 + (SUSPECT_EMPTY_MAX_RETRIES if SUSPECT_EMPTY_RETRY else 0)

    def _run_attempt(attempt_idx, proxy, geo):
        """Run one full browser lifecycle for the batch with the given proxy."""
        attempt = {
            "results": {},
            "retryable": [],
            "raw_total": 0,
            "accepted_total": 0,
            "rejected_total": 0,
            "ok_tiles": 0,
            "failed_tiles": 0,
            "log_buffer": None,
            "setup_failed": False,
            "proxy": proxy,
            "geo": geo,
        }
        results = attempt["results"]
        retryable = attempt["retryable"]
        worker_started = time.perf_counter()
        logger.info(
            "[mode=global-tile-batch worker=%s] batch=%s attempt=%d/%d "
            "proxy=%s tz=%s tiles=%d hotspot_tiles=%d browser=starting",
            worker_id, batch_label, attempt_idx, max_attempts,
            _proxy_log_label(proxy, geo), geo.timezone_id,
            len(tile_batch), hotspot_tiles,
        )

        try:
            with sync_playwright() as p:
                _has_display = os.environ.get("DISPLAY") or sys.platform != "linux"
                chrome_args = ["--headless=new", "--disable-dev-shm-usage"]
                if _has_display:
                    chrome_args += [
                        "--enable-gpu-rasterization",
                        "--enable-zero-copy",
                        "--use-angle=default",
                    ]

                browser = p.chromium.launch(
                    headless=False,
                    channel="chrome",
                    args=chrome_args,
                )
                context = browser.new_context(
                    proxy={
                        "server": proxy["server"],
                        "username": proxy["username"],
                        "password": proxy["password"],
                    },
                    viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                    device_scale_factor=1.0,
                    timezone_id=geo.timezone_id,
                    locale=geo.locale,
                    geolocation={"latitude": geo.latitude, "longitude": geo.longitude},
                    permissions=["geolocation"],
                    extra_http_headers={"Accept-Language": geo.accept_language},
                    user_agent=_chrome_user_agent(browser.version),
                )
                logger.info(
                    "[worker=%s batch=%s attempt=%d] browser+context started "
                    "proxy=%s",
                    worker_id, batch_label, attempt_idx, proxy["server"],
                )

                page = context.new_page()
                _inject_map_hooks(page)

                # Track vessel-data network responses so the per-tile AIS wait
                # can key off real data arrival (the marker canvas is
                # cross-origin tainted and the map object is not injectable, so
                # this is the only reliable "markers loaded" signal).
                # MarineTraffic fetches vessel positions per map tile from
                # .../getData/get_data_json_4/...
                vessel_state = {"last_ts": 0.0, "count": 0}

                def _on_vessel_response(response, _state=vessel_state):
                    try:
                        if "get_data_json" in response.url.lower():
                            _state["last_ts"] = time.perf_counter()
                            _state["count"] += 1
                    except Exception:
                        pass

                page.on("response", _on_vessel_response)

                center_lat = first["center_lat"]
                center_lon = first["center_lon"]
                url = build_url(center_lat, center_lon, zoom)
                logger.info(
                    "[worker=%s batch=%s attempt=%d] loading url at zoom %d",
                    worker_id, batch_label, attempt_idx, zoom,
                )
                page.goto(url, wait_until="domcontentloaded")

                page_ready = True
                filter_state = "skipped"
                dark_state = "skipped"
                try:
                    cloudflare_passed = _wait_for_cloudflare(page)
                    if cloudflare_passed:
                        logger.info("[worker=%s batch=%s] Cloudflare passed",
                                    worker_id, batch_label)
                    elif _is_cloudflare_blocked(page):
                        raise RuntimeError("cloudflare block")

                    wait_for_map_tiles(page)
                    dismiss_cookie_banner(page)
                    dark_ok = set_dark_mode(page)
                    dark_state = "applied" if dark_ok else "unavailable"
                    filter_ok = set_vessel_filter(page)
                    filter_state = "applied" if filter_ok else "failed"
                    if not filter_ok:
                        raise RuntimeError("required setup failed: vessel filter")
                    hide_ui_overlays(page)
                    install_capture_visibility_css(page)
                    if LEAFLET_DIAGNOSTICS:
                        _discover_leaflet_map(page)
                except Exception as exc:
                    retry_setup = (
                        _is_crash_error(exc)
                        or "required setup failed" in str(exc).lower()
                        or "cloudflare block" in str(exc).lower()
                    )
                    if retry_setup:
                        logger.error(
                            "[worker=%s batch=%s] setup failed (retryable): %s",
                            worker_id, batch_label, exc,
                        )
                        retryable.extend(t["tile_id"] for t in tile_batch)
                    else:
                        logger.error(
                            "[worker=%s batch=%s] setup failed: %s",
                            worker_id, batch_label, exc,
                        )
                    page_ready = False

                if not page_ready:
                    attempt["setup_failed"] = True
                    attempt["failed_tiles"] = len(tile_batch)
                    logger.info(
                        "TIMING global_tile_worker batch=%s setup_failed "
                        "elapsed=%.3fs",
                        batch_label, time.perf_counter() - worker_started,
                    )
                    context.close()
                    browser.close()
                    logger.info(
                        "[worker=%s batch=%s attempt=%d] browser+context "
                        "closed (setup_failed)",
                        worker_id, batch_label, attempt_idx,
                    )
                    return attempt

                map_dims = _get_map_dimensions(page)
                center_offset = None
                if LEAFLET_DIAGNOSTICS:
                    center_offset = _wait_for_map_center_offset(page, timeout_ms=1000)
                    try:
                        map_probe = run_map_probe(page)
                        logger.info("%s", json.dumps({
                            "event": "map_probe",
                            "worker": worker_id,
                            "batch": batch_label,
                            "probe": map_probe,
                        }, sort_keys=True))
                    except Exception as exc:
                        logger.warning(
                            "[worker=%s batch=%s] map probe failed: %s",
                            worker_id, batch_label, exc,
                        )

                mouse_probe = _read_mouseposition_anchor(
                    page,
                    map_dims,
                    center_lat,
                    center_lon,
                    zoom,
                    first["tile_id"],
                    tile_id="worker_probe",
                )
                setup_elapsed = time.perf_counter() - worker_started
                logger.info(
                    "[worker=%s batch=%s] setup ok: nav_default=mouse-drag "
                    "map_dims=%dx%d leaflet_center_offset=%s "
                    "mouseposition=%s dark=%s filter=%s",
                    worker_id,
                    batch_label,
                    int(map_dims["width"]),
                    int(map_dims["height"]),
                    "ok" if center_offset else "missing",
                    "ok" if mouse_probe.get("available") else "missing",
                    dark_state,
                    filter_state,
                )
                logger.info(
                    "TIMING global_tile_worker batch=%s setup_ok elapsed=%.3fs",
                    batch_label, setup_elapsed,
                )

                try:
                    capture_started = time.perf_counter()
                    batch_results, batch_stats = _capture_global_tile_batch(
                        tile_batch, timestamp_str, page, map_dims,
                        vessel_state=vessel_state, defer_log=defer_log,
                    )
                    results.update(batch_results)
                    attempt["raw_total"] = batch_stats["raw_total"]
                    attempt["accepted_total"] = batch_stats["accepted_total"]
                    attempt["rejected_total"] = batch_stats["rejected_total"]
                    attempt["ok_tiles"] = batch_stats["ok_tiles"]
                    attempt["failed_tiles"] = batch_stats["failed_tiles"]
                    attempt["log_buffer"] = batch_stats["log_buffer"]
                    capture_elapsed = time.perf_counter() - capture_started
                    logger.info(
                        "TIMING global_tile_worker batch=%s finished "
                        "total=%.3fs setup=%.3fs capture_batch=%.3fs",
                        batch_label,
                        time.perf_counter() - worker_started,
                        setup_elapsed,
                        capture_elapsed,
                    )
                except Exception as exc:
                    capture_elapsed = time.perf_counter() - capture_started
                    logger.info(
                        "TIMING global_tile_worker batch=%s capture_failed "
                        "total=%.3fs setup=%.3fs capture_batch=%.3fs",
                        batch_label,
                        time.perf_counter() - worker_started,
                        setup_elapsed,
                        capture_elapsed,
                    )
                    if _is_crash_error(exc):
                        logger.error(
                            "[worker=%s batch=%s] capture failed (retryable): %s",
                            worker_id, batch_label, exc,
                        )
                        retryable.extend(t["tile_id"] for t in tile_batch)
                    else:
                        logger.error(
                            "[worker=%s batch=%s] capture failed: %s",
                            worker_id, batch_label, exc,
                        )

                context.close()
                browser.close()
                logger.info(
                    "[worker=%s batch=%s attempt=%d] browser+context closed",
                    worker_id, batch_label, attempt_idx,
                )

        except Exception as exc:
            if _is_crash_error(exc):
                logger.error(
                    "[worker=%s batch=%s] worker crashed (retryable): %s",
                    worker_id, batch_label, exc,
                )
                retryable.extend(t["tile_id"] for t in tile_batch)
            else:
                logger.error("[worker=%s batch=%s] worker failed: %s",
                             worker_id, batch_label, exc)

        return attempt

    tried_servers = set()
    best = None
    for attempt_idx in range(1, max_attempts + 1):
        proxy, geo = _pick_batch_proxy(tried_servers)
        tried_servers.add(proxy["server"])
        attempt = _run_attempt(attempt_idx, proxy, geo)

        logger.info(
            "[mode=global-tile-batch worker=%s] batch=%s attempt=%d/%d "
            "proxy=%s marker_summary raw=%d accepted=%d rejected=%d "
            "ok_tiles=%d failed_tiles=%d hotspot_tiles=%d",
            worker_id, batch_label, attempt_idx, max_attempts,
            _proxy_log_label(proxy, geo),
            attempt["raw_total"], attempt["accepted_total"],
            attempt["rejected_total"], attempt["ok_tiles"],
            attempt["failed_tiles"], hotspot_tiles,
        )

        # Suspect-empty: a successful batch over a known hotspot, or a zoom-9
        # batch in the active latitude belt, that saw almost no raw markers.
        suspect_reason = _suspect_empty_reason(
            tile_batch, hotspot_tiles, attempt
        )
        suspect = suspect_reason is not None
        attempt["suspect"] = suspect
        attempt["suspect_reason"] = suspect_reason
        if suspect:
            logger.warning("%s", json.dumps({
                "event": "suspect_empty_batch",
                "reason": suspect_reason,
                "worker": worker_id,
                "batch": batch_label,
                "attempt": attempt_idx,
                "max_attempts": max_attempts,
                "proxy_server": proxy["server"],
                "exit_ip": geo.exit_ip or "unresolved",
                "country": geo.country_code,
                "timezone": geo.timezone_id,
                "hotspot_tiles": hotspot_tiles,
                "tiles": len(tile_batch),
                "ok_tiles": attempt["ok_tiles"],
                "raw_total": attempt["raw_total"],
                "accepted_total": attempt["accepted_total"],
                "raw_threshold": SUSPECT_EMPTY_RAW_THRESHOLD,
                "zoom9_latitude_min": SUSPECT_EMPTY_ZOOM9_MIN_LAT,
                "zoom9_latitude_max": SUSPECT_EMPTY_ZOOM9_MAX_LAT,
                "retry_enabled": SUSPECT_EMPTY_RETRY,
            }, sort_keys=True))

        if best is None or attempt["raw_total"] > best["raw_total"]:
            best = attempt

        if not suspect:
            break
        if attempt_idx < max_attempts:
            logger.warning(
                "[worker=%s batch=%s] suspect_empty_batch -> retrying with "
                "fresh browser+proxy (next attempt %d/%d)",
                worker_id, batch_label, attempt_idx + 1, max_attempts,
            )

    if best is None:
        # Defensive: should not happen (loop runs at least once).
        results = {"_retryable": [t["tile_id"] for t in tile_batch]}
        return results

    if _should_requeue_exhausted_suspect(best):
        logger.warning("%s", json.dumps({
            "event": "suspect_empty_batch_exhausted",
            "worker": worker_id,
            "batch": batch_label,
            "attempts": max_attempts,
            "reason": best.get("suspect_reason"),
            "raw_total": best.get("raw_total", 0),
            "accepted_total": best.get("accepted_total", 0),
            "action": "queue_retry",
        }, sort_keys=True))
        # Deferred logging means no empty captures reached the scratch artifact.
        # The empty artifact makes worker.process_batch fail this job retryably.
        return {"_retryable": [t["tile_id"] for t in tile_batch]}

    # Flush only the winning attempt's deferred logs (retry path); when
    # deferral is off, the batch already wrote logs incrementally.
    if defer_log and best.get("log_buffer"):
        for log_args, log_kwargs in best["log_buffer"]:
            _log_tile_json(*log_args, **log_kwargs)
        logger.info(
            "[worker=%s batch=%s] flushed %d deferred tile log(s) from best "
            "attempt (raw=%d accepted=%d)",
            worker_id, batch_label, len(best["log_buffer"]),
            best["raw_total"], best["accepted_total"],
        )

    results = dict(best["results"])
    results["_retryable"] = best["retryable"]
    return results


# --- Logging ------------------------------------------------------------------

_log_lock = threading.Lock()
# Per-run raw output: each capture run writes its own
# data/raw/runs/<run_id>/captures.jsonl. Ingest is a separate step
# (update_database.py <path>); the scraper no longer ingests by default.
_RAW_RUNS_DIR = Path("./data/raw/runs")
_LATEST_RUN_POINTER = _RAW_RUNS_DIR / "LATEST"
# Active run file; set per run by capture_all_regions, read by _log_tile_json.
_capture_log_path = _RAW_RUNS_DIR / "captures.jsonl"


def _log_tile_json(timestamp, tile, filepath, status, size_kb, counts,
                   detections=None, markers=None, nav_mode=None,
                   projection_mode=None, qa_flags=None, qa_confidence=None):
    """Append one global tile capture entry to the active run's captures.jsonl."""
    _capture_log_path.parent.mkdir(parents=True, exist_ok=True)
    status = status or "success"
    ok = 1 if status == "success" else 0
    failed = 0 if status == "success" else 1
    entry = {
        "capture_type": "tile",
        "tile_id": tile["tile_id"],
        "tile": tile,
        "row": tile["row"],
        "col": tile["col"],
        "center_lat": tile["center_lat"],
        "center_lon": tile["center_lon"],
        "tile_bounds": tile["tile_bounds"],
        "capture_bounds": tile["capture_bounds"],
        "owner_bounds_px": tile["owner_bounds_px"],
        "capture_bounds_px": tile["capture_bounds_px"],
        "schedule_minutes": tile.get("schedule_minutes"),
        "priority": tile.get("priority", 0),
        "source": tile.get("source"),
        "seed_regions": tile.get("seed_regions", []),
        "filepath": filepath,
        "date_time": timestamp,
        "tiles_total": 1,
        "tiles_ok": ok,
        "tiles_failed": failed,
        "zoom": int(tile["zoom"]),
        "zoom_used": int(tile["zoom"]),
        "file_size_kb": round(size_kb, 1),
        "tankers": int(counts.get("tankers", 0)),
        "cargos": int(counts.get("cargos", 0)),
        "moving_tankers": int(counts.get("moving_tankers", 0)),
        "moving_cargos": int(counts.get("moving_cargos", 0)),
        "status": status,
        "markers": markers or [],
        "detections": detections or [],
        "nav_mode": nav_mode,
        "projection_mode": projection_mode,
        "qa_flags": qa_flags or [],
        "qa_confidence": qa_confidence,
    }

    with _log_lock:
        with open(_capture_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    logger.info(
        "_log_tile_json: tile=%s status=%s tankers=%d cargos=%d "
        "moving_t=%d moving_c=%d markers=%d",
        tile["tile_id"],
        status,
        entry["tankers"],
        entry["cargos"],
        entry["moving_tankers"],
        entry["moving_cargos"],
        len(markers or []),
    )


# --- Orchestration ------------------------------------------------------------


def _select_global_tiles(region_filter=None, zoom_filter=None, tier_filter=None,
                         tile_ids=None, respect_schedule=True):
    tiles = list(GLOBAL_TILE_MANIFEST)
    if tile_ids:
        wanted = set(tile_ids)
        tiles = [t for t in tiles if t["tile_id"] in wanted]
        missing = wanted - {t["tile_id"] for t in tiles}
        if missing:
            logger.warning("Unknown tile ids ignored: %s", sorted(missing))
    if zoom_filter:
        allowed_zoom = set(int(z) for z in zoom_filter)
        tiles = [t for t in tiles if int(t["zoom"]) in allowed_zoom]

    region_codes = set(region_filter or [])
    if tier_filter:
        allowed_tiers = set(tier_filter)
        region_codes.update(
            code for code, tier in REGION_TIERS.items() if tier in allowed_tiers
        )
    if region_codes:
        polygons = [
            ACTIVE_REGIONS[code]["polygon"]
            for code in region_codes
            if code in ACTIVE_REGIONS
        ]
        tiles = [
            t for t in tiles
            if any(tile_intersects_polygon(t, polygon) for polygon in polygons)
        ]

    # Normal global waves may omit known non-productive cells. Explicit tile
    # selection remains an override so excluded cells can still be diagnosed.
    if GLOBAL_TILE_EXCLUDE_IDS and not tile_ids:
        before = len(tiles)
        tiles = [
            t for t in tiles
            if t["tile_id"] not in GLOBAL_TILE_EXCLUDE_IDS
        ]
        logger.info(
            "Global tile exclusion filter: %d/%d retained (%d excluded)",
            len(tiles),
            before,
            before - len(tiles),
        )

    if respect_schedule and RESPECT_TILE_SCHEDULE:
        try:
            due_ids = get_due_tile_ids(t["tile_id"] for t in tiles)
            before = len(tiles)
            tiles = [t for t in tiles if t["tile_id"] in due_ids]
            logger.info(
                "Tile schedule filter: %d/%d selected tiles are due",
                len(tiles),
                before,
            )
        except Exception as exc:
            logger.warning(
                "Tile schedule filter unavailable (%s); capturing selected tiles",
                exc,
            )

    # Snake/boustrophedon order (same key as the manifest) so sequential
    # navigation makes small local hops. A plain (zoom,row,col) sort would jump
    # back across the whole row at every row boundary, which for sparse z12
    # tiles produced hundreds of mouse-drag steps per move.
    return sorted(tiles, key=tile_scan_key)


def _chunk_global_tiles(tiles):
    """Group tiles into same-zoom batches, preserving input order.

    Each worker processes one batch on a single page, so batch membership
    determines navigation locality. ``tiles`` is expected snake-ordered (see
    :func:`_select_global_tiles`); grouping by zoom and slicing in order keeps
    each batch spatially compact. The url-load fallback in :func:`_pan_map`
    absorbs the unavoidable far jumps between sparse clusters.
    """
    by_zoom = {}
    for tile in tiles:
        by_zoom.setdefault(int(tile["zoom"]), []).append(tile)

    batches = []
    batch_size = max(1, GLOBAL_TILE_BATCH_SIZE)
    for zoom in sorted(by_zoom):
        zoom_tiles = by_zoom[zoom]
        for i in range(0, len(zoom_tiles), batch_size):
            batches.append(zoom_tiles[i:i + batch_size])
    return batches


def capture_all_regions(region_filter=None, no_ingest=False,
                        zoom_filter=None, tier_filter=None, tile_ids=None,
                        ingest=False):
    """Capture the global tile manifest into a per-run raw JSONL file.

    Each run writes ``data/raw/runs/<run_id>/captures.jsonl`` and updates the
    ``data/raw/runs/LATEST`` pointer. The raw file is the deliverable; ingest
    into PostgreSQL is a separate step (``update_database.py <path>``).

    By default the run is **not** ingested. Pass ``ingest=True`` to ingest the
    just-written run inline; ``no_ingest=True`` always wins and forces it off.

    ``region_filter`` is only a debug selector. It chooses global tiles whose
    owner bounds intersect those region polygons; persisted captures remain
    tile-scoped.
    """
    global _capture_log_path

    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    logger.info("Starting global tile capture run: %s (run_id=%s)", timestamp_str, run_id)
    _capture_log_path = _RAW_RUNS_DIR / run_id / "captures.jsonl"
    logger.info("Raw capture log: %s", _capture_log_path)
    do_ingest = ingest and not no_ingest

    tiles = _select_global_tiles(
        region_filter=region_filter,
        zoom_filter=zoom_filter,
        tier_filter=tier_filter,
        tile_ids=tile_ids,
    )
    batches = _chunk_global_tiles(tiles)
    total_tiles = len(tiles)
    summary = manifest_summary(tiles)
    tile_by_id = {tile["tile_id"]: tile for tile in tiles}
    logger.info(
        "Global tile selection: %d tiles in %d batches | by_zoom=%s "
        "by_source=%s bbox=%s default_zoom=%d region_filter=%s",
        total_tiles,
        len(batches),
        summary["by_zoom"],
        summary["by_source"],
        GLOBAL_GRID_BBOX,
        GLOBAL_GRID_DEFAULT_ZOOM,
        region_filter,
    )
    if ENABLE_CROSS_ZOOM_QA:
        logger.warning(
            "Cross-zoom QA is diagnostic-only for global tile capture. "
            "Sustainable adaptive zoom/split decisions still need a QA redesign."
        )

    t_start = time.perf_counter()
    all_results = {}

    def _run_tile_batches_parallel(tile_batches):
        worker_results_all = {}
        retry_tile_ids = []
        with ThreadPoolExecutor(max_workers=MAX_BROWSERS) as executor:
            futures = {
                executor.submit(capture_tile_batch_worker, batch, timestamp_str): [
                    t["tile_id"] for t in batch
                ]
                for batch in tile_batches
            }
            for future in as_completed(futures):
                batch_ids = futures[future]
                try:
                    worker_results = future.result() or {}
                    retry_tile_ids.extend(worker_results.pop("_retryable", []))
                    worker_results_all.update(worker_results)
                except Exception as e:
                    logger.error("Worker for tile batch %s failed: %s", batch_ids, e)
                    retry_tile_ids.extend(batch_ids)
        return worker_results_all, retry_tile_ids

    results_batch, retryable = _run_tile_batches_parallel(batches)
    all_results.update(results_batch)

    for attempt in range(1, MAX_REGION_RETRIES + 1):
        if not retryable:
            break
        retryable = list(dict.fromkeys(
            tile_id for tile_id in retryable if tile_id not in all_results
        ))
        if not retryable:
            break

        backoff = RETRY_BACKOFF_BASE * attempt + random.uniform(0, 5)
        logger.info(
            "Retry attempt %d/%d for %d retryable tile(s): %s "
            "(backoff %.1fs)",
            attempt,
            MAX_REGION_RETRIES,
            len(retryable),
            retryable,
            backoff,
        )
        time.sleep(backoff)

        retry_batches = _chunk_global_tiles([
            tile_by_id[tile_id] for tile_id in retryable if tile_id in tile_by_id
        ])
        results_batch, retryable = _run_tile_batches_parallel(retry_batches)
        all_results.update(results_batch)

    still_failed = [tid for tid in retryable if tid not in all_results] if retryable else []
    if still_failed:
        logger.error(
            "Tiles failed after %d retries: %s",
            MAX_REGION_RETRIES,
            still_failed,
        )

    elapsed = time.perf_counter() - t_start
    per_tile = elapsed / total_tiles if total_tiles > 0 else 0
    grand_tankers = sum(r.get("tankers", 0) for r in all_results.values()
                        if isinstance(r, dict))
    grand_cargos = sum(r.get("cargos", 0) for r in all_results.values()
                       if isinstance(r, dict))
    grand_mov_t = sum(r.get("moving_tankers", 0) for r in all_results.values()
                      if isinstance(r, dict))
    grand_mov_c = sum(r.get("moving_cargos", 0) for r in all_results.values()
                      if isinstance(r, dict))

    logger.info(
        "STOPWATCH  global tiles: %.2fs total | %d tiles | %.2fs/tile | "
        "%d tankers (%d mov) | %d cargo (%d mov)",
        elapsed,
        total_tiles,
        per_tile,
        grand_tankers,
        grand_mov_t,
        grand_cargos,
        grand_mov_c,
    )

    # Record the run pointer so orchestrators (run.py) can find the raw file.
    if _capture_log_path.exists():
        try:
            _LATEST_RUN_POINTER.parent.mkdir(parents=True, exist_ok=True)
            _LATEST_RUN_POINTER.write_text(str(_capture_log_path), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write run pointer %s: %s", _LATEST_RUN_POINTER, exc)
        logger.info("Raw run written: %s", _capture_log_path)
        print(f"RUN_CAPTURES={_capture_log_path}")
    else:
        logger.warning("No captures written this run; raw file absent: %s", _capture_log_path)

    if not do_ingest:
        logger.info(
            "Database ingestion skipped (raw-only). Ingest with: "
            "python update_database.py %s", _capture_log_path,
        )
    else:
        try:
            logger.info("Ingesting run into database: %s", _capture_log_path)
            ingest_file(_capture_log_path)
            logger.info("Database ingestion complete")
        except Exception as e:
            logger.error(
                "Database ingestion failed: %s (raw data preserved at %s)",
                e, _capture_log_path,
            )

    return all_results


def scheduled_run(**kwargs):
    jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
    delay = max(0, jitter)
    if delay > 0:
        logger.info("Jitter: waiting %.0fs before run", delay)
        time.sleep(delay)
    capture_all_regions(**kwargs)


def main():
    global geo_profiles

    # Parse CLI flags
    region_filter = None
    zoom_filter = None
    tier_filter = None
    tile_ids_filter = None
    run_once = False
    no_ingest = False
    ingest = False
    dry_run_grid = False
    list_tiles = False
    for arg in sys.argv[1:]:
        if arg.startswith("--regions="):
            region_filter = arg.split("=", 1)[1].split(",")
        elif arg.startswith("--zoom="):
            zoom_filter = [int(z) for z in arg.split("=", 1)[1].split(",")]
        elif arg.startswith("--tier="):
            tier_filter = arg.split("=", 1)[1].split(",")
        elif arg.startswith("--tile-ids="):
            tile_ids_filter = [tid for tid in arg.split("=", 1)[1].split(",") if tid]
        elif arg == "--save-images":
            pass  # Already handled at module level via SAVE_IMAGES
        elif arg == "--once":
            run_once = True
        elif arg == "--no-ingest":
            no_ingest = True
        elif arg == "--ingest":
            ingest = True
        elif arg == "--dry-run-grid":
            dry_run_grid = True
        elif arg == "--list-tiles":
            list_tiles = True
        elif arg == "--list-regions":
            print(f"{'Key':<6} {'Zoom':<5} {'Class':<8} {'Tier':<10} {'Name'}")
            print("-" * 78)
            for key, config in sorted(ACTIVE_REGIONS.items()):
                tier = REGION_TIERS.get(key, "?")
                print(
                    f"{key:<6} z{config['zoom']:<4} "
                    f"{config.get('crowded_class', '?'):<8} "
                    f"{tier:<10} {config.get('name', key)}"
                )
            return
        elif arg == "--help":
            print(__doc__)
            return

    selected_tiles = _select_global_tiles(
        region_filter=region_filter,
        zoom_filter=zoom_filter,
        tier_filter=tier_filter,
        tile_ids=tile_ids_filter,
        respect_schedule=not (dry_run_grid or list_tiles),
    )
    selected_summary = manifest_summary(selected_tiles)
    if dry_run_grid or list_tiles:
        print("Global tile grid")
        print(f"  bbox: {GLOBAL_GRID_BBOX}")
        print(f"  viewport: {VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}")
        print(f"  default_zoom: {GLOBAL_GRID_DEFAULT_ZOOM}")
        print(f"  accept_buffer_px: {TILE_ACCEPT_BUFFER_PX}")
        print(f"  total_tiles: {selected_summary['total_tiles']}")
        print(f"  by_zoom: {selected_summary['by_zoom']}")
        print(f"  by_source: {selected_summary['by_source']}")
        print("  qa: diagnostic only; adaptive zoom/split QA needs redesign")
        if list_tiles:
            for tile in selected_tiles:
                print(
                    f"{tile['tile_id']} z{tile['zoom']} "
                    f"r{tile['row']} c{tile['col']} "
                    f"source={tile.get('source')} seeds={','.join(tile.get('seed_regions', []))}"
                )
        return

    # Resolve proxy geolocations at startup
    logger.info("Resolving proxy geolocations...")
    geo_profiles = resolve_all_proxies(proxies)
    unresolved = [
        proxy["server"] for proxy in proxies
        if not getattr(geo_profiles.get(proxy["server"]), "exit_ip", "")
    ]
    if unresolved:
        raise RuntimeError(
            "proxy geo resolution incomplete: " + ", ".join(unresolved)
        )
    logger.info("Resolved %d/%d proxy profiles", len(geo_profiles), len(proxies))

    logger.info("Global tile summary: %s", selected_summary)
    logger.info("QA note: adaptive zoom/split decisions require a future QA redesign")
    do_ingest = ingest and not no_ingest
    logger.info("Viewport: %dx%d | Max browsers: %d | Save images: %s | "
                "cross_zoom_qa=%s | projection=mouseposition-dom | ingest=%s",
                VIEWPORT_WIDTH, VIEWPORT_HEIGHT, MAX_BROWSERS, SAVE_IMAGES,
                ENABLE_CROSS_ZOOM_QA, do_ingest)

    capture_kwargs = dict(
        region_filter=region_filter,
        no_ingest=no_ingest,
        ingest=ingest,
        zoom_filter=zoom_filter,
        tier_filter=tier_filter,
        tile_ids=tile_ids_filter,
    )

    # Run once immediately
    capture_all_regions(**capture_kwargs)
    if run_once:
        return

    # Schedule future runs (same filters and ingest mode as the initial run)
    sched.every(SCRAPE_INTERVAL_MINUTES).minutes.do(scheduled_run, **capture_kwargs)
    while True:
        sched.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
