"""Region definitions for MarineScraper.

Extracted so both the scraper and the API can import REGIONS without
pulling in browser/proxy dependencies.
"""

import os


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
