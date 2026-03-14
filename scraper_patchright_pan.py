#!/bin/python3
"""Patchright scraper using mouse-drag map panning instead of page.goto() per tile.

Loads MarineTraffic once per region, then pans the map via simulated mouse
drag for each subsequent tile.  Uses Mercator projection to compute exact
pixel offsets.  This avoids repeated Cloudflare checks, full page loads,
and re-running cookie/overlay cleanup on every tile.
"""

import base64
import io
import json
import logging
import os
import random
import string
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

ZOOM_LEVEL = int(os.getenv("ZOOM_LEVEL", "13"))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "7680"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "4320"))
CAPTURES_DIR = os.getenv("CAPTURES_DIR_PATCHRIGHT_PAN", "./captures_patchright_pan")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "300"))


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


NORTH_POLYGON = _parse_polygon("NORTH_POLYGON", [
    (31.575, 31.91),
    (31.77435, 32.27517),
    (31.31, 32.27036),
    (31.517, 32.5445),
])

SOUTH_POLYGON = _parse_polygon("SOUTH_POLYGON", [
    (29.865656, 32.481079),
    (29.900187, 32.598495),
    (29.657029, 32.57515),
    (29.702964, 32.715225),
])

HORMUZ_POLYGON = _parse_polygon("HORMUZ_POLYGON", [
    (26.410, 56.250),
    (26.110, 57.100),
    (24.210, 56.300),
    (25.240, 57.300),
])

BAB_AL_MANDAB_POLYGON = _parse_polygon("BAB_AL_MANDAB_POLYGON", [
    (12.80, 43.10),
    (12.80, 43.60),
    (12.35, 43.60),
    (12.35, 43.10),
])

PANAMA_CANAL_POLYGON = _parse_polygon("PANAMA_CANAL_POLYGON", [
    (9.45, -79.95),
    (9.45, -79.50),
    (8.85, -79.50),
    (8.85, -79.95),
])

MALACCA_POLYGON = _parse_polygon("MALACCA_POLYGON", [
    (1.60, 103.30),
    (1.60, 104.10),
    (1.05, 104.10),
    (1.05, 103.30),
])

GIBRALTAR_POLYGON = _parse_polygon("GIBRALTAR_POLYGON", [
    (36.20, -5.60),
    (36.20, -5.20),
    (35.85, -5.20),
    (35.85, -5.60),
])

ENGLISH_CHANNEL_POLYGON = _parse_polygon("ENGLISH_CHANNEL_POLYGON", [
    (51.15, 1.15),
    (51.15, 1.65),
    (50.85, 1.65),
    (50.85, 1.15),
])

REGIONS = {
    "N": NORTH_POLYGON,
    "S": SOUTH_POLYGON,
    "H": HORMUZ_POLYGON,
    "B": BAB_AL_MANDAB_POLYGON,
    "P": PANAMA_CANAL_POLYGON,
    "M": MALACCA_POLYGON,
    "G": GIBRALTAR_POLYGON,
    "E": ENGLISH_CHANNEL_POLYGON,
}

# Human-readable region names for logging/display
REGION_NAMES = {
    "N": "Suez North",
    "S": "Suez South",
    "H": "Strait of Hormuz",
    "B": "Bab al-Mandab",
    "P": "Panama Canal",
    "M": "Strait of Malacca",
    "G": "Strait of Gibraltar",
    "E": "English Channel",
}

# --- Logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- Proxies ------------------------------------------------------------------

proxies = []
for i in range(10011, 10097):
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
            // Preserve Leaflet map panes — they contain rendering layers
            // like the overlay pane that draws the sea/land boundary.
            if (el.closest('.leaflet-pane')) continue;
            el.style.setProperty('display', 'none', 'important');
        }

        // Hide all siblings of #map_canvas (sidebar/nav rail)
        const mc = document.getElementById('map_canvas');
        if (mc && mc.parentElement) {
            for (const sib of mc.parentElement.children) {
                if (sib !== mc && !sib.contains(mc)) {
                    sib.style.setProperty('display', 'none', 'important');
                }
            }
        }

        // Remove borders/shadows/outlines from all remaining elements
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
    // Match navigator.languages to geo locale
    Object.defineProperty(navigator, 'languages', {{
        get: () => ['{locale}', '{language}', 'en'],
    }});

    // Permissions API — return real state instead of throwing
    const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : _origQuery(params);

    // Plugin count — real Chrome always has at least a few
    Object.defineProperty(navigator, 'plugins', {{
        get: () => [1, 2, 3, 4, 5],
    }});
    """)


def _inject_map_hooks(page):
    """Hook Leaflet.Map to disable inertia and capture the map instance.

    Leaflet (the JS map library MarineTraffic uses) applies "inertia" by
    default — after mouse-up the map keeps sliding with momentum.  This
    causes pan overshoot that compounds across tiles, ruining stitching.

    This init script intercepts the global ``L`` object when Leaflet assigns
    it to ``window.L``, patches the Map defaults to disable inertia, and
    stores the map instance in ``window.__mtMap`` so we can later call
    ``map.setView()`` for pixel-perfect positioning.
    """
    page.add_init_script("""
    (function() {
        window.__mtMap = null;

        function patchLeaflet(L) {
            if (!L || !L.Map) return;
            // Disable inertia globally for all Leaflet maps
            if (L.Map.mergeOptions) {
                L.Map.mergeOptions({
                    inertia: false,
                    inertiaDeceleration: 99999,
                    inertiaMaxSpeed: 0,
                });
            }
            // Capture map instance at construction time
            var _origInit = L.Map.prototype.initialize;
            L.Map.prototype.initialize = function() {
                _origInit.apply(this, arguments);
                window.__mtMap = this;
            };
        }

        // Hook window.L setter to catch when Leaflet loads
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
    """Check if the page shows a Cloudflare block."""
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
    """Wait for a Cloudflare JS challenge to resolve.
    Returns True if challenge was detected and passed, False otherwise."""
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


# --- Mouse-drag map panning ---------------------------------------------------


MAX_DRAG_PX = 800  # max single-drag distance to avoid Leaflet inertia


def _get_map_dimensions(page):
    """Measure the actual #map_canvas element bounding rect."""
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


def _canvas_snapshot(page):
    """Capture the map canvas via JS toDataURL — faster than element screenshot.

    Playwright's locator.screenshot() goes through the CDP screenshot pipeline:
    compositor renders full page → clips to element → encodes PNG → transfers
    over the DevTools protocol.  This function instead calls canvas.toDataURL()
    directly in JavaScript, which reads pixel data straight from the GPU-backed
    canvas buffer and encodes it in-process.  For large canvases (8K) this
    skips the compositor step entirely, saving significant time.

    Returns PNG bytes, or None if the canvas isn't accessible (tainted by
    cross-origin tiles, missing element, etc.) — caller should fall back to
    locator.screenshot().
    """
    data_url = page.evaluate("""
    () => {
        // Find the canvas inside #map_canvas (Leaflet's rendering surface)
        const mc = document.getElementById('map_canvas');
        const canvas = mc ? mc.querySelector('canvas') : document.querySelector('canvas');
        if (!canvas) return null;
        try {
            return canvas.toDataURL('image/png');
        } catch(e) {
            // SecurityError if canvas is tainted by cross-origin tiles
            return null;
        }
    }
    """)
    if not data_url:
        return None
    # data_url format: "data:image/png;base64,iVBOR..."
    _, b64_data = data_url.split(",", 1)
    return base64.b64decode(b64_data)


def _pan_map_js(page, target_lat, target_lon, zoom):
    """Pan map using Leaflet's setView API — pixel-perfect positioning.

    Uses the map instance captured by ``_inject_map_hooks()`` to call
    ``map.setView()`` with ``animate: false``, which instantly repositions
    the map without any inertia or animation drift.

    Returns True if the JS pan succeeded, False if the map instance
    was not available (caller should fall back to mouse drag).
    """
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
    """Pan the map from (cur_lat, cur_lon) to (target_lat, target_lon).

    Tries pixel-perfect JS setView first (via captured Leaflet map instance).
    Falls back to simulated mouse drag with Mercator pixel offsets.
    Returns True on success.
    """
    # --- Strategy 1: pixel-perfect JS pan via Leaflet API ---
    if _pan_map_js(page, target_lat, target_lon, zoom):
        logger.info("  Panned map via: setView (%.5f, %.5f)", target_lat, target_lon)
        time.sleep(0.1)
        _wait_for_tiles_after_pan(page, timeout_ms)
        time.sleep(0.5)  # Wait for ship AIS data to load via AJAX
        return True

    # --- Strategy 2: mouse drag fallback ---
    total_pixels = 256 * (2 ** zoom)

    # Pixel deltas (how far the *map content* needs to move)
    dx = (target_lon - cur_lon) * total_pixels / 360.0
    dy = lat_to_pixel_y(target_lat, zoom) - lat_to_pixel_y(cur_lat, zoom)

    # Drag direction is opposite to content movement
    drag_x = -dx
    drag_y = -dy

    if abs(drag_x) < 1 and abs(drag_y) < 1:
        return True  # already at target

    if map_center:
        cx, cy = map_center
    else:
        cx = VIEWPORT_WIDTH // 2
        cy = VIEWPORT_HEIGHT // 2

    # Split into steps if drag distance exceeds MAX_DRAG_PX
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
        # steps=20 → slow smooth drag to prevent Leaflet inertia/momentum
        page.mouse.move(cx + step_dx, cy + step_dy, steps=20)
        page.mouse.up()
        if steps_needed > 1:
            time.sleep(0.05)  # settle between multi-step drags

    logger.info("  Panned map via: mouse_drag (dx=%.0f dy=%.0f, %d step(s))",
                drag_x, drag_y, steps_needed)

    # Wait for new tiles to render
    time.sleep(0.1)
    _wait_for_tiles_after_pan(page, timeout_ms)
    time.sleep(0.5)  # Wait for ship AIS data to load via AJAX
    return True


def _wait_for_tiles_after_pan(page, timeout_ms=5000):
    """Wait for map tiles to re-render after a pan.
    Lighter than wait_for_map_tiles — the canvas already exists."""
    deadline = time.time() + (timeout_ms / 1000)
    # Brief pause to let the map start loading new tiles
    time.sleep(0.1)
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
        time.sleep(0.1)


# --- Core capture -------------------------------------------------------------


def capture_region(region_name, polygon, timestamp_str):
    """Capture tiles for a region.  Loads page once, then pans via mouse drag."""
    output_dir = Path(CAPTURES_DIR) / region_name
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{region_name}{timestamp_str}.png"
    output_path = output_dir / filename

    tile_images = {}
    tiles_ok = 0
    tiles_failed = 0
    tiles = []
    grid_info = {}
    map_width = VIEWPORT_WIDTH
    map_height = VIEWPORT_HEIGHT

    proxy = random.choice(proxies)
    fallback = GeoProfile(proxy=proxy, **EGYPT_FALLBACK_DATA)
    geo = geo_profiles.get(proxy["server"], fallback)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=[
                    "--headless=new",
                    # GPU-accelerated rendering — speeds up canvas compositing
                    "--enable-gpu-rasterization",
                    "--enable-zero-copy",
                    "--use-angle=default",
                    # Large viewport memory safety
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
                extra_http_headers={
                    "Accept-Language": geo.accept_language,
                },
                user_agent=_random_user_agent(),
            )

            context.add_cookies(_random_cookies(".marinetraffic.com"))  # type: ignore[arg-type]
            page = context.new_page()
            _inject_stealth_scripts(page, geo)
            _inject_map_hooks(page)

            # --- INITIAL LOAD: navigate to polygon center ---
            lats = [p[0] for p in polygon]
            lons = [p[1] for p in polygon]
            center_lat = (min(lats) + max(lats)) / 2
            center_lon = (min(lons) + max(lons)) / 2
            initial_url = build_url(center_lat, center_lon, ZOOM_LEVEL)
            logger.info("  Initial load: %s", region_name)

            page.goto(initial_url, wait_until="domcontentloaded")

            # Handle Cloudflare — only needed once
            if _wait_for_cloudflare(page):
                logger.info("  Cloudflare challenge passed")
            elif _is_cloudflare_blocked(page):
                logger.error("  Cloudflare block on initial load, aborting region")
                context.close()
                browser.close()
                _log_json(timestamp_str, region_name, "", 0, 0, 0, 0)
                return None

            wait_for_map_tiles(page)
            dismiss_cookie_banner(page)
            hide_ui_overlays(page)

            # Verify the Leaflet map instance was captured by our init hook.
            # If the hook missed it (e.g. Leaflet loaded before our script),
            # try to discover it from the DOM as a fallback.
            has_map = page.evaluate("""
            () => {
                if (window.__mtMap) return true;

                // Fallback: find the Leaflet map instance from DOM
                const containers = document.querySelectorAll('.leaflet-container');
                for (const c of containers) {
                    for (const key of Object.keys(c)) {
                        const val = c[key];
                        if (val && typeof val === 'object'
                            && typeof val.setView === 'function'
                            && typeof val.getCenter === 'function') {
                            window.__mtMap = val;
                            // Also disable inertia on the discovered instance
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
            logger.info("  Leaflet map instance captured: %s", has_map)

            # Measure actual map element dimensions
            map_dims = _get_map_dimensions(page)
            map_width = int(map_dims["width"])
            map_height = int(map_dims["height"])
            map_cx = int(map_dims["x"]) + map_width // 2
            map_cy = int(map_dims["y"]) + map_height // 2
            logger.info("  Map element: %dx%d at (%d,%d)",
                        map_width, map_height, int(map_dims["x"]), int(map_dims["y"]))

            # Compute tile grid using actual map dimensions
            tiles, grid_info = get_tile_centers(
                polygon, ZOOM_LEVEL, map_width, map_height
            )
            n_rows = grid_info["n_rows"]
            n_cols = grid_info["n_cols"]
            logger.info("Region %s: %d tiles (%dx%d)", region_name, len(tiles), n_rows, n_cols)

            # Navigate to first tile center
            first_row, first_col, first_lat, first_lon = tiles[0]
            _pan_map(page, center_lat, center_lon, first_lat, first_lon, ZOOM_LEVEL,
                     map_center=(map_cx, map_cy))

            # Capture first tile (element screenshot — composites all Leaflet layers)
            map_locator = page.locator('#map_canvas')
            tile_images[(first_row, first_col)] = map_locator.screenshot()
            tiles_ok += 1
            logger.info("  Tile (%d,%d) captured [1/%d]", first_row, first_col, len(tiles))
            current_lat, current_lon = first_lat, first_lon

            # --- SUBSEQUENT TILES: pan via mouse drag ---
            for i, (row, col, lat, lon) in enumerate(tiles[1:], start=2):
                logger.info("  Tile (%d,%d) [%d/%d] (pan)", row, col, i, len(tiles))
                try:
                    _pan_map(page, current_lat, current_lon, lat, lon, ZOOM_LEVEL,
                             map_center=(map_cx, map_cy))
                    current_lat, current_lon = lat, lon

                    tile_images[(row, col)] = map_locator.screenshot()
                    tiles_ok += 1
                    logger.info("  Tile (%d,%d) captured", row, col)
                except Exception as e:
                    logger.error("  Tile (%d,%d) failed: %s", row, col, e)
                    tiles_failed += 1

            context.close()
            browser.close()

    except Exception as e:
        logger.error("Region %s session failed: %s", region_name, e)

    if not tile_images:
        logger.error("Region %s: all tiles failed, skipping composite", region_name)
        _log_json(timestamp_str, region_name, "", len(tiles), 0, tiles_failed, 0)
        return None

    # --- Stitch tiles into composite ------------------------------------------
    comp_w = grid_info["n_cols"] * map_width
    comp_h = grid_info["n_rows"] * map_height
    composite = Image.new("RGB", (comp_w, comp_h), (0, 0, 0))

    for (row, col), img_bytes in tile_images.items():
        tile_img = Image.open(io.BytesIO(img_bytes))
        composite.paste(tile_img, (col * map_width, row * map_height))

    # --- Debug: draw tile grid lines to verify stitching alignment ------------
    if os.getenv("DEBUG_TILE_GRID"):
        debug_draw = ImageDraw.Draw(composite)
        for r in range(grid_info["n_rows"] + 1):
            y = r * map_height
            debug_draw.line([(0, y), (comp_w, y)], fill=(255, 0, 255), width=2)
        for c in range(grid_info["n_cols"] + 1):
            x = c * map_width
            debug_draw.line([(x, 0), (x, comp_h)], fill=(255, 0, 255), width=2)
        logger.info("  Debug tile grid drawn (%dx%d)", grid_info["n_cols"], grid_info["n_rows"])

    # --- Mask to polygon (black out outside) ----------------------------------
    pixel_coords = polygon_to_pixel_coords(polygon, grid_info, ZOOM_LEVEL)
    mask = Image.new("L", composite.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.polygon(pixel_coords, fill=255)

    black = Image.new("RGB", composite.size, (0, 0, 0))
    result = Image.composite(composite, black, mask)

    result.save(str(output_path))
    file_size_kb = output_path.stat().st_size / 1024
    logger.info("Region %s: saved %s (%.1f KB)", region_name, filename, file_size_kb)

    _log_json(
        timestamp_str, region_name, filename,
        len(tiles), tiles_ok, tiles_failed, file_size_kb,
    )
    return output_path


def _log_json(timestamp, region, filename, total, ok, failed, size_kb):
    json_path = Path(CAPTURES_DIR) / "captures_log.json"
    Path(CAPTURES_DIR).mkdir(parents=True, exist_ok=True)

    if json_path.exists():
        with open(json_path, "r") as f:
            entries = json.load(f)
    else:
        entries = []

    status = "success" if failed == 0 else ("partial" if ok > 0 else "error")
    filepath = str(Path(CAPTURES_DIR) / region / filename) if filename else ""

    entries.append({
        "region": region,
        "filename": filename,
        "filepath": filepath,
        "is_north": region == "N",
        "date_time": timestamp,
        "tiles_total": total,
        "tiles_ok": ok,
        "tiles_failed": failed,
        "zoom": ZOOM_LEVEL,
        "file_size_kb": round(size_kb, 1),
        "status": status,
    })

    with open(json_path, "w") as f:
        json.dump(entries, f, indent=2)


# --- Scheduling ---------------------------------------------------------------


def capture_all_regions():
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    logger.info("Starting capture run: %s", timestamp_str)

    total_tiles = 0
    for name, poly in REGIONS.items():
        tiles, _ = get_tile_centers(poly, ZOOM_LEVEL, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
        total_tiles += len(tiles)

    t_start = time.perf_counter()

    # Capture all regions in parallel — each gets its own browser in its own thread
    with ThreadPoolExecutor(max_workers=len(REGIONS)) as executor:
        futures = {
            executor.submit(capture_region, name, poly, timestamp_str): name
            for name, poly in REGIONS.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error("Region %s failed entirely: %s", name, e)

    elapsed = time.perf_counter() - t_start
    per_tile = elapsed / total_tiles if total_tiles > 0 else 0
    logger.info(
        "STOPWATCH  all regions: %.2fs total | %d tiles | %.2fs per tile",
        elapsed, total_tiles, per_tile,
    )


def scheduled_run():
    jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
    delay = max(0, jitter)
    if delay > 0:
        logger.info("Jitter: waiting %.0fs before run", delay)
        time.sleep(delay)
    capture_all_regions()


def main():
    global geo_profiles

    # Resolve proxy geolocations at startup
    logger.info("Resolving proxy geolocations...")
    geo_profiles = resolve_all_proxies(proxies)
    logger.info("Resolved %d/%d proxy profiles", len(geo_profiles), len(proxies))

    for name, poly in REGIONS.items():
        tiles, info = get_tile_centers(poly, ZOOM_LEVEL, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
        logger.info(
            "Region %s: %d tiles (%dx%d grid)",
            name, len(tiles), info["n_rows"], info["n_cols"],
        )
    logger.info("Zoom: %d, Viewport: %dx%d", ZOOM_LEVEL, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
    logger.info("Interval: %dm, Jitter: +/-%ds", SCRAPE_INTERVAL_MINUTES, JITTER_SECONDS)

    # Run once immediately
    capture_all_regions()

    # Schedule future runs
    sched.every(SCRAPE_INTERVAL_MINUTES).minutes.do(scheduled_run)
    while True:
        sched.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
