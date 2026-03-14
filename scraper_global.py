#!/bin/python3
"""Patchright scraper — global maritime chokepoint monitor.

Loads MarineTraffic once per browser tab, then pans via Leaflet setView()
for each subsequent tile.  Supports per-region zoom levels, tab-based
parallelism (multiple regions per browser), inline OpenCV ship detection,
and JPEG output for minimal storage.

Usage:
  python scraper_global.py                  # Run all regions
  python scraper_global.py --save-images    # Also save tile images
  python scraper_global.py --regions N,S,H  # Run specific regions only
  python scraper_global.py --list-regions    # Show all defined regions
"""

import base64
import io
import json
import logging
import os
import random
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import schedule as sched
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from patchright.sync_api import sync_playwright

from geo_profile import GeoProfile, resolve_all_proxies, EGYPT_FALLBACK_DATA
from grid import get_tile_centers, polygon_to_pixel_coords, lat_to_pixel_y

load_dotenv()

# --- Configuration -----------------------------------------------------------

DEFAULT_ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "13"))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "7680"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "4320"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR_PATCHRIGHT_PAN", "./captures_patchright_pan")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "300"))

# Tab parallelism: how many regions to load as tabs in a single browser
TABS_PER_BROWSER = int(os.getenv("TABS_PER_BROWSER", "4"))
# Max concurrent browser processes
MAX_BROWSERS = int(os.getenv("MAX_BROWSERS", "4"))
# Save images to disk (default: only counts are kept)
SAVE_IMAGES = os.getenv("SAVE_IMAGES", "0") == "1" or "--save-images" in sys.argv
# Screenshot format: jpeg is ~5x smaller and ~2x faster to encode than png
SCREENSHOT_FORMAT = os.getenv("SCREENSHOT_FORMAT", "jpeg")
SCREENSHOT_QUALITY = int(os.getenv("SCREENSHOT_QUALITY", "85"))


def _parse_polygon(env_key, default):
    """Parse 'lat,lon;lat,lon;...' into list of (lat, lon) tuples."""
    raw = os.getenv(env_key)
    if not raw:
        return default
    points = []
    for pair in raw.split(";"):
        lat_s, lon_s = pair.strip().split(",")
        points.append((float(lat_s), float(lon_s)))
    return points


# ---------------------------------------------------------------------------
# Region definitions — per-region zoom, polygon, and human-readable name.
#
# Zoom strategy:
#   13 = narrow canals / extremely dense straits (individual ships must be
#         distinguishable — e.g. Malacca, Suez, Panama, Bosporus)
#   12 = dense chokepoints where markers may overlap at lower zoom
#   11 = wide straits with moderate traffic
#   10 = regional corridors (large areas, moderate density)
#    9 = open ocean shipping lanes (sparse traffic, huge coverage per tile)
# ---------------------------------------------------------------------------

REGIONS = {
    # ── Zoom 13: Narrow / extremely dense ────────────────────────────────
    "N": {
        "polygon": _parse_polygon("NORTH_POLYGON", [
            (31.575, 31.91), (31.77435, 32.27517),
            (31.31, 32.27036), (31.517, 32.5445),
        ]),
        "zoom": 13,
        "name": "Suez Canal North",
    },
    "S": {
        "polygon": _parse_polygon("SOUTH_POLYGON", [
            (29.865656, 32.481079), (29.900187, 32.598495),
            (29.657029, 32.57515), (29.702964, 32.715225),
        ]),
        "zoom": 13,
        "name": "Suez Canal South",
    },
    "P": {
        "polygon": _parse_polygon("PANAMA_POLYGON", [
            (9.45, -79.95), (9.45, -79.50),
            (8.85, -79.50), (8.85, -79.95),
        ]),
        "zoom": 13,
        "name": "Panama Canal",
    },
    "M": {
        "polygon": _parse_polygon("MALACCA_POLYGON", [
            (1.60, 103.30), (1.60, 104.10),
            (1.05, 104.10), (1.05, 103.30),
        ]),
        "zoom": 13,
        "name": "Strait of Malacca",
    },
    "BO": {
        "polygon": _parse_polygon("BOSPORUS_POLYGON", [
            (41.25, 28.90), (41.25, 29.20),
            (40.95, 29.20), (40.95, 28.90),
        ]),
        "zoom": 13,
        "name": "Bosporus",
    },

    # ── Zoom 12: Dense chokepoints ───────────────────────────────────────
    "H": {
        "polygon": _parse_polygon("HORMUZ_POLYGON", [
            (26.410, 56.250), (26.110, 57.100),
            (24.210, 56.300), (25.240, 57.300),
        ]),
        "zoom": 12,
        "name": "Strait of Hormuz",
    },
    "B": {
        "polygon": _parse_polygon("BAB_AL_MANDAB_POLYGON", [
            (12.80, 43.10), (12.80, 43.60),
            (12.35, 43.60), (12.35, 43.10),
        ]),
        "zoom": 12,
        "name": "Bab al-Mandab",
    },
    "G": {
        "polygon": _parse_polygon("GIBRALTAR_POLYGON", [
            (36.20, -5.60), (36.20, -5.20),
            (35.85, -5.20), (35.85, -5.60),
        ]),
        "zoom": 12,
        "name": "Strait of Gibraltar",
    },
    "E": {
        "polygon": _parse_polygon("ENGLISH_CHANNEL_POLYGON", [
            (51.15, 1.15), (51.15, 1.65),
            (50.85, 1.65), (50.85, 1.15),
        ]),
        "zoom": 12,
        "name": "Dover Strait",
    },
    "SU": {
        "polygon": _parse_polygon("SUNDA_POLYGON", [
            (-5.80, 105.65), (-5.80, 106.20),
            (-6.20, 106.20), (-6.20, 105.65),
        ]),
        "zoom": 12,
        "name": "Sunda Strait",
    },
    "LO": {
        "polygon": _parse_polygon("LOMBOK_POLYGON", [
            (-8.25, 115.35), (-8.25, 115.90),
            (-8.85, 115.90), (-8.85, 115.35),
        ]),
        "zoom": 12,
        "name": "Lombok Strait",
    },
    "SG": {
        "polygon": _parse_polygon("SINGAPORE_POLYGON", [
            (1.50, 103.50), (1.50, 104.25),
            (1.00, 104.25), (1.00, 103.50),
        ]),
        "zoom": 12,
        "name": "Singapore Strait",
    },

    # ── Zoom 11: Wide straits ────────────────────────────────────────────
    "TW": {
        "polygon": _parse_polygon("TAIWAN_POLYGON", [
            (25.50, 118.00), (25.50, 120.50),
            (23.50, 120.50), (23.50, 118.00),
        ]),
        "zoom": 11,
        "name": "Taiwan Strait",
    },
    "KO": {
        "polygon": _parse_polygon("KOREA_POLYGON", [
            (35.00, 128.50), (35.00, 130.50),
            (33.50, 130.50), (33.50, 128.50),
        ]),
        "zoom": 11,
        "name": "Korean Strait",
    },
    "DA": {
        "polygon": _parse_polygon("DANISH_POLYGON", [
            (58.00, 9.50), (58.00, 13.00),
            (55.00, 13.00), (55.00, 9.50),
        ]),
        "zoom": 11,
        "name": "Danish Straits",
    },
    "SC": {
        "polygon": _parse_polygon("SICILY_POLYGON", [
            (38.00, 10.00), (38.00, 13.00),
            (35.50, 13.00), (35.50, 10.00),
        ]),
        "zoom": 11,
        "name": "Sicilian Channel",
    },
    "YU": {
        "polygon": _parse_polygon("YUCATAN_POLYGON", [
            (22.50, -87.50), (22.50, -85.50),
            (20.50, -85.50), (20.50, -87.50),
        ]),
        "zoom": 11,
        "name": "Yucatan Channel",
    },

    # ── Zoom 10: Regional corridors ──────────────────────────────────────
    "SCS": {
        "polygon": _parse_polygon("S_CHINA_SEA_POLYGON", [
            (22.00, 108.00), (22.00, 118.00),
            (10.00, 118.00), (10.00, 108.00),
        ]),
        "zoom": 10,
        "name": "South China Sea",
    },
    "RS": {
        "polygon": _parse_polygon("RED_SEA_POLYGON", [
            (28.00, 32.50), (28.00, 42.00),
            (20.00, 42.00), (20.00, 32.50),
        ]),
        "zoom": 10,
        "name": "Red Sea",
    },
    "PG": {
        "polygon": _parse_polygon("PERSIAN_GULF_POLYGON", [
            (30.50, 47.00), (30.50, 57.00),
            (23.50, 57.00), (23.50, 47.00),
        ]),
        "zoom": 10,
        "name": "Persian Gulf",
    },
    "GA": {
        "polygon": _parse_polygon("GULF_OF_ADEN_POLYGON", [
            (15.50, 43.00), (15.50, 51.50),
            (11.00, 51.50), (11.00, 43.00),
        ]),
        "zoom": 10,
        "name": "Gulf of Aden",
    },
    "ECS": {
        "polygon": _parse_polygon("E_CHINA_SEA_POLYGON", [
            (34.00, 120.00), (34.00, 130.00),
            (26.00, 130.00), (26.00, 120.00),
        ]),
        "zoom": 10,
        "name": "East China Sea",
    },
    "MZ": {
        "polygon": _parse_polygon("MOZAMBIQUE_POLYGON", [
            (-12.00, 35.00), (-12.00, 45.00),
            (-25.00, 45.00), (-25.00, 35.00),
        ]),
        "zoom": 10,
        "name": "Mozambique Channel",
    },
    "CG": {
        "polygon": _parse_polygon("CAPE_POLYGON", [
            (-33.00, 15.00), (-33.00, 22.00),
            (-36.00, 22.00), (-36.00, 15.00),
        ]),
        "zoom": 10,
        "name": "Cape of Good Hope",
    },
    "JV": {
        "polygon": _parse_polygon("JAVA_SEA_POLYGON", [
            (-3.00, 105.00), (-3.00, 115.00),
            (-8.00, 115.00), (-8.00, 105.00),
        ]),
        "zoom": 10,
        "name": "Java Sea",
    },
    "YS": {
        "polygon": _parse_polygon("YELLOW_SEA_POLYGON", [
            (39.00, 119.00), (39.00, 127.00),
            (33.00, 127.00), (33.00, 119.00),
        ]),
        "zoom": 10,
        "name": "Yellow Sea",
    },

    # ── Zoom 9: Open ocean shipping lanes ────────────────────────────────
    "NAE": {
        "polygon": _parse_polygon("N_ATLANTIC_E_POLYGON", [
            (55.00, -30.00), (55.00, -10.00),
            (40.00, -10.00), (40.00, -30.00),
        ]),
        "zoom": 9,
        "name": "North Atlantic East",
    },
    "NAW": {
        "polygon": _parse_polygon("N_ATLANTIC_W_POLYGON", [
            (45.00, -75.00), (45.00, -50.00),
            (30.00, -50.00), (30.00, -75.00),
        ]),
        "zoom": 9,
        "name": "North Atlantic West",
    },
    "MEW": {
        "polygon": _parse_polygon("MED_WEST_POLYGON", [
            (43.00, -5.00), (43.00, 15.00),
            (33.00, 15.00), (33.00, -5.00),
        ]),
        "zoom": 9,
        "name": "Mediterranean West",
    },
    "MEE": {
        "polygon": _parse_polygon("MED_EAST_POLYGON", [
            (40.00, 15.00), (40.00, 36.00),
            (30.00, 36.00), (30.00, 15.00),
        ]),
        "zoom": 9,
        "name": "Mediterranean East",
    },
    "ARS": {
        "polygon": _parse_polygon("ARABIAN_SEA_POLYGON", [
            (24.00, 55.00), (24.00, 72.00),
            (10.00, 72.00), (10.00, 55.00),
        ]),
        "zoom": 9,
        "name": "Arabian Sea",
    },
    "BOB": {
        "polygon": _parse_polygon("BAY_OF_BENGAL_POLYGON", [
            (20.00, 80.00), (20.00, 95.00),
            (5.00, 95.00), (5.00, 80.00),
        ]),
        "zoom": 9,
        "name": "Bay of Bengal",
    },
    "WP": {
        "polygon": _parse_polygon("W_PACIFIC_POLYGON", [
            (40.00, 125.00), (40.00, 145.00),
            (25.00, 145.00), (25.00, 125.00),
        ]),
        "zoom": 9,
        "name": "Western Pacific",
    },
    "IO": {
        "polygon": _parse_polygon("INDIAN_OCEAN_POLYGON", [
            (0.00, 55.00), (0.00, 80.00),
            (-15.00, 80.00), (-15.00, 55.00),
        ]),
        "zoom": 9,
        "name": "Indian Ocean",
    },
}


# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- Proxies ------------------------------------------------------------------

proxies = []
for i in range(10011, 10025):
    proxy = {
        "server": f"http://isp.decodo.com:{i}",
        "username": "sp9r12fuvq",
        "password": "c8yCmlGlR5Kk2=g4rm",
    }
    proxies.append(proxy)

# Geo profiles resolved at startup
geo_profiles: dict[str, GeoProfile] = {}

# --- User agents --------------------------------------------------------------

CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _random_user_agent() -> str:
    return random.choice(CHROME_USER_AGENTS)


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

    page.add_init_script(f"""
    Object.defineProperty(navigator, 'languages', {{
        get: () => ['{locale}', '{language}', 'en'],
    }});

    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : _origQuery(params);

    Object.defineProperty(navigator, 'plugins', {{
        get: () => [1, 2, 3, 4, 5],
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
                         viewport_w, viewport_h):
    """Run OpenCV ship detection in-memory.

    Returns (counts_dict, markers_list):
      - counts: {"stationary_tankers": int, "moving_tankers": int,
                 "stationary_cargos": int, "moving_cargos": int}
      - markers: [{"lat": float, "lon": float, "type": str, "motion": str}, ...]
    """
    from seer import count_ships_from_bytes, extract_marker_coords
    counts = count_ships_from_bytes(img_bytes)
    markers = extract_marker_coords(img_bytes, center_lat, center_lon, zoom,
                                    viewport_w, viewport_h)
    return counts, markers


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


# --- Core capture (single region, given a page) ------------------------------


def _capture_region_tiles(region_name, config, timestamp_str, page, map_dims):
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

    tiles, grid_info = get_tile_centers(polygon, zoom, map_width, map_height)
    n_rows = grid_info["n_rows"]
    n_cols = grid_info["n_cols"]
    logger.info("Region %s (%s): %d tiles (%dx%d), zoom %d",
                region_name, region_display, len(tiles), n_rows, n_cols, zoom)

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

            # Inline ship detection + geo-coordinate extraction
            det, tile_markers = _detect_ships_inline(
                img_bytes, lat, lon, zoom, map_width, map_height
            )
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
    }


# --- Batch capture (tab parallelism) -----------------------------------------


def capture_batch(region_batch, timestamp_str):
    """Capture a batch of regions using multiple tabs in one browser.

    Tab parallelism: all regions in the batch share one browser process and
    one proxy.  Each region gets its own tab (page).  The initial page loads
    happen concurrently (the browser handles parallel network requests),
    then tiles are captured sequentially per tab.

    Compared to one-browser-per-region: saves ~10s startup per extra region
    in the batch, and uses ~80% less memory per additional region.
    """
    proxy = random.choice(proxies)
    fallback = GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)
    geo = geo_profiles.get(proxy["server"], fallback)

    results = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=[
                    "--headless=new",
                    "--enable-gpu-rasterization",
                    "--enable-zero-copy",
                    "--use-angle=default",
                    "--disable-dev-shm-usage",
                ],
            )

            context = browser.new_context(
                proxy={
                    "server": proxy["server"],
                    "username": proxy["username"],
                    "password": proxy["password"],
                },
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                timezone_id=geo.timezone_id,
                locale=geo.locale,
                geolocation={"latitude": geo.latitude, "longitude": geo.longitude},
                permissions=["geolocation"],
                extra_http_headers={"Accept-Language": geo.accept_language},
                user_agent=_random_user_agent(),
            )
            context.add_cookies(_random_cookies(".marinetraffic.com"))

            # Phase 1: Open all tabs and start loading in parallel
            tabs = {}
            for name in region_batch:
                config = REGIONS[name]
                page = context.new_page()
                _inject_stealth_scripts(page, geo)
                _inject_map_hooks(page)

                center_lat, center_lon = _polygon_center(config["polygon"])
                url = build_url(center_lat, center_lon, config["zoom"])
                logger.info("  [batch] Loading tab for %s (%s) at zoom %d",
                            name, config.get("name", name), config["zoom"])
                page.goto(url, wait_until="domcontentloaded")
                tabs[name] = page

            # Phase 2: Setup each tab (Cloudflare, overlays, map discovery)
            ready_tabs = {}
            for name, page in tabs.items():
                try:
                    if _wait_for_cloudflare(page):
                        logger.info("  [%s] Cloudflare passed", name)
                    elif _is_cloudflare_blocked(page):
                        logger.error("  [%s] Cloudflare block", name)
                        _log_json(timestamp_str, name, "", 0, 0, 0, 0.0, 0, 0,
                                  REGIONS[name]["zoom"], [])
                        continue

                    wait_for_map_tiles(page)
                    dismiss_cookie_banner(page)
                    hide_ui_overlays(page)
                    _discover_leaflet_map(page)

                    map_dims = _get_map_dimensions(page)
                    ready_tabs[name] = (page, map_dims)
                except Exception as e:
                    logger.error("  [%s] Setup failed: %s", name, e)
                    _log_json(timestamp_str, name, "", 0, 0, 0, 0.0, 0, 0,
                              REGIONS[name]["zoom"], [])

            # Phase 3: Capture tiles for each ready region
            for name, (page, map_dims) in ready_tabs.items():
                try:
                    result = _capture_region_tiles(
                        name, REGIONS[name], timestamp_str, page, map_dims
                    )
                    results[name] = result
                except Exception as e:
                    logger.error("Region %s capture failed: %s", name, e)

            context.close()
            browser.close()

    except Exception as e:
        logger.error("Batch failed: %s", e)

    return results


# --- Single-region capture (backward-compatible wrapper) ----------------------


def capture_region(region_name, config, timestamp_str):
    """Capture tiles for a single region with its own browser."""
    return capture_batch([region_name], timestamp_str).get(region_name)


# --- Logging ------------------------------------------------------------------


def _log_json(timestamp, region, filepath, total, ok, failed, size_kb,
              tankers=0, cargos=0, zoom=None, detections=None,
              moving_tankers=0, moving_cargos=0, markers=None):
    json_path = Path(CAPTURES_DIR) / "captures_log.json"
    Path(CAPTURES_DIR).mkdir(parents=True, exist_ok=True)

    if json_path.exists():
        with open(json_path, "r") as f:
            entries = json.load(f)
    else:
        entries = []

    status = "success" if failed == 0 else ("partial" if ok > 0 else "error")

    entries.append({
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
    })

    with open(json_path, "w") as f:
        json.dump(entries, f, indent=2)


# --- Orchestration ------------------------------------------------------------


def capture_all_regions(region_filter=None):
    """Capture all (or filtered) regions using batched tab parallelism.

    Regions are grouped into batches of TABS_PER_BROWSER.  Up to MAX_BROWSERS
    batches run in parallel, each in its own browser process with its own proxy.
    """
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    logger.info("Starting capture run: %s", timestamp_str)

    # Determine which regions to capture
    if region_filter:
        names = [n for n in region_filter if n in REGIONS]
    else:
        names = list(REGIONS.keys())

    # Log tile counts per region
    total_tiles = 0
    for name in names:
        config = REGIONS[name]
        tiles, info = get_tile_centers(
            config["polygon"], config["zoom"], VIEWPORT_WIDTH, VIEWPORT_HEIGHT
        )
        total_tiles += len(tiles)
        logger.info("  %s (%s): %d tiles, zoom %d",
                    name, config.get("name", name), len(tiles), config["zoom"])

    logger.info("Total: %d regions, %d tiles", len(names), total_tiles)

    # Split into batches
    batches = [names[i:i + TABS_PER_BROWSER]
               for i in range(0, len(names), TABS_PER_BROWSER)]

    t_start = time.perf_counter()
    all_results = {}

    with ThreadPoolExecutor(max_workers=MAX_BROWSERS) as executor:
        futures = {
            executor.submit(capture_batch, batch, timestamp_str): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                batch_results = future.result()
                all_results.update(batch_results or {})
            except Exception as e:
                logger.error("Batch %s failed: %s", batch, e)

    elapsed = time.perf_counter() - t_start
    per_tile = elapsed / total_tiles if total_tiles > 0 else 0

    # Summary
    grand_tankers = sum(r.get("tankers", 0) for r in all_results.values())
    grand_cargos = sum(r.get("cargos", 0) for r in all_results.values())
    grand_mov_t = sum(r.get("moving_tankers", 0) for r in all_results.values())
    grand_mov_c = sum(r.get("moving_cargos", 0) for r in all_results.values())

    logger.info(
        "STOPWATCH  all regions: %.2fs total | %d tiles | %.2fs/tile | "
        "%d tankers (%d mov) | %d cargo (%d mov)",
        elapsed, total_tiles, per_tile,
        grand_tankers, grand_mov_t, grand_cargos, grand_mov_c,
    )

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
    for arg in sys.argv[1:]:
        if arg.startswith("--regions="):
            region_filter = arg.split("=", 1)[1].split(",")
        elif arg == "--save-images":
            pass  # Already handled at module level via SAVE_IMAGES
        elif arg == "--list-regions":
            print(f"{'Key':<6} {'Zoom':<5} {'Name'}")
            print("-" * 50)
            for key, config in sorted(REGIONS.items()):
                print(f"{key:<6} z{config['zoom']:<4} {config.get('name', key)}")
            return
        elif arg == "--help":
            print(__doc__)
            return

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
        tiles, info = get_tile_centers(
            config["polygon"], config["zoom"], VIEWPORT_WIDTH, VIEWPORT_HEIGHT
        )
        total_tiles += len(tiles)

    active_count = len(region_filter) if region_filter else len(REGIONS)
    logger.info("Total: %d regions, %d tiles | Viewport: %dx%d | "
                "Tabs/browser: %d | Max browsers: %d | Save images: %s",
                active_count, total_tiles, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
                TABS_PER_BROWSER, MAX_BROWSERS, SAVE_IMAGES)

    # Run once immediately
    capture_all_regions(region_filter)

    # Schedule future runs
    sched.every(SCRAPE_INTERVAL_MINUTES).minutes.do(scheduled_run)
    while True:
        sched.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
