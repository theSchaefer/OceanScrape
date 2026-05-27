#!/bin/python3
"""Patchright scraper — global maritime chokepoint monitor.

Single-region-per-worker model: each region is an atomic task processed in
a fresh browser/context.  Up to MAX_BROWSERS regions run in parallel, but
no worker ever transitions between regions.  This eliminates state drift
(map zoom/center, UI overlays, vessel filter, projection offset) that the
previous work-stealing model accumulated across stolen regions.  Supports
per-region zoom levels, inline OpenCV ship detection, and JPEG output for
minimal storage.

Usage:
  python scraper_global.py                  # Run all regions
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
import os
import platform
import random
import re
import string
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

from coastline_alignment import CoastlineOffsetTracker, coastline_source_status
from debug_map_probe import run_map_probe, write_frame_scan
from geo_profile import GeoProfile, resolve_all_proxies, EGYPT_FALLBACK_DATA
from grid import (
    get_tile_centers, get_bbox_tile_centers, polygon_to_pixel_coords,
    tile_id as make_tile_id, _point_in_polygon,
    lat_to_pixel_y, lon_to_pixel_x, pixel_x_to_lon, pixel_y_to_lat,
)
from regions import REGIONS, REGION_TIERS, load_bbox_regions
from update_database import process_log

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
MAX_DRAG_PX = 800  # max single-drag distance to avoid Leaflet inertia
USE_SETVIEW_OPTIMIZATION = os.getenv("USE_SETVIEW_OPTIMIZATION", "0") == "1"
LEAFLET_DIAGNOSTICS = os.getenv("LEAFLET_DIAGNOSTICS", "0") == "1"
LEAFLET_PROJECTION_FALLBACK = os.getenv("LEAFLET_PROJECTION_FALLBACK", "0") == "1"
USE_BBOX_TILING = os.getenv("USE_BBOX_TILING", "1") == "1"
BBOX_OVERLAP_PX = int(os.getenv("BBOX_OVERLAP_PX", "128"))
ENABLE_CROSS_ZOOM_QA = os.getenv("ENABLE_CROSS_ZOOM_QA", "1") == "1"
QA_SAMPLE_RATE = float(os.getenv("QA_SAMPLE_RATE", "0.10"))
QA_MIN_SAMPLES = int(os.getenv("QA_MIN_SAMPLES", "1"))
QA_MAX_SAMPLES = int(os.getenv("QA_MAX_SAMPLES", "3"))
QA_TOTAL_RATIO_THRESHOLD = float(os.getenv("QA_TOTAL_RATIO_THRESHOLD", "1.35"))
QA_TYPE_RATIO_THRESHOLD = float(os.getenv("QA_TYPE_RATIO_THRESHOLD", "1.50"))
QA_ABS_DELTA_THRESHOLD = int(os.getenv("QA_ABS_DELTA_THRESHOLD", "5"))


def _default_coastline_calibration_enabled():
    if os.getenv("COASTLINE_DATA_PATH") or os.getenv("COASTLINE_GEOJSON"):
        return True
    base = Path("./data/coastline")
    return any(
        (base / name).exists()
        for name in ("ne_10m_land.geojson", "ne_10m_land.zip", "ne_10m_land.shp")
    )


ENABLE_COASTLINE_CALIBRATION = (
    os.getenv(
        "ENABLE_COASTLINE_CALIBRATION",
        "1" if _default_coastline_calibration_enabled() else "0",
    ) == "1"
)
ACTIVE_REGIONS = load_bbox_regions(use_bbox_tiling=USE_BBOX_TILING)

# Crash retry settings
MAX_REGION_RETRIES = 2
RETRY_BACKOFF_BASE = 5  # seconds

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
for i in range(10026, 10096):
    proxy = {
        "server": f"http://isp.decodo.com:{i}",
        "username": os.getenv("DECODO_USERNAME"),
        "password": os.getenv("DECODO_PASSWORD"),
    }
    proxies.append(proxy)

# Geo profiles resolved at startup
geo_profiles: dict[str, GeoProfile] = {}

# --- User agents --------------------------------------------------------------

# UA lists keyed by actual OS — must match navigator.platform to avoid
# Cloudflare cross-check detection.  Versions should track recent stable
# Chrome releases; outdated versions are a strong bot signal.
_CHROME_UA_BY_OS = {
    "windows": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    ],
    "linux": [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    ],
    "darwin": [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    ],
}

# Detect host OS once at import time
_HOST_OS = "linux" if platform.system() == "Linux" else (
    "darwin" if platform.system() == "Darwin" else "windows"
)


def _random_user_agent() -> str:
    return random.choice(_CHROME_UA_BY_OS[_HOST_OS])


# --- Helpers ------------------------------------------------------------------


def build_url(lat, lon, zoom):
    return (
        f"https://www.marinetraffic.com/en/ais/home"
        f"/centerx:{lon}/centery:{lat}/zoom:{zoom}"
    )


def _polygon_center(polygon):
    """Return (lat, lon) center of a polygon."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)


def _random_cookies(domain):
    """Generate random cookies to mimic a returning visitor."""
    def _rand_str(n):
        return "".join(random.choices(string.ascii_letters + string.digits, k=n))

    cookies = [
        {"name": "_ga", "value": f"GA1.2.{random.randint(100000000, 999999999)}.{random.randint(1600000000, 1710000000)}", "domain": domain, "path": "/"},
        {"name": "_gid", "value": f"GA1.2.{random.randint(100000000, 999999999)}.{random.randint(1700000000, 1710000000)}", "domain": domain, "path": "/"},
        {"name": "_gat", "value": "1", "domain": domain, "path": "/"},
        {"name": "JSESSIONID", "value": _rand_str(32), "domain": domain, "path": "/"},
        {"name": "SERVERID", "value": f"s{random.randint(1, 5)}", "domain": domain, "path": "/"},
    ]
    return random.sample(cookies, k=random.randint(2, len(cookies)))


def dismiss_cookie_banner(page):
    """Click the cookie consent 'Accept' button if present."""
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
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                logger.info("  Dismissed cookie banner via: %s", sel)
                time.sleep(0.5)
                return
        except Exception:
            continue


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

    if _click_dark_map_option_after_open(page):
        if (_page_has_dark_theme(page) or _page_has_dark_map_resources(page)
                or _page_has_dark_map_dom(page)):
            logger.info("  Dark map applied")
            return True

    direct_selectors = [
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
            async ({ desired }) => {
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
                    const deadline = Date.now() + 2500;
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
            {"desired": desired},
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


def wait_for_map_tiles(page, timeout_ms=8000):
    """Wait for map canvas to render actual tile imagery."""
    try:
        page.wait_for_selector('canvas', state='attached', timeout=timeout_ms)
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            has_content = page.evaluate("""
            () => {
                const canvas = document.querySelector('canvas');
                if (!canvas) return false;
                try {
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return false;
                    const w = canvas.width, h = canvas.height;
                    const pts = [[w*0.25,h*0.25],[w*0.5,h*0.5],[w*0.75,h*0.75],
                                 [w*0.25,h*0.75],[w*0.75,h*0.25]];
                    let hits = 0;
                    for (const [x, y] of pts) {
                        const p = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data;
                        if (p[3] > 0 && (p[0] < 250 || p[1] < 250 || p[2] < 250)) hits++;
                    }
                    return hits >= 2;
                } catch(e) { return false; }
            }
            """)
            if has_content:
                logger.info("  Map tiles rendered")
                return
            time.sleep(0.25)
    except Exception as e:
        logger.warning("  wait_for_map_tiles: %s", e)
    time.sleep(0.25)


# --- Stealth ------------------------------------------------------------------


def _inject_stealth_scripts(page, geo: GeoProfile):
    """Minimal stealth — Patchright already patches navigator.webdriver and
    chrome.runtime.  Only add what Patchright does NOT handle."""
    locale = geo.locale
    language = locale.split("-")[0]

    # navigator.platform must match the UA string's OS claim, otherwise
    # Cloudflare's cross-check flags the mismatch as a bot.
    plat_value = {
        "windows": "Win32",
        "linux": "Linux x86_64",
        "darwin": "MacIntel",
    }[_HOST_OS]

    page.add_init_script(f"""
    Object.defineProperty(navigator, 'languages', {{
        get: () => ['{locale}', '{language}', 'en'],
    }});

    Object.defineProperty(navigator, 'platform', {{
        get: () => '{plat_value}',
    }});

    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : _origQuery(params);

    // Mimic a real Chrome plugin list — must be PluginArray-shaped, not raw ints.
    // Real Chrome always has these two; returning integers trips any script that
    // reads .name or .description.
    Object.defineProperty(navigator, 'plugins', {{
        get: () => {{
            const pdf = {{ name: 'PDF Viewer', description: 'Portable Document Format',
                          filename: 'internal-pdf-viewer', length: 1 }};
            const native = {{ name: 'Chrome PDF Viewer', description: '',
                             filename: 'internal-pdf-viewer', length: 1 }};
            const arr = [pdf, native];
            arr.item = (i) => arr[i];
            arr.namedItem = (n) => arr.find(p => p.name === n) || null;
            arr.refresh = () => {{}};
            return arr;
        }}
    }});
    """)


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


def _wait_for_ais_markers(page, timeout_ms=3000):
    """Wait until AIS ship markers appear on the Leaflet overlay pane.

    Replaces the fixed time.sleep() AIS wait with actual detection of when
    ship marker elements or overlay canvas content appear.  Falls back after
    timeout (open ocean may legitimately have no ships).
    """
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        has_markers = page.evaluate("""
        () => {
            const pane = document.querySelector('.leaflet-overlay-pane');
            if (!pane) return false;

            // Check if the overlay canvas has non-transparent content
            const canvas = pane.querySelector('canvas');
            if (canvas && canvas.width > 0) {
                try {
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return false;
                    const w = canvas.width, h = canvas.height;
                    const pts = [
                        [w*0.25, h*0.25], [w*0.5, h*0.5], [w*0.75, h*0.75],
                        [w*0.25, h*0.75], [w*0.75, h*0.25],
                        [w*0.1, h*0.5], [w*0.9, h*0.5],
                        [w*0.5, h*0.1], [w*0.5, h*0.9],
                    ];
                    for (const [x, y] of pts) {
                        const p = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data;
                        if (p[3] > 10) return true;
                    }
                } catch(e) { /* cross-origin taint */ }
            }

            // Fallback: check for SVG/img elements in overlay pane
            const children = pane.querySelectorAll('svg, img, div');
            if (children.length > 0) return true;

            return false;
        }
        """)
        if has_markers:
            return True
        time.sleep(0.05)

    return False


# --- Leaflet setView map panning ---------------------------------------------


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


def _pan_map_js(page, target_lat, target_lon, zoom):
    """Pan map using Leaflet's setView API — pixel-perfect positioning."""
    return page.evaluate(f"""
    () => {{
        const map = window.__mtMap;
        if (!map || typeof map.setView !== 'function') return false;
        map.setView([{target_lat}, {target_lon}], {zoom}, {{animate: false}});
        return true;
    }}
    """)


def _pan_map(page, cur_lat, cur_lon, target_lat, target_lon, zoom,
             map_center=None, timeout_ms=5000, region_name=None,
             timestamp_str=None, worker_id=None):
    """Pan the map using mouse drag by default.

    Leaflet setView remains an explicit optimization path. Production capture
    does not require the map object and does not abort when it is unavailable.
    Returns the navigation mode used: ``setView`` or ``mouse-drag``.
    """

    if USE_SETVIEW_OPTIMIZATION:
        setview_available = False
        try:
            setview_available = _discover_leaflet_map(page)
            if setview_available and _pan_map_js(page, target_lat, target_lon, zoom):
                logger.info("  Panned via setView (%.5f, %.5f)", target_lat, target_lon)
                time.sleep(0.05)
                _wait_for_tiles_after_pan(page, timeout_ms)
                _wait_for_ais_markers(page, timeout_ms=2000)
                return "setView"
        except Exception as exc:
            logger.debug("  setView optimization unavailable: %s", exc)

        if LEAFLET_DIAGNOSTICS and not setview_available:
            _emit_frame_scan(
                page,
                timestamp_str,
                region_name or "unknown",
                "setView_unavailable",
                worker_id=worker_id,
            )

    total_pixels = 256 * (2 ** zoom)
    dx = (target_lon - cur_lon) * total_pixels / 360.0
    dy = lat_to_pixel_y(target_lat, zoom) - lat_to_pixel_y(cur_lat, zoom)
    drag_x = -dx
    drag_y = -dy

    if abs(drag_x) < 1 and abs(drag_y) < 1:
        return "mouse-drag"

    if map_center:
        cx, cy = map_center
    else:
        cx = VIEWPORT_WIDTH // 2
        cy = VIEWPORT_HEIGHT // 2

    steps_needed = max(
        1,
        int(abs(drag_x) / MAX_DRAG_PX) + (1 if abs(drag_x) % MAX_DRAG_PX > 0 else 0),
        int(abs(drag_y) / MAX_DRAG_PX) + (1 if abs(drag_y) % MAX_DRAG_PX > 0 else 0),
    )
    step_dx = drag_x / steps_needed
    step_dy = drag_y / steps_needed

    for _ in range(steps_needed):
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + step_dx, cy + step_dy, steps=20)
        page.mouse.up()
        if steps_needed > 1:
            time.sleep(0.05)

    logger.info("  Panned via mouse_drag (dx=%.0f dy=%.0f, %d step(s))",
                drag_x, drag_y, steps_needed)
    time.sleep(0.05)
    _wait_for_tiles_after_pan(page, timeout_ms)
    _wait_for_ais_markers(page, timeout_ms=2000)
    return "mouse-drag"


def _wait_for_tiles_after_pan(page, timeout_ms=5000):
    """Wait for map tiles to re-render after a pan."""
    deadline = time.time() + (timeout_ms / 1000)
    time.sleep(0.05)
    while time.time() < deadline:
        has_content = page.evaluate("""
        () => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return false;
            try {
                const ctx = canvas.getContext('2d');
                if (!ctx) return false;
                const w = canvas.width, h = canvas.height;
                const pts = [[w*0.25,h*0.25],[w*0.5,h*0.5],[w*0.75,h*0.75],
                             [w*0.25,h*0.75],[w*0.75,h*0.25]];
                let hits = 0;
                for (const [x, y] of pts) {
                    const p = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data;
                    if (p[3] > 0 && (p[0] < 250 || p[1] < 250 || p[2] < 250)) hits++;
                }
                return hits >= 3;
            } catch(e) { return false; }
        }
        """)
        if has_content:
            return
        time.sleep(0.05)


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


def _shift_marker_by_pixels(marker, dx_px, dy_px, zoom):
    """Shift a geolocated marker in Web Mercator pixel space."""
    gx = lon_to_pixel_x(marker["lon"], zoom) + dx_px
    gy = lat_to_pixel_y(marker["lat"], zoom) + dy_px
    shifted = dict(marker)
    shifted["lon"] = round(pixel_x_to_lon(gx, zoom), 5)
    shifted["lat"] = round(pixel_y_to_lat(gy, zoom), 5)
    return shifted


def _projection_before_after(tile_markers, coast_fit, zoom, polygon):
    """Estimate before/after impact of coastline projection correction.

    Detection count is unchanged by projection correction, but polygon-filtered
    marker counts can change when the coordinates move near a region boundary.
    ``before`` is the requested-center projection estimate reconstructed from
    the corrected coordinates.
    """
    if not coast_fit or not coast_fit.usable or not tile_markers:
        return {
            "positional_error_before_m": 0.0,
            "positional_error_after_m": 0.0,
            "markers_before_filter": len(tile_markers),
            "markers_after_filter": len(tile_markers),
        }

    fallback_markers = [
        _shift_marker_by_pixels(m, coast_fit.dx_px, coast_fit.dy_px, zoom)
        for m in tile_markers
    ]
    if polygon:
        before_counts, before_filtered = _filter_markers_to_polygon(
            fallback_markers, polygon
        )
        after_counts, after_filtered = _filter_markers_to_polygon(tile_markers, polygon)
    else:
        before_counts = _count_markers_by_type(fallback_markers)
        after_counts = _count_markers_by_type(tile_markers)
        before_filtered = fallback_markers
        after_filtered = tile_markers
    residual_m = coast_fit.meters * max(0.0, 1.0 - coast_fit.confidence)
    return {
        "positional_error_before_m": round(coast_fit.meters, 1),
        "positional_error_after_m": round(residual_m, 1),
        "markers_before_filter": len(before_filtered),
        "markers_after_filter": len(after_filtered),
        "counts_before_filter": before_counts,
        "counts_after_filter": after_counts,
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
    if nav_counts.get("setView", 0) and nav_counts.get("mouse-drag", 0):
        return "mixed"
    if nav_counts.get("setView", 0):
        return "setView"
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
    if any(str(s).startswith("coastline") for s in sources):
        return "mixed-coastline"
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
    nav_counts = {"mouse-drag": 0, "setView": 0}
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


def _write_calibration_artifact(timestamp_str, region_name, config, coastline_summary):
    payload = {
        "enabled": ENABLE_COASTLINE_CALIBRATION,
        "region": region_name,
        "region_name": config.get("name", region_name),
        "timestamp": timestamp_str,
        "zoom_used": config.get("zoom"),
        "calibration": coastline_summary,
    }
    payload["path"] = _write_json_artifact("calibration", timestamp_str, region_name, payload)
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


# --- Core capture (single region, given a page) ------------------------------


def _capture_region_tiles(region_name, config, timestamp_str, page, map_dims):
    """Capture all tiles for a region using an already-setup page.

    Returns dict with capture results: tankers, cargos, markers, file paths.
    """
    polygon = config.get("polygon")
    zoom = config["zoom"]
    region_display = config.get("name", region_name)
    tiling_mode = "bbox" if USE_BBOX_TILING and config.get("bbox") else "polygon"

    map_width = int(map_dims["width"])
    map_height = int(map_dims["height"])
    map_cx = int(map_dims["x"]) + map_width // 2
    map_cy = int(map_dims["y"]) + map_height // 2

    tiles, grid_info = _get_tile_grid(region_name, config)
    n_rows = grid_info["n_rows"]
    n_cols = grid_info["n_cols"]
    logger.info("Region %s (%s): %d tiles (%dx%d), zoom %d mode=%s class=%s",
                region_name, region_display, len(tiles), n_rows, n_cols, zoom,
                tiling_mode, config.get("crowded_class"))

    tile_images = {}
    tile_detections = []
    all_markers = []
    tiles_ok = 0
    tiles_failed = 0
    total_tankers = 0
    total_cargos = 0
    total_moving_tankers = 0
    total_moving_cargos = 0
    projection_fallback_logged = False
    leaflet_diag_logged = False
    nav_counts = {"mouse-drag": 0, "setView": 0}
    coastline_tracker = CoastlineOffsetTracker(
        region_name,
        logger=logger,
        enabled=ENABLE_COASTLINE_CALIBRATION,
    )
    coastline_status = coastline_tracker.source_status()
    logger.info(
        "Region %s: coastline alignment source=%s available=%s path=%s "
        "polygons=%d reason=%s",
        region_name,
        coastline_status["source"],
        coastline_status["available"],
        coastline_status["path"],
        coastline_status["polygons"],
        coastline_status.get("reason") or "ok",
    )

    center_lat, center_lon = _region_center(config)
    current_lat, current_lon = center_lat, center_lon
    map_locator = page.locator('#map_canvas')

    # Verify map canvas is present before capturing tiles
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
        logger.warning("Region %s: map not ready — %s", region_name, canvas_state)

    # Guard: if tile filtering removed everything, skip this region
    if not tiles:
        logger.warning("Region %s: 0 tiles after grid filtering, skipping", region_name)
        return {
            "tankers": 0, "cargos": 0,
            "moving_tankers": 0, "moving_cargos": 0,
            "tiles_ok": 0, "tiles_failed": 0, "tiles_total": 0,
            "markers": [], "tile_images": {},
        }

    # Navigate to first tile
    first_row, first_col, first_lat, first_lon, _first_tile_id = _unpack_tile(
        tiles[0], region_name, zoom
    )
    nav_mode = _pan_map(
        page, center_lat, center_lon, first_lat, first_lon, zoom,
        map_center=(map_cx, map_cy), region_name=region_name,
        timestamp_str=timestamp_str,
    )
    nav_counts[nav_mode] = nav_counts.get(nav_mode, 0) + 1
    current_lat, current_lon = first_lat, first_lon

    # Capture tiles
    for i, tile in enumerate(tiles):
        row, col, lat, lon, tid = _unpack_tile(tile, region_name, zoom)
        if i > 0:
            nav_mode = _pan_map(
                page, current_lat, current_lon, lat, lon, zoom,
                map_center=(map_cx, map_cy), region_name=region_name,
                timestamp_str=timestamp_str,
            )
            nav_counts[nav_mode] = nav_counts.get(nav_mode, 0) + 1
            current_lat, current_lon = lat, lon

        try:
            pre_capture_center_offset = None
            if LEAFLET_DIAGNOSTICS or LEAFLET_PROJECTION_FALLBACK:
                pre_capture_center_offset = _wait_for_map_center_offset(
                    page, timeout_ms=250)
            screenshot_args = {"type": SCREENSHOT_FORMAT}
            if SCREENSHOT_FORMAT == "jpeg":
                screenshot_args["quality"] = SCREENSHOT_QUALITY
            img_bytes = map_locator.screenshot(**screenshot_args)

            leaflet_center_offset = None
            if LEAFLET_DIAGNOSTICS or LEAFLET_PROJECTION_FALLBACK:
                try:
                    leaflet_center_offset = (
                        _get_map_center_offset(page) or pre_capture_center_offset
                    )
                except Exception:
                    leaflet_center_offset = pre_capture_center_offset
                if (
                    LEAFLET_DIAGNOSTICS
                    and not leaflet_center_offset
                    and not leaflet_diag_logged
                ):
                    scan_path = _emit_frame_scan(
                        page,
                        timestamp_str,
                        region_name,
                        "center_offset_capture_unavailable",
                    )
                    logger.warning(
                        "Region %s: Leaflet center_offset unavailable; "
                        "continuing with coastline/requested-center projection; "
                        "frame_scan=%s",
                        region_name,
                        scan_path,
                    )
                    leaflet_diag_logged = True

            coast_fit = coastline_tracker.estimate(
                img_bytes, lat, lon, zoom, tile=(row, col)
            )
            center_offset = None
            projection_source = "requested-center"
            proj_lat = lat
            proj_lon = lon
            proj_zoom = zoom
            if coast_fit.usable:
                center_offset = coast_fit.as_center_offset(map_width, map_height, zoom)
                projection_source = f"coastline-{coast_fit.source}"
                if coast_fit.source == "fit":
                    logger.info(
                        "  Tile (%d,%d): coastline-fit conf=%.3f "
                        "offset=(%.6f, %.6f; %.0fm) px=(%.0f, %.0f)",
                        row, col, coast_fit.confidence,
                        coast_fit.delta_lat, coast_fit.delta_lon,
                        coast_fit.meters, coast_fit.dx_px, coast_fit.dy_px,
                    )
            elif LEAFLET_PROJECTION_FALLBACK and leaflet_center_offset:
                center_offset = leaflet_center_offset
                projection_source = "leaflet-fallback"
                proj_lat = leaflet_center_offset.get("map_lat") or lat
                proj_lon = leaflet_center_offset.get("map_lng") or lon
                proj_zoom = leaflet_center_offset.get("map_zoom") or zoom
            elif not projection_fallback_logged:
                logger.warning(
                    "Region %s: projection fallback active: coastline source=%s "
                    "confidence=%.3f reason=%s; using requested tile centers",
                    region_name,
                    coast_fit.source,
                    coast_fit.confidence,
                    coast_fit.reason,
                )
                projection_fallback_logged = True

            # Inline ship detection + geo-coordinate extraction. Coastline
            # registration supplies a seer.py-compatible center_offset, so the
            # marker projection is corrected without making Leaflet mandatory.
            logger.debug("  Tile (%d,%d): running OpenCV detection on %d bytes",
                         row, col, len(img_bytes))
            det, tile_markers, img_shape = _detect_ships_inline(
                img_bytes, proj_lat, proj_lon, proj_zoom,
                map_width, map_height, center_offset=center_offset
            )

            # BBox mode keeps the full bbox; legacy mode preserves polygon filtering.
            raw_count = len(tile_markers)
            filter_polygon = polygon if tiling_mode == "polygon" else None
            projection_compare = _projection_before_after(
                tile_markers,
                coast_fit if coast_fit.usable else None,
                zoom,
                filter_polygon,
            )
            if filter_polygon:
                det, tile_markers = _filter_markers_to_polygon(
                    tile_markers, filter_polygon
                )
            if raw_count != len(tile_markers):
                logger.debug("  Tile (%d,%d): geo-filtered %d → %d markers",
                             row, col, raw_count, len(tile_markers))

            logger.debug("  Tile (%d,%d): detection result: %s, %d markers",
                         row, col, det, len(tile_markers))

            # Debug: warn if Leaflet's actual center/zoom drifted from the
            # requested setView arguments (would indicate MarineTraffic is
            # rounding, clamping, or otherwise mutating our pan calls).
            if leaflet_center_offset:
                from seer import _debug_center_check
                _debug_center_check(lat, lon, leaflet_center_offset, row, col, logger)
                act_zoom = leaflet_center_offset.get("map_zoom")
                if act_zoom is not None and abs(act_zoom - zoom) > 1e-6:
                    logger.warning("  Tile (%d,%d): setZoom drift! "
                                   "requested %s, actual %s",
                                   row, col, zoom, act_zoom)

            st = det["stationary_tankers"]
            mt = det["moving_tankers"]
            sc = det["stationary_cargos"]
            mc = det["moving_cargos"]
            tankers = st + mt
            cargos = sc + mc

            total_tankers += tankers
            total_cargos += cargos
            total_moving_tankers += mt
            total_moving_cargos += mc
            all_markers.extend(tile_markers)

            tile_detections.append({
                "tile_id": tid,
                "tile": [row, col],
                "center_lat": lat,
                "center_lon": lon,
                "zoom": zoom,
                "tiling_mode": tiling_mode,
                "tankers": tankers,
                "cargos": cargos,
                "moving_tankers": mt,
                "moving_cargos": mc,
                "markers": tile_markers,
                # Projection forensics — lets us correlate misplaced markers
                # with the exact map state and screenshot dimensions the
                # scraper saw at capture time.
                "proj": {
                    "req_lat": lat, "req_lon": lon, "req_zoom": zoom,
                    "source": projection_source,
                    "act_lat": (leaflet_center_offset.get("map_lat")
                                if leaflet_center_offset else None),
                    "act_lon": (leaflet_center_offset.get("map_lng")
                                if leaflet_center_offset else None),
                    "act_zoom": (leaflet_center_offset.get("map_zoom")
                                 if leaflet_center_offset else None),
                    "dpr": center_offset.get("dpr") if center_offset else None,
                    "img_h": int(img_shape[0]) if img_shape else None,
                    "img_w": int(img_shape[1]) if img_shape else None,
                    "center_x": center_offset.get("center_x") if center_offset else None,
                    "center_y": center_offset.get("center_y") if center_offset else None,
                    "coastline": coast_fit.to_log_dict(),
                    "before_after": projection_compare,
                },
            })

            if SAVE_IMAGES:
                tile_images[(row, col)] = img_bytes

            tiles_ok += 1
            logger.info(
                "  Tile (%d,%d) [%d/%d]: %d tankers (%d mov), "
                "%d cargo (%d mov) projection=%s coast_conf=%.3f",
                row, col, i + 1, len(tiles), tankers, mt, cargos, mc,
                projection_source, coast_fit.confidence,
            )

        except Exception as e:
            logger.error("  Tile (%d,%d) failed: %s", row, col, e)
            tiles_failed += 1

    coastline_summary = coastline_tracker.summary()
    last_fit = coastline_summary.get("last_fit") or {}
    logger.info(
        "Region %s navigation summary: nav=%s setView_opt=%s "
        "coast_conf=%s offset=(dlat=%s dlon=%s meters=%s) "
        "projection_fallbacks=%d coastline_stats=%s",
        region_name,
        nav_counts,
        USE_SETVIEW_OPTIMIZATION,
        last_fit.get("confidence"),
        last_fit.get("delta_lat"),
        last_fit.get("delta_lon"),
        last_fit.get("meters"),
        coastline_summary["stats"].get("fallback", 0),
        coastline_summary["stats"],
    )

    # --- Save composite image if requested ------------------------------------
    saved_path = ""
    file_size_kb = 0.0

    if SAVE_IMAGES and tile_images:
        output_dir = Path(CAPTURES_DIR) / region_name
        output_dir.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if SCREENSHOT_FORMAT == "jpeg" else "png"
        filename = f"{region_name}{timestamp_str}.{ext}"
        output_path = output_dir / filename

        comp_w = n_cols * map_width
        comp_h = n_rows * map_height
        composite = Image.new("RGB", (comp_w, comp_h), (0, 0, 0))

        for (r, c), img_bytes in tile_images.items():
            tile_img = Image.open(io.BytesIO(img_bytes))
            composite.paste(tile_img, (c * map_width, r * map_height))

        if tiling_mode == "polygon" and polygon:
            # Mask to polygon only in legacy polygon mode.
            pixel_coords = polygon_to_pixel_coords(polygon, grid_info, zoom)
            mask = Image.new("L", composite.size, 0)
            draw = ImageDraw.Draw(mask)
            draw.polygon(pixel_coords, fill=255)

            black = Image.new("RGB", composite.size, (0, 0, 0))
            result = Image.composite(composite, black, mask)

            # Crop to polygon bounding box to reduce file size
            bbox = mask.getbbox()
            if bbox:
                result = result.crop(bbox)
        else:
            result = composite

        if SCREENSHOT_FORMAT == "jpeg":
            result.save(str(output_path), quality=SCREENSHOT_QUALITY)
        else:
            result.save(str(output_path))

        file_size_kb = output_path.stat().st_size / 1024
        saved_path = str(output_path)
        logger.info("Region %s: saved %s (%.1f KB)", region_name, filename, file_size_kb)

    baseline_by_tile_id = {
        tile.get("tile_id"): tile for tile in tile_detections if tile.get("tile_id")
    }
    nav_mode_used = _nav_mode_summary(nav_counts)
    projection_mode = _projection_mode_summary(tile_detections)
    try:
        qa_summary = _run_cross_zoom_qa(
            page,
            region_name,
            config,
            timestamp_str,
            tiles,
            baseline_by_tile_id,
            map_dims,
            nav_mode_used,
            projection_mode,
        )
    except Exception as exc:
        qa_summary = {
            "enabled": ENABLE_CROSS_ZOOM_QA,
            "region": region_name,
            "timestamp": timestamp_str,
            "nav_mode": nav_mode_used,
            "projection_mode": projection_mode,
            "zoom_used": zoom,
            "qa_flags": ["qa_failed"],
            "qa_confidence": 0.0,
            "error": str(exc),
        }
        qa_summary["path"] = _write_json_artifact(
            "qa", timestamp_str, region_name, qa_summary
        )
        logger.warning("Region %s QA failed: %s", region_name, exc)
    calibration_artifact = _write_calibration_artifact(
        timestamp_str, region_name, config, coastline_summary
    )

    qa_flags = qa_summary.get("qa_flags", [])
    qa_confidence = qa_summary.get("qa_confidence", 0.0)

    # --- Log results ----------------------------------------------------------
    _log_json(
        timestamp_str, region_name, saved_path,
        len(tiles), tiles_ok, tiles_failed, file_size_kb,
        total_tankers, total_cargos, zoom, tile_detections,
        moving_tankers=total_moving_tankers,
        moving_cargos=total_moving_cargos,
        markers=all_markers,
        nav_mode=nav_mode_used,
        projection_mode=projection_mode,
        zoom_used=zoom,
        qa_flags=qa_flags,
        qa_confidence=qa_confidence,
    )

    logger.info("Region %s: %d tankers (%d mov), %d cargo (%d mov) (from %d tiles)",
                region_name, total_tankers, total_moving_tankers,
                total_cargos, total_moving_cargos, tiles_ok)

    return {
        "region": region_name,
        "tankers": total_tankers,
        "cargos": total_cargos,
        "moving_tankers": total_moving_tankers,
        "moving_cargos": total_moving_cargos,
        "tiles_ok": tiles_ok,
        "tiles_failed": tiles_failed,
        "detections": tile_detections,
        "nav": nav_counts,
        "nav_mode": nav_mode_used,
        "projection_mode": projection_mode,
        "zoom_used": zoom,
        "qa": qa_summary,
        "qa_flags": qa_flags,
        "qa_confidence": qa_confidence,
        "coastline": coastline_summary,
        "calibration": calibration_artifact,
    }


# --- Single-region browser worker --------------------------------------------


def capture_worker(region_name, timestamp_str):
    """Single-region worker: opens a fresh browser, processes exactly one
    region, then tears the browser down.

    Replaces the previous work-stealing model where one worker would steal
    multiple regions and reuse the same page via Leaflet.setView() panning.
    State drift between regions (map zoom rounding, vessel-filter UI state,
    overlay visibility, projection offset, Cloudflare cookies) accumulated
    across stolen regions and caused systematic marker misplacement on the
    second+ region. Trading throughput for determinism: every region pays
    the Cloudflare + setup cost, but starts from a known-clean state.
    """
    worker_id = threading.current_thread().name
    config = ACTIVE_REGIONS[region_name]
    zoom = config["zoom"]
    region_display = config.get("name", region_name)

    proxy = random.choice(proxies)
    fallback = GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)
    geo = geo_profiles.get(proxy["server"], fallback)

    results = {}
    retryable = []  # populated if browser/driver crashed (retry with fresh worker)

    logger.info(
        "[mode=single-region-worker worker=%s] region=%s (%s) zoom=%d starting",
        worker_id, region_name, region_display, zoom,
    )

    try:
        with sync_playwright() as p:
            # GPU flags differ by environment: on a headless Linux VPS without
            # a real GPU, requesting GPU rasterization / ANGLE causes WebGL to
            # report a SwiftShader or "Google Inc." renderer — a known bot
            # signal.  We only enable GPU acceleration when a display is
            # available (i.e. a desktop with a real GPU).
            _has_display = os.environ.get("DISPLAY") or _HOST_OS != "linux"
            chrome_args = [
                "--headless=new",
                "--disable-dev-shm-usage",
            ]
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
                # Pin device_scale_factor=1 so screenshot dimensions exactly
                # match the viewport CSS size. Without this, host OS display
                # scaling (e.g. Windows at 150%) can leak through and produce
                # over-resolution screenshots, which then break the marker
                # projection math (see seer._pixel_to_latlon).
                device_scale_factor=1.0,
                timezone_id=geo.timezone_id,
                locale=geo.locale,
                geolocation={"latitude": geo.latitude, "longitude": geo.longitude},
                permissions=["geolocation"],
                extra_http_headers={"Accept-Language": geo.accept_language},
                user_agent=_random_user_agent(),
            )
            context.add_cookies(_random_cookies(".marinetraffic.com"))

            page = context.new_page()
            _inject_stealth_scripts(page, geo)
            _inject_map_hooks(page)

            center_lat, center_lon = _region_center(config)
            url = build_url(center_lat, center_lon, zoom)
            logger.info(
                "[worker=%s region=%s] loading url at zoom %d",
                worker_id, region_name, zoom,
            )
            page.goto(url, wait_until="domcontentloaded")

            # Setup: Cloudflare, overlays, map discovery
            page_ready = True
            filter_state = "skipped"
            dark_state = "skipped"
            try:
                if _wait_for_cloudflare(page):
                    logger.info("[worker=%s region=%s] Cloudflare passed",
                                worker_id, region_name)
                elif _is_cloudflare_blocked(page):
                    logger.error("[worker=%s region=%s] Cloudflare block",
                                 worker_id, region_name)
                    _log_json(timestamp_str, region_name, "", 0, 0, 0, 0.0, 0, 0,
                              zoom, [])
                    page_ready = False

                if page_ready:
                    wait_for_map_tiles(page)
                    dismiss_cookie_banner(page)
                    dark_ok = set_dark_mode(page)
                    dark_state = "applied" if dark_ok else "unavailable"
                    filter_ok = set_vessel_filter(page)
                    filter_state = "applied" if filter_ok else "failed"
                    if not filter_ok:
                        raise RuntimeError("required setup failed: vessel filter")
                    hide_ui_overlays(page)
                    if USE_SETVIEW_OPTIMIZATION or LEAFLET_DIAGNOSTICS:
                        _discover_leaflet_map(page)
            except Exception as e:
                retry_setup = (
                    _is_crash_error(e)
                    or "required setup failed" in str(e).lower()
                )
                if retry_setup:
                    logger.error("[worker=%s region=%s] setup failed (retryable): %s",
                                 worker_id, region_name, e)
                    retryable.append(region_name)
                else:
                    logger.error("[worker=%s region=%s] setup failed: %s",
                                 worker_id, region_name, e)
                    _log_json(timestamp_str, region_name, "", 0, 0, 0, 0.0, 0, 0,
                              zoom, [])
                page_ready = False

            if not page_ready:
                context.close()
                browser.close()
                results["_retryable"] = retryable
                return results

            map_dims = _get_map_dimensions(page)
            center_offset = None
            if USE_SETVIEW_OPTIMIZATION or LEAFLET_DIAGNOSTICS or LEAFLET_PROJECTION_FALLBACK:
                center_offset = _wait_for_map_center_offset(page, timeout_ms=1000)
                try:
                    map_probe = run_map_probe(page)
                    logger.info("%s", json.dumps({
                        "event": "map_probe",
                        "worker": worker_id,
                        "region": region_name,
                        "probe": map_probe,
                    }, sort_keys=True))
                except Exception as e:
                    logger.warning(
                        "[worker=%s region=%s] map probe failed: %s",
                        worker_id, region_name, e,
                    )
                if LEAFLET_DIAGNOSTICS and not center_offset:
                    scan_path = _emit_frame_scan(
                        page,
                        timestamp_str,
                        region_name,
                        "center_offset_setup_unavailable",
                        worker_id=worker_id,
                    )
                    logger.warning(
                        "[worker=%s region=%s] Leaflet center_offset unavailable; "
                        "production capture continues with coastline/requested-center "
                        "projection; frame_scan=%s",
                        worker_id, region_name, scan_path,
                    )
            coast_status = coastline_source_status()

            logger.info(
                "[worker=%s region=%s] setup ok: nav_default=mouse-drag "
                "setView_opt=%s map_dims=%dx%d center_offset=%s source=%s "
                "coastline_calibration=%s coastline_available=%s dark=%s filter=%s",
                worker_id, region_name,
                USE_SETVIEW_OPTIMIZATION,
                int(map_dims["width"]), int(map_dims["height"]),
                "ok" if center_offset else "missing",
                center_offset.get("source") if center_offset else None,
                ENABLE_COASTLINE_CALIBRATION,
                coast_status.get("available"),
                dark_state,
                filter_state,
            )

            try:
                result = _capture_region_tiles(
                    region_name, config, timestamp_str, page, map_dims,
                )
                results[region_name] = result
            except Exception as e:
                retry_capture = (
                    _is_crash_error(e)
                )
                if retry_capture:
                    logger.error("[worker=%s region=%s] capture failed (retryable): %s",
                                 worker_id, region_name, e)
                    retryable.append(region_name)
                else:
                    logger.error("[worker=%s region=%s] capture failed: %s",
                                 worker_id, region_name, e)
                    _log_json(timestamp_str, region_name, "", 0, 0, 1, 0.0, 0, 0,
                              zoom, [])

            context.close()
            browser.close()

    except Exception as e:
        if _is_crash_error(e):
            logger.error("[worker=%s region=%s] worker crashed (retryable): %s",
                         worker_id, region_name, e)
            if region_name not in retryable and region_name not in results:
                retryable.append(region_name)
        else:
            logger.error("[worker=%s region=%s] worker failed: %s",
                         worker_id, region_name, e)
            qa_summary = _write_failure_qa_artifact(
                timestamp_str, region_name, config, "capture_failed", e
            )
            _log_json(
                timestamp_str, region_name, "", 0, 0, 1, 0.0, 0, 0,
                zoom, [], zoom_used=zoom,
                qa_flags=qa_summary.get("qa_flags", []),
                qa_confidence=qa_summary.get("qa_confidence"),
            )
            qa_path = qa_summary["path"]
            logger.info("[worker=%s region=%s] failure QA artifact=%s",
                        worker_id, region_name, qa_path)

    results["_retryable"] = retryable
    return results


# --- Single-region capture (backward-compatible wrapper) ----------------------


def capture_region(region_name, config, timestamp_str):
    """Capture tiles for a single region with its own browser."""
    res = capture_worker(region_name, timestamp_str)
    res.pop("_retryable", None)
    return res.get(region_name)


# --- Logging ------------------------------------------------------------------

_log_lock = threading.Lock()
_capture_log_path = Path("./data") / "captures_log.jsonl"


def _log_json(timestamp, region, filepath, total, ok, failed, size_kb,
              tankers=0, cargos=0, zoom=None, detections=None,
              moving_tankers=0, moving_cargos=0, markers=None,
              nav_mode=None, projection_mode=None, zoom_used=None,
              qa_flags=None, qa_confidence=None):
    """Append a single JSON line to captures_log.jsonl (thread-safe)."""
    Path("./data").mkdir(parents=True, exist_ok=True)

    if total == 0 and ok == 0:
        status = "error"
    elif failed == 0:
        status = "success"
    elif ok > 0:
        status = "partial"
    else:
        status = "error"

    entry = {
        "region": region,
        "region_name": REGIONS.get(region, {}).get("name", region),
        "filepath": filepath,
        "is_north": region == "N",
        "date_time": timestamp,
        "tiles_total": total,
        "tiles_ok": ok,
        "tiles_failed": failed,
        "zoom": zoom or REGIONS.get(region, {}).get("zoom", DEFAULT_ZOOM_LEVEL),
        "file_size_kb": round(size_kb, 1),
        "tankers": tankers,
        "cargos": cargos,
        "moving_tankers": moving_tankers,
        "moving_cargos": moving_cargos,
        "status": status,
        "markers": markers or [],
        "detections": detections or [],
        "nav_mode": nav_mode,
        "projection_mode": projection_mode,
        "zoom_used": zoom_used or zoom or REGIONS.get(region, {}).get("zoom", DEFAULT_ZOOM_LEVEL),
        "qa_flags": qa_flags or [],
        "qa_confidence": qa_confidence,
    }

    with _log_lock:
        with open(_capture_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    logger.info("_log_json: region=%s status=%s tankers=%d cargos=%d "
                "moving_t=%d moving_c=%d tiles=%d/%d markers=%d",
                region, status, tankers, cargos, moving_tankers, moving_cargos,
                ok, total, len(markers or []))


# --- Orchestration ------------------------------------------------------------


def capture_all_regions(region_filter=None, no_ingest=False):
    """Capture all (or filtered) regions in single-region-per-worker mode.

    Each region is submitted to a ThreadPoolExecutor as an atomic task; the
    worker opens a fresh browser, processes only that region, and tears the
    browser down. Concurrency is capped at MAX_BROWSERS. Regions are sorted
    largest-first so the tail of the run is dominated by smaller regions.
    """
    global _capture_log_path

    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    logger.info("Starting capture run: %s", timestamp_str)
    previous_log_path = _capture_log_path
    if no_ingest:
        _capture_log_path = Path("./data") / f"captures_validation_{timestamp_str}.jsonl"
        logger.info("Validation capture log: %s", _capture_log_path)

    # Determine which regions to capture
    if region_filter:
        names = [n for n in region_filter if n in ACTIVE_REGIONS]
    else:
        names = list(ACTIVE_REGIONS.keys())

    # Compute tile counts and sort largest-first (reduces tail latency)
    tile_counts = {}
    total_tiles = 0
    for name in names:
        config = ACTIVE_REGIONS[name]
        tiles, info = _get_tile_grid(name, config)
        tile_counts[name] = len(tiles)
        total_tiles += len(tiles)
        logger.info("  %s (%s): %d tiles, zoom %d class=%s mode=%s",
                    name, config.get("name", name), len(tiles), config["zoom"],
                    config.get("crowded_class"),
                    "bbox" if USE_BBOX_TILING and config.get("bbox") else "polygon")

    names.sort(key=lambda n: tile_counts[n], reverse=True)
    logger.info("Total: %d regions, %d tiles (sorted largest-first)", len(names), total_tiles)

    t_start = time.perf_counter()
    all_results = {}

    def _run_regions_parallel(region_names):
        """Submit one fresh-browser worker per region; cap concurrency at
        MAX_BROWSERS. Each region is an atomic task — workers never share
        state, never transition between regions."""
        worker_results_all = {}
        retry_names = []
        with ThreadPoolExecutor(max_workers=MAX_BROWSERS) as executor:
            futures = {
                executor.submit(capture_worker, name, timestamp_str): name
                for name in region_names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    worker_results = future.result() or {}
                    retry_names.extend(worker_results.pop("_retryable", []))
                    worker_results_all.update(worker_results)
                except Exception as e:
                    logger.error("Worker for region %s failed: %s", name, e)
        return worker_results_all, retry_names

    # Initial run
    results_batch, retryable = _run_regions_parallel(names)
    all_results.update(results_batch)

    # Retry crashed regions with fresh browsers
    for attempt in range(1, MAX_REGION_RETRIES + 1):
        if not retryable:
            break
        # De-duplicate and keep only regions not yet successfully captured
        retryable = list(dict.fromkeys(
            n for n in retryable if n not in all_results
        ))
        if not retryable:
            break

        backoff = RETRY_BACKOFF_BASE * attempt + random.uniform(0, 5)
        logger.info(
            "Retry attempt %d/%d for %d retryable region(s): %s  "
            "(backoff %.1fs)",
            attempt, MAX_REGION_RETRIES, len(retryable), retryable, backoff,
        )
        time.sleep(backoff)

        results_batch, retryable = _run_regions_parallel(retryable)
        all_results.update(results_batch)

    # Report regions that exhausted all retries
    still_failed = [n for n in retryable if n not in all_results] if retryable else []
    if still_failed:
        logger.error(
            "Regions failed after %d retries: %s",
            MAX_REGION_RETRIES, still_failed,
        )

    elapsed = time.perf_counter() - t_start
    per_tile = elapsed / total_tiles if total_tiles > 0 else 0

    # Summary — exclude internal keys like _retryable
    grand_tankers = sum(r.get("tankers", 0) for r in all_results.values()
                        if isinstance(r, dict))
    grand_cargos = sum(r.get("cargos", 0) for r in all_results.values()
                       if isinstance(r, dict))
    grand_mov_t = sum(r.get("moving_tankers", 0) for r in all_results.values()
                      if isinstance(r, dict))
    grand_mov_c = sum(r.get("moving_cargos", 0) for r in all_results.values()
                      if isinstance(r, dict))

    logger.info(
        "STOPWATCH  all regions: %.2fs total | %d tiles | %.2fs/tile | "
        "%d tankers (%d mov) | %d cargo (%d mov)",
        elapsed, total_tiles, per_tile,
        grand_tankers, grand_mov_t, grand_cargos, grand_mov_c,
    )

    # Flush captures_log.jsonl into PostgreSQL unless this is a validation run.
    if no_ingest:
        logger.info("Database ingestion skipped (--no-ingest)")
    else:
        try:
            logger.info("Ingesting captures log into database...")
            process_log()
            logger.info("Database ingestion complete")
        except Exception as e:
            logger.error("Database ingestion failed: %s (data preserved in captures_log.jsonl)", e)

    if no_ingest:
        _capture_log_path = previous_log_path

    return all_results


def scheduled_run():
    jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
    delay = max(0, jitter)
    if delay > 0:
        logger.info("Jitter: waiting %.0fs before run", delay)
        time.sleep(delay)
    capture_all_regions()


def main():
    global geo_profiles

    # Parse CLI flags
    region_filter = None
    zoom_filter = None
    tier_filter = None
    run_once = False
    no_ingest = False
    for arg in sys.argv[1:]:
        if arg.startswith("--regions="):
            region_filter = arg.split("=", 1)[1].split(",")
        elif arg.startswith("--zoom="):
            zoom_filter = [int(z) for z in arg.split("=", 1)[1].split(",")]
        elif arg.startswith("--tier="):
            tier_filter = arg.split("=", 1)[1].split(",")
        elif arg == "--save-images":
            pass  # Already handled at module level via SAVE_IMAGES
        elif arg == "--once":
            run_once = True
        elif arg == "--no-ingest":
            no_ingest = True
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

    # Apply --zoom and --tier filters to build region_filter
    if zoom_filter or tier_filter:
        filtered = set()
        for code, config in ACTIVE_REGIONS.items():
            zoom_ok = zoom_filter is None or config["zoom"] in zoom_filter
            tier_ok = tier_filter is None or REGION_TIERS.get(code) in tier_filter
            if zoom_ok and tier_ok:
                filtered.add(code)
        # Intersect with --regions if both specified
        if region_filter:
            filtered &= set(region_filter)
        region_filter = list(filtered) if filtered else ["__none__"]

    # Resolve proxy geolocations at startup
    logger.info("Resolving proxy geolocations...")
    geo_profiles = resolve_all_proxies(proxies)
    logger.info("Resolved %d/%d proxy profiles", len(geo_profiles), len(proxies))

    # Log region summary by zoom level
    zoom_groups = {}
    for name, config in ACTIVE_REGIONS.items():
        z = config["zoom"]
        zoom_groups.setdefault(z, []).append(name)

    for z in sorted(zoom_groups.keys()):
        regions_at_z = zoom_groups[z]
        logger.info("Zoom %d: %d regions (%s)", z, len(regions_at_z),
                    ", ".join(regions_at_z))

    total_tiles = 0
    for name in (region_filter or ACTIVE_REGIONS.keys()):
        if name not in ACTIVE_REGIONS:
            continue
        config = ACTIVE_REGIONS[name]
        tiles, info = _get_tile_grid(name, config)
        total_tiles += len(tiles)

    active_count = len(region_filter) if region_filter else len(ACTIVE_REGIONS)
    logger.info("Total: %d regions, %d tiles | Viewport: %dx%d | "
                "Max browsers: %d | Save images: %s | bbox=%s | cross_zoom_qa=%s "
                "| coastline_calibration=%s | no_ingest=%s",
                active_count, total_tiles, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
                MAX_BROWSERS, SAVE_IMAGES, USE_BBOX_TILING,
                ENABLE_CROSS_ZOOM_QA, ENABLE_COASTLINE_CALIBRATION, no_ingest)

    # Run once immediately
    capture_all_regions(region_filter, no_ingest=no_ingest)
    if run_once:
        return

    # Schedule future runs
    sched.every(SCRAPE_INTERVAL_MINUTES).minutes.do(scheduled_run)
    while True:
        sched.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
