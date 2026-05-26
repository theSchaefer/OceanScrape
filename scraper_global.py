#!/bin/python3
"""Patchright scraper — global maritime chokepoint monitor.

Launches MAX_BROWSERS concurrent browser workers, each pulling regions from
a shared queue (work-stealing).  Each worker loads MarineTraffic once, then
pans via Leaflet setView() for all subsequent regions and tiles.  Supports
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
import io
import json
import logging
import os
import platform
import random
import string
import queue
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

from geo_profile import GeoProfile, resolve_all_proxies, EGYPT_FALLBACK_DATA
from grid import get_tile_centers, polygon_to_pixel_coords, lat_to_pixel_y, _point_in_polygon
from regions import REGIONS, REGION_TIERS
from update_database import process_log

load_dotenv()

# --- Configuration -----------------------------------------------------------

DEFAULT_ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "13"))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "3840"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "2160"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR_PATCHRIGHT_PAN", "./data/captures")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "120"))
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "300"))

# Max concurrent browser workers (each pulls regions from a shared queue)
MAX_BROWSERS = int(os.getenv("MAX_BROWSERS", "2"))
# Save images to disk (default: only counts are kept)
SAVE_IMAGES = os.getenv("SAVE_IMAGES", "0") == "1" or "--save-images" in sys.argv
# Screenshot format: jpeg is ~5x smaller and ~2x faster to encode than png
SCREENSHOT_FORMAT = os.getenv("SCREENSHOT_FORMAT", "jpeg")
SCREENSHOT_QUALITY = int(os.getenv("SCREENSHOT_QUALITY", "70"))

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


# --- Canvas readiness probe (shared between init + capture) ------------------

_CANVAS_READY_JS = """
() => {
    const mc = document.getElementById('map_canvas');
    if (!mc) return 'canvas_missing';
    const rect = mc.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return 'canvas_zero_size';
    const overlay = document.querySelector('.leaflet-overlay-pane');
    if (!overlay) return 'overlay_pane_missing';
    return 'ok';
}
"""


# --- Tile grid cache ----------------------------------------------------------
# Polygons, zoom levels, and viewport dims are constant across scrape cycles,
# so tile grids only need to be computed once per region.

_tile_grid_cache = {}


def _get_tile_grid(region_name, config):
    """Return (tiles, grid_info) for a region, computing only on first call."""
    if region_name not in _tile_grid_cache:
        _tile_grid_cache[region_name] = get_tile_centers(
            config["polygon"], config["zoom"], VIEWPORT_WIDTH, VIEWPORT_HEIGHT
        )
    return _tile_grid_cache[region_name]


# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

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
    "fishing", "pleasure", "navigation aid", "unspecified",
)
_KEEP_VESSEL_LABELS = ("cargo", "tanker")


def set_vessel_filter(page):
    """Open MarineTraffic's vessel-type filter and uncheck everything except
    cargo and tankers. Best-effort — never raises; logs detailed status so a
    silent regression (e.g. filter UI selectors changed) is visible.
    """
    # page.evaluate's second argument is forwarded as the JS function's only
    # parameter — cleaner than f-string interpolation because we don't have
    # to double-escape JS braces. The dict is JSON-serialized by Playwright
    # and destructured by the async arrow on the JS side.
    try:
        result = page.evaluate(
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
    """Hook Leaflet.Map to disable inertia and capture the map instance."""
    page.add_init_script("""
    (function() {
        window.__mtMap = null;

        function patchLeaflet(L) {
            if (!L || !L.Map) return;
            if (L.Map.mergeOptions) {
                L.Map.mergeOptions({
                    inertia: false,
                    inertiaDeceleration: 99999,
                    inertiaMaxSpeed: 0,
                });
            }
            var _origInit = L.Map.prototype.initialize;
            L.Map.prototype.initialize = function() {
                _origInit.apply(this, arguments);
                window.__mtMap = this;
            };
        }

        var _L = window.L;
        if (_L) patchLeaflet(_L);
        Object.defineProperty(window, 'L', {
            get: function() { return _L; },
            set: function(v) {
                _L = v;
                patchLeaflet(v);
            },
            configurable: true,
        });
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


# --- Mouse-drag map panning ---------------------------------------------------


MAX_DRAG_PX = 800


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
        const center = map.getCenter();
        const centerPt = map.latLngToContainerPoint(center);
        const mapEl = map.getContainer();
        const mapRect = mapEl.getBoundingClientRect();
        const canvas = document.getElementById('map_canvas');
        const canvasRect = canvas.getBoundingClientRect();
        return {
            center_x: centerPt.x + (mapRect.x - canvasRect.x),
            center_y: centerPt.y + (mapRect.y - canvasRect.y),
            map_lat: center.lat,
            map_lng: center.lng,
            map_zoom: map.getZoom(),
            dpr: window.devicePixelRatio || 1
        };
    }
    """)


def _pan_map_js(page, target_lat, target_lon, zoom):
    """Pan map using Leaflet's setView API — pixel-perfect positioning."""
    return page.evaluate(f"""
    () => {{
        const map = window.__mtMap;
        if (!map || !map.setView) return false;
        map.setView([{target_lat}, {target_lon}], {zoom}, {{animate: false}});
        return true;
    }}
    """)


def _pan_map(page, cur_lat, cur_lon, target_lat, target_lon, zoom,
             map_center=None, timeout_ms=5000):
    """Pan the map.  JS setView first, mouse-drag fallback."""

    if _pan_map_js(page, target_lat, target_lon, zoom):
        logger.info("  Panned via setView (%.5f, %.5f)", target_lat, target_lon)
        time.sleep(0.05)
        _wait_for_tiles_after_pan(page, timeout_ms)
        _wait_for_ais_markers(page, timeout_ms=2000)
        return True

    # --- Mouse drag fallback ---
    total_pixels = 256 * (2 ** zoom)
    dx = (target_lon - cur_lon) * total_pixels / 360.0
    dy = lat_to_pixel_y(target_lat, zoom) - lat_to_pixel_y(cur_lat, zoom)
    drag_x = -dx
    drag_y = -dy

    if abs(drag_x) < 1 and abs(drag_y) < 1:
        return True

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
    return True


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
    counts = {
        "stationary_tankers": 0, "moving_tankers": 0,
        "stationary_cargos": 0, "moving_cargos": 0,
    }
    for m in filtered:
        key = ("moving_" if m["motion"] == "moving" else "stationary_") + \
              ("tankers" if m["type"] == "tanker" else "cargos")
        counts[key] += 1
    return counts, filtered


# --- Leaflet map discovery ----------------------------------------------------


def _discover_leaflet_map(page):
    """Try to find the Leaflet map instance if the init hook missed it."""
    return page.evaluate("""
    () => {
        if (window.__mtMap) return true;
        const containers = document.querySelectorAll('.leaflet-container');
        for (const c of containers) {
            for (const key of Object.keys(c)) {
                const val = c[key];
                if (val && typeof val === 'object'
                    && typeof val.setView === 'function'
                    && typeof val.getCenter === 'function') {
                    window.__mtMap = val;
                    val.options.inertia = false;
                    val.options.inertiaDeceleration = 99999;
                    val.options.inertiaMaxSpeed = 0;
                    return true;
                }
            }
        }
        return false;
    }
    """)


# --- Filter-state probe ------------------------------------------------------


def _probe_vessel_filter_state(page):
    """Cheap, read-only check of MarineTraffic's vessel-filter panel state.

    Returns ``{"regressed": bool, "still_checked_drops": list[str]}``.
    ``regressed=True`` means at least one label in ``_DROP_VESSEL_LABELS`` is
    currently checked — i.e. the filter is no longer suppressing it, and the
    OpenCV pipeline would see overlapping markers again. On any error, returns
    ``regressed=True`` so the caller re-applies (fail-safe).
    """
    try:
        return page.evaluate(
            """
            ({dropLabels}) => {
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

                const inputs = [...document.querySelectorAll(
                    'input[type="checkbox"], input[type="radio"], ' +
                    '[role="checkbox"], [role="switch"]'
                )].filter(visible);

                const stillChecked = new Set();
                for (const input of inputs) {
                    let labelText = '';
                    let el = input;
                    for (let i = 0; i < 5 && el; i++) {
                        const t = textOf(el);
                        if (dropLabels.some(l => t.includes(l))) {
                            labelText = t; break;
                        }
                        el = el.parentElement;
                    }
                    if (!labelText && input.id) {
                        const lbl = document.querySelector(
                            'label[for="' + CSS.escape(input.id) + '"]');
                        if (lbl) {
                            const t = textOf(lbl);
                            if (dropLabels.some(l => t.includes(l))) labelText = t;
                        }
                    }
                    if (!labelText) continue;
                    const drop = dropLabels.find(l => labelText.includes(l));
                    if (drop && isChecked(input)) stillChecked.add(drop);
                }
                return {still_checked_drops: [...stillChecked]};
            }
            """,
            {"dropLabels": list(_DROP_VESSEL_LABELS)},
        ) or {"still_checked_drops": []}
    except Exception as e:
        logger.debug("  Filter probe failed: %s (will treat as regressed)", e)
        return {"still_checked_drops": list(_DROP_VESSEL_LABELS), "_probe_error": str(e)}


# --- Per-region init / re-validation -----------------------------------------


def _per_region_init(page, region_name, region_idx):
    """Validate and refresh per-region browser state before capture.

    Runs idempotent setup steps (Leaflet handle, tile-wait for stolen regions,
    filter check + repair-if-regressed, overlay hide, canvas readiness, fresh
    map_dims + center_offset). Returns a status dict; the caller treats
    ``ok=False`` as a retryable failure rather than ingesting bad data.
    """
    t0 = time.perf_counter()
    status = {
        "ok": False,
        "region": region_name,
        "idx": region_idx,
        "reason": None,
        "crash": False,
        "map_dims": None,
        "center_offset": None,
        "dpr": None,
        "filter_regressed": False,
        "filter_repaired": False,
        "filter_still_checked": [],
        "init_ms": 0,
    }

    try:
        # 1. Leaflet handle (cheap, idempotent — short-circuits if already set)
        _discover_leaflet_map(page)

        # 2. Stolen-region tile wait. First region's pre-init flow (page.goto
        #    + Cloudflare) already gates on this, so skip to save ~250 ms.
        if region_idx > 0:
            wait_for_map_tiles(page, timeout_ms=4000)

        # 3. Filter check (cheap probe) + repair only if regressed
        probe = _probe_vessel_filter_state(page)
        still_checked = probe.get("still_checked_drops", []) or []
        status["filter_still_checked"] = still_checked
        if still_checked:
            status["filter_regressed"] = True
            ok = set_vessel_filter(page)
            status["filter_repaired"] = bool(ok)

        # 4. Re-hide overlays — idempotent DOM mutation
        hide_ui_overlays(page)

        # 5. Canvas + overlay-pane readiness
        canvas_state = page.evaluate(_CANVAS_READY_JS)
        if canvas_state != "ok":
            status["reason"] = f"canvas:{canvas_state}"
            return status

        # 6. Fresh map dimensions and Leaflet centre anchor
        map_dims = _get_map_dimensions(page)
        center_offset = _get_map_center_offset(page)
        status["map_dims"] = map_dims
        status["center_offset"] = center_offset
        if center_offset is None:
            status["reason"] = "no_center_offset"
            return status

        status["dpr"] = center_offset.get("dpr")
        status["ok"] = True
        return status

    except Exception as e:
        if _is_crash_error(e):
            status["reason"] = f"crash:{e}"
            status["crash"] = True
        else:
            status["reason"] = f"error:{e}"
        return status

    finally:
        status["init_ms"] = int((time.perf_counter() - t0) * 1000)
        dims = status.get("map_dims") or {}
        dim_str = (f"{int(dims['width'])}x{int(dims['height'])}"
                   if dims.get("width") and dims.get("height") else "?")
        logger.info(
            "[region %s | idx=%d] init: %s, filter_regressed=%s, "
            "filter_repaired=%s, dims=%s, dpr=%s, %dms",
            region_name, region_idx,
            "ok" if status["ok"] else f"FAIL({status['reason']})",
            status["filter_regressed"], status["filter_repaired"],
            dim_str, status["dpr"], status["init_ms"],
        )


# --- Core capture (single region, given a page) ------------------------------


def _capture_region_tiles(region_name, config, timestamp_str, page, map_dims,
                          region_idx=0):
    """Capture all tiles for a region using an already-setup page.

    Returns dict with capture results: tankers, cargos, markers, file paths.
    """
    polygon = config["polygon"]
    zoom = config["zoom"]
    region_display = config.get("name", region_name)

    map_width = int(map_dims["width"])
    map_height = int(map_dims["height"])
    map_cx = int(map_dims["x"]) + map_width // 2
    map_cy = int(map_dims["y"]) + map_height // 2

    tiles, grid_info = _get_tile_grid(region_name, config)
    n_rows = grid_info["n_rows"]
    n_cols = grid_info["n_cols"]
    logger.info("Region %s (%s | idx=%d): %d tiles (%dx%d), zoom %d",
                region_name, region_display, region_idx,
                len(tiles), n_rows, n_cols, zoom)

    tile_images = {}
    tile_detections = []
    all_markers = []
    tiles_ok = 0
    tiles_failed = 0
    total_tankers = 0
    total_cargos = 0
    total_moving_tankers = 0
    total_moving_cargos = 0

    center_lat, center_lon = _polygon_center(polygon)
    current_lat, current_lon = center_lat, center_lon
    map_locator = page.locator('#map_canvas')

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
    first_row, first_col, first_lat, first_lon = tiles[0]
    _pan_map(page, center_lat, center_lon, first_lat, first_lon, zoom,
             map_center=(map_cx, map_cy))
    current_lat, current_lon = first_lat, first_lon

    # Capture tiles
    for i, (row, col, lat, lon) in enumerate(tiles):
        if i > 0:
            _pan_map(page, current_lat, current_lon, lat, lon, zoom,
                     map_center=(map_cx, map_cy))
            current_lat, current_lon = lat, lon

        try:
            screenshot_args = {"type": SCREENSHOT_FORMAT}
            if SCREENSHOT_FORMAT == "jpeg":
                screenshot_args["quality"] = SCREENSHOT_QUALITY
            img_bytes = map_locator.screenshot(**screenshot_args)

            # Query actual map-centre pixel (accounts for UI chrome + DPR)
            center_offset = _get_map_center_offset(page)

            # Inline ship detection + geo-coordinate extraction.
            # Anchor the projection on the *actual* map state read from
            # Leaflet (map.getCenter() / map.getZoom()), not the requested
            # setView arguments. Guards against MarineTraffic rounding or
            # clamping our pan/zoom calls.
            proj_lat = center_offset["map_lat"] if center_offset else lat
            proj_lon = center_offset["map_lng"] if center_offset else lon
            proj_zoom = (center_offset.get("map_zoom") if center_offset
                         else zoom) or zoom
            logger.debug("  Tile (%d,%d): running OpenCV detection on %d bytes",
                         row, col, len(img_bytes))
            det, tile_markers, img_shape = _detect_ships_inline(
                img_bytes, proj_lat, proj_lon, proj_zoom,
                map_width, map_height, center_offset=center_offset
            )

            # Filter markers to region polygon boundary
            raw_count = len(tile_markers)
            det, tile_markers = _filter_markers_to_polygon(tile_markers, polygon)
            if raw_count != len(tile_markers):
                logger.debug("  Tile (%d,%d): geo-filtered %d → %d markers",
                             row, col, raw_count, len(tile_markers))

            logger.debug("  Tile (%d,%d): detection result: %s, %d markers",
                         row, col, det, len(tile_markers))

            # Debug: warn if Leaflet's actual center/zoom drifted from the
            # requested setView arguments (would indicate MarineTraffic is
            # rounding, clamping, or otherwise mutating our pan calls).
            if center_offset:
                from seer import _debug_center_check
                _debug_center_check(lat, lon, center_offset, row, col, logger)
                act_zoom = center_offset.get("map_zoom")
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
                "tile": [row, col],
                "center_lat": lat,
                "center_lon": lon,
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
                    "act_lat": center_offset.get("map_lat") if center_offset else None,
                    "act_lon": center_offset.get("map_lng") if center_offset else None,
                    "act_zoom": center_offset.get("map_zoom") if center_offset else None,
                    "dpr": center_offset.get("dpr") if center_offset else None,
                    "img_h": int(img_shape[0]) if img_shape else None,
                    "img_w": int(img_shape[1]) if img_shape else None,
                    "center_x": center_offset.get("center_x") if center_offset else None,
                    "center_y": center_offset.get("center_y") if center_offset else None,
                },
            })

            if SAVE_IMAGES:
                tile_images[(row, col)] = img_bytes

            tiles_ok += 1
            logger.info("  Tile (%d,%d) [%d/%d]: %d tankers (%d mov), %d cargo (%d mov)",
                        row, col, i + 1, len(tiles), tankers, mt, cargos, mc)

        except Exception as e:
            logger.error("  Tile (%d,%d) failed: %s", row, col, e)
            tiles_failed += 1

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

        # Mask to polygon
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

        if SCREENSHOT_FORMAT == "jpeg":
            result.save(str(output_path), quality=SCREENSHOT_QUALITY)
        else:
            result.save(str(output_path))

        file_size_kb = output_path.stat().st_size / 1024
        saved_path = str(output_path)
        logger.info("Region %s: saved %s (%.1f KB)", region_name, filename, file_size_kb)

    # --- Log results ----------------------------------------------------------
    _log_json(
        timestamp_str, region_name, saved_path,
        len(tiles), tiles_ok, tiles_failed, file_size_kb,
        total_tankers, total_cargos, zoom, tile_detections,
        moving_tankers=total_moving_tankers,
        moving_cargos=total_moving_cargos,
        markers=all_markers,
        region_idx=region_idx,
    )

    logger.info("Region %s (idx=%d): %d tankers (%d mov), %d cargo (%d mov) (from %d tiles)",
                region_name, region_idx, total_tankers, total_moving_tankers,
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
    }


# --- Work-stealing browser workers -------------------------------------------


def capture_batch(region_batch, timestamp_str):
    """Capture a fixed batch of regions (legacy interface for retries).

    Wraps capture_worker by feeding the batch into a queue.
    """
    q = queue.Queue()
    for name in region_batch:
        q.put(name)
    return capture_worker(q, timestamp_str)


def capture_worker(region_queue, timestamp_str):
    """Work-stealing worker: opens one browser, pulls regions from a shared
    queue until it is empty.

    The first region triggers a full page.goto() + Cloudflare setup.
    Subsequent regions reuse the same browser session via Leaflet setView()
    panning — no extra page loads, no repeated Cloudflare challenges.
    """
    proxy = random.choice(proxies)
    fallback = GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)
    geo = geo_profiles.get(proxy["server"], fallback)

    results = {}
    retryable = []  # regions that failed due to browser/driver crashes

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

            # Pull first region for initial page load + Cloudflare setup
            try:
                first_name = region_queue.get_nowait()
            except queue.Empty:
                context.close()
                browser.close()
                results["_retryable"] = retryable
                return results

            config = REGIONS[first_name]
            page = context.new_page()
            _inject_stealth_scripts(page, geo)
            _inject_map_hooks(page)

            center_lat, center_lon = _polygon_center(config["polygon"])
            url = build_url(center_lat, center_lon, config["zoom"])
            logger.info("  [worker] Loading %s (%s) at zoom %d",
                        first_name, config.get("name", first_name), config["zoom"])
            page.goto(url, wait_until="domcontentloaded")

            # One-shot setup: Cloudflare + cookie banner. Per-region validation
            # (filter, overlays, Leaflet handle, map_dims) is delegated to
            # _per_region_init below so the stolen-region path gets the same
            # treatment as the first one.
            setup_failed_retryable = False
            try:
                if _wait_for_cloudflare(page):
                    logger.info("  [%s] Cloudflare passed", first_name)
                elif _is_cloudflare_blocked(page):
                    logger.error("  [%s] Cloudflare block — marking retryable",
                                 first_name)
                    setup_failed_retryable = True
                if not setup_failed_retryable:
                    dismiss_cookie_banner(page)
            except Exception as e:
                if _is_crash_error(e):
                    logger.error("  [%s] Setup crashed (retryable): %s",
                                 first_name, e)
                else:
                    logger.error("  [%s] Setup failed (retryable): %s",
                                 first_name, e)
                setup_failed_retryable = True

            if setup_failed_retryable:
                retryable.append(first_name)
                context.close()
                browser.close()
                # Put remaining regions back as retryable so they aren't lost
                while True:
                    try:
                        retryable.append(region_queue.get_nowait())
                    except queue.Empty:
                        break
                results["_retryable"] = retryable
                return results

            # Per-region init for the first region (idx=0).
            init = _per_region_init(page, first_name, region_idx=0)
            if not init["ok"]:
                logger.warning("Region %s init failed: %s — marking retryable",
                               first_name, init["reason"])
                retryable.append(first_name)
                if init.get("crash"):
                    while True:
                        try:
                            retryable.append(region_queue.get_nowait())
                        except queue.Empty:
                            break
                context.close()
                browser.close()
                results["_retryable"] = retryable
                return results

            # Capture first region
            try:
                result = _capture_region_tiles(
                    first_name, REGIONS[first_name], timestamp_str, page,
                    init["map_dims"], region_idx=0,
                )
                results[first_name] = result
            except Exception as e:
                if _is_crash_error(e):
                    logger.error("Region %s crashed (retryable): %s", first_name, e)
                    retryable.append(first_name)
                    # Browser crashed — drain queue into retryable
                    while True:
                        try:
                            retryable.append(region_queue.get_nowait())
                        except queue.Empty:
                            break
                    context.close()
                    browser.close()
                    results["_retryable"] = retryable
                    return results
                else:
                    logger.error("Region %s capture failed: %s", first_name, e)
                    _log_json(timestamp_str, first_name, "", 0, 0, 1, 0.0, 0, 0,
                              REGIONS[first_name]["zoom"], [], region_idx=0)

            # Work-stealing loop: pull more regions from the shared queue.
            # Each stolen region gets its own _per_region_init pass — if it
            # fails the region is marked retryable rather than ingested with
            # corrupted data.
            region_idx = 1
            while True:
                try:
                    name = region_queue.get_nowait()
                except queue.Empty:
                    break

                logger.info("  [worker] Stealing region %s (%s) [idx=%d]",
                            name, REGIONS[name].get("name", name), region_idx)

                init = _per_region_init(page, name, region_idx=region_idx)
                if not init["ok"]:
                    logger.warning("Region %s init failed: %s — retryable",
                                   name, init["reason"])
                    retryable.append(name)
                    if init.get("crash"):
                        # Browser-level crash — drain queue and abandon worker
                        while True:
                            try:
                                retryable.append(region_queue.get_nowait())
                            except queue.Empty:
                                break
                        break
                    region_idx += 1
                    continue

                try:
                    result = _capture_region_tiles(
                        name, REGIONS[name], timestamp_str, page,
                        init["map_dims"], region_idx=region_idx,
                    )
                    results[name] = result
                except Exception as e:
                    if _is_crash_error(e):
                        logger.error("Region %s crashed (retryable): %s", name, e)
                        retryable.append(name)
                        # Browser crashed — drain remaining queue
                        while True:
                            try:
                                retryable.append(region_queue.get_nowait())
                            except queue.Empty:
                                break
                        break
                    else:
                        logger.error("Region %s capture failed: %s", name, e)
                        _log_json(timestamp_str, name, "", 0, 0, 1, 0.0, 0, 0,
                                  REGIONS[name]["zoom"], [],
                                  region_idx=region_idx)
                region_idx += 1

            context.close()
            browser.close()

    except Exception as e:
        if _is_crash_error(e):
            logger.error("Worker crashed (retryable): %s", e)
            # Drain queue into retryable
            while True:
                try:
                    retryable.append(region_queue.get_nowait())
                except queue.Empty:
                    break
        else:
            logger.error("Worker failed: %s", e)

    results["_retryable"] = retryable
    return results


# --- Single-region capture (backward-compatible wrapper) ----------------------


def capture_region(region_name, config, timestamp_str):
    """Capture tiles for a single region with its own browser."""
    return capture_batch([region_name], timestamp_str).get(region_name)


# --- Logging ------------------------------------------------------------------

_log_lock = threading.Lock()


def _log_json(timestamp, region, filepath, total, ok, failed, size_kb,
              tankers=0, cargos=0, zoom=None, detections=None,
              moving_tankers=0, moving_cargos=0, markers=None,
              region_idx=None):
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
    }
    if region_idx is not None:
        entry["region_idx"] = region_idx

    jsonl_path = Path("./data") / "captures_log.jsonl"
    with _log_lock:
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    logger.info("_log_json: region=%s status=%s tankers=%d cargos=%d "
                "moving_t=%d moving_c=%d tiles=%d/%d markers=%d",
                region, status, tankers, cargos, moving_tankers, moving_cargos,
                ok, total, len(markers or []))


# --- Orchestration ------------------------------------------------------------


def capture_all_regions(region_filter=None):
    """Capture all (or filtered) regions using work-stealing parallelism.

    Regions are sorted largest-first (most tiles) and placed in a shared
    queue.  Up to MAX_BROWSERS worker threads each open a browser and pull
    regions from the queue until it is empty — no idle tabs.
    """
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    logger.info("Starting capture run: %s", timestamp_str)

    # Determine which regions to capture
    if region_filter:
        names = [n for n in region_filter if n in REGIONS]
    else:
        names = list(REGIONS.keys())

    # Compute tile counts and sort largest-first (reduces tail latency)
    tile_counts = {}
    total_tiles = 0
    for name in names:
        config = REGIONS[name]
        tiles, info = _get_tile_grid(name, config)
        tile_counts[name] = len(tiles)
        total_tiles += len(tiles)
        logger.info("  %s (%s): %d tiles, zoom %d",
                    name, config.get("name", name), len(tiles), config["zoom"])

    names.sort(key=lambda n: tile_counts[n], reverse=True)
    logger.info("Total: %d regions, %d tiles (sorted largest-first)", len(names), total_tiles)

    t_start = time.perf_counter()
    all_results = {}

    def _run_with_queue(region_names):
        """Populate a shared queue and run MAX_BROWSERS workers."""
        region_q = queue.Queue()
        for n in region_names:
            region_q.put(n)

        worker_results_all = {}
        retry_names = []
        with ThreadPoolExecutor(max_workers=MAX_BROWSERS) as executor:
            futures = {
                executor.submit(capture_worker, region_q, timestamp_str): i
                for i in range(min(MAX_BROWSERS, len(region_names)))
            }
            for future in as_completed(futures):
                try:
                    worker_results = future.result() or {}
                    retry_names.extend(worker_results.pop("_retryable", []))
                    worker_results_all.update(worker_results)
                except Exception as e:
                    logger.error("Worker failed: %s", e)
        return worker_results_all, retry_names

    # Initial run
    results_batch, retryable = _run_with_queue(names)
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
            "Retry attempt %d/%d for %d crashed region(s): %s  "
            "(backoff %.1fs)",
            attempt, MAX_REGION_RETRIES, len(retryable), retryable, backoff,
        )
        time.sleep(backoff)

        results_batch, retryable = _run_with_queue(retryable)
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

    # Flush captures_log.jsonl into PostgreSQL
    try:
        logger.info("Ingesting captures log into database...")
        process_log()
        logger.info("Database ingestion complete")
    except Exception as e:
        logger.error("Database ingestion failed: %s (data preserved in captures_log.jsonl)", e)

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
    for arg in sys.argv[1:]:
        if arg.startswith("--regions="):
            region_filter = arg.split("=", 1)[1].split(",")
        elif arg.startswith("--zoom="):
            zoom_filter = [int(z) for z in arg.split("=", 1)[1].split(",")]
        elif arg.startswith("--tier="):
            tier_filter = arg.split("=", 1)[1].split(",")
        elif arg == "--save-images":
            pass  # Already handled at module level via SAVE_IMAGES
        elif arg == "--list-regions":
            print(f"{'Key':<6} {'Zoom':<5} {'Tier':<10} {'Name'}")
            print("-" * 65)
            for key, config in sorted(REGIONS.items()):
                tier = REGION_TIERS.get(key, "?")
                print(f"{key:<6} z{config['zoom']:<4} {tier:<10} {config.get('name', key)}")
            return
        elif arg == "--help":
            print(__doc__)
            return

    # Apply --zoom and --tier filters to build region_filter
    if zoom_filter or tier_filter:
        filtered = set()
        for code, config in REGIONS.items():
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
    for name, config in REGIONS.items():
        z = config["zoom"]
        zoom_groups.setdefault(z, []).append(name)

    for z in sorted(zoom_groups.keys()):
        regions_at_z = zoom_groups[z]
        logger.info("Zoom %d: %d regions (%s)", z, len(regions_at_z),
                    ", ".join(regions_at_z))

    total_tiles = 0
    for name in (region_filter or REGIONS.keys()):
        if name not in REGIONS:
            continue
        config = REGIONS[name]
        tiles, info = _get_tile_grid(name, config)
        total_tiles += len(tiles)

    active_count = len(region_filter) if region_filter else len(REGIONS)
    logger.info("Total: %d regions, %d tiles | Viewport: %dx%d | "
                "Max browsers: %d | Save images: %s",
                active_count, total_tiles, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
                MAX_BROWSERS, SAVE_IMAGES)

    # Run once immediately
    capture_all_regions(region_filter)

    # Schedule future runs
    sched.every(SCRAPE_INTERVAL_MINUTES).minutes.do(scheduled_run)
    while True:
        sched.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
