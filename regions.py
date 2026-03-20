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

# ---------------------------------------------------------------------------
# Tier classification for CLI filtering (--tier=1, --tier=2, --tier=3).
#   original = the original 34 chokepoint/corridor regions
#   1        = major global trade arteries (added expansion)
#   2        = regionally critical routes (added expansion)
#   3        = coverage fill for ~30% ocean surface (added expansion)
# ---------------------------------------------------------------------------
REGION_TIERS = {
    # Original regions (no tier flag needed — always included unless filtered)
    "N": "original", "S": "original", "P": "original", "M": "original",
    "BO": "original", "H": "original", "B": "original", "G": "original",
    "E": "original", "SU": "original", "LO": "original", "SG": "original",
    "TW": "original", "KO": "original", "DA": "original", "SC": "original",
    "YU": "original", "SCS": "original", "RS": "original", "PG": "original",
    "GA": "original", "ECS": "original", "MZ": "original", "CG": "original",
    "JV": "original", "YS": "original", "NAE": "original", "NAW": "original",
    "MEW": "original", "MEE": "original", "ARS": "original", "BOB": "original",
    "WP": "original", "IO": "original",
    # Tier 1: Major global trade arteries
    "GOM": "1", "CAR": "1", "USE": "1", "USW": "1", "NS": "1", "BS": "1",
    "BLK": "1", "GG": "1", "SAW": "1", "SAE": "1", "MKS": "1", "PHI": "1",
    "NWP": "1",
    "TSU": "1", "TSM": "1", "TOR": "1", "CHR": "1", "HOU": "1", "SHA": "1",
    "BNF": "1",
    # Tier 2: Regionally critical
    "NOR": "2", "GOT": "2", "COR": "2", "SIO": "2", "NAM": "2", "SEP": "2",
    "RLP": "2",
    "BAS": "2", "CK": "2", "WIN": "2", "MON": "2", "ORE": "2", "LUZ": "2",
    "GSA": "2", "MAR": "2", "SAN": "2",
    # Tier 3: Coverage fill
    "CEP": "3", "SPO": "3", "SOA": "3",
    "ARC": "3", "NNC": "3", "WAO": "3", "EAF": "3", "NEP": "3", "BFS": "3",
}

REGIONS = {
    # ── Zoom 13: Narrow / extremely dense ────────────────────────────────
    "N": {
        "polygon": _parse_polygon("NORTH_POLYGON", [
            (31.575, 31.91), (31.77435, 32.27517),
            (31.517, 32.5445), (31.31, 32.27036),
        ]),
        "zoom": 13,
        "name": "Suez Canal North",
    },
    "S": {
        "polygon": _parse_polygon("SOUTH_POLYGON", [
            (29.865656, 32.481079), (29.900187, 32.598495),
            (29.702964, 32.715225), (29.657029, 32.57515),
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
            (25.240, 57.300), (24.210, 56.300),
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
    "MKS": {
        "polygon": _parse_polygon("MAKASSAR_POLYGON", [
            (-1.00, 117.00), (-1.00, 120.00),
            (-5.00, 120.00), (-5.00, 117.00),
        ]),
        "zoom": 11,
        "name": "Makassar Strait",
    },
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
    "NS": {
        "polygon": _parse_polygon("NORTH_SEA_POLYGON", [
            (61.00, -4.00), (61.00, 9.00),
            (51.00, 9.00), (51.00, -4.00),
        ]),
        "zoom": 10,
        "name": "North Sea",
    },
    "BLK": {
        "polygon": _parse_polygon("BLACK_SEA_POLYGON", [
            (47.00, 28.00), (47.00, 42.00),
            (41.00, 42.00), (41.00, 28.00),
        ]),
        "zoom": 10,
        "name": "Black Sea",
    },
    "GOT": {
        "polygon": _parse_polygon("GULF_OF_THAILAND_POLYGON", [
            (14.00, 97.00), (14.00, 107.00),
            (5.00, 107.00), (5.00, 97.00),
        ]),
        "zoom": 10,
        "name": "Gulf of Thailand",
    },
    "RLP": {
        "polygon": _parse_polygon("RIO_DE_LA_PLATA_POLYGON", [
            (-34.00, -58.00), (-34.00, -52.00),
            (-37.00, -52.00), (-37.00, -58.00),
        ]),
        "zoom": 10,
        "name": "Rio de la Plata",
    },
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
    "GOM": {
        "polygon": _parse_polygon("GULF_OF_MEXICO_POLYGON", [
            (30.00, -98.00), (30.00, -82.00),
            (18.00, -82.00), (18.00, -98.00),
        ]),
        "zoom": 9,
        "name": "Gulf of Mexico",
    },
    "CAR": {
        "polygon": _parse_polygon("CARIBBEAN_POLYGON", [
            (20.00, -85.00), (20.00, -60.00),
            (10.00, -60.00), (10.00, -85.00),
        ]),
        "zoom": 9,
        "name": "Caribbean Sea",
    },
    "USE": {
        "polygon": _parse_polygon("US_EAST_COAST_POLYGON", [
            (40.00, -80.00), (40.00, -65.00),
            (25.00, -65.00), (25.00, -80.00),
        ]),
        "zoom": 9,
        "name": "US East Coast",
    },
    "USW": {
        "polygon": _parse_polygon("US_WEST_COAST_POLYGON", [
            (50.00, -135.00), (50.00, -117.00),
            (30.00, -117.00), (30.00, -135.00),
        ]),
        "zoom": 9,
        "name": "US/Canada West Coast",
    },
    "BS": {
        "polygon": _parse_polygon("BALTIC_SEA_POLYGON", [
            (66.00, 10.00), (66.00, 30.00),
            (54.00, 30.00), (54.00, 10.00),
        ]),
        "zoom": 9,
        "name": "Baltic Sea",
    },
    "GG": {
        "polygon": _parse_polygon("GULF_OF_GUINEA_POLYGON", [
            (6.00, -10.00), (6.00, 10.00),
            (-6.00, 10.00), (-6.00, -10.00),
        ]),
        "zoom": 9,
        "name": "Gulf of Guinea",
    },
    "SAW": {
        "polygon": _parse_polygon("S_ATLANTIC_W_POLYGON", [
            (-5.00, -50.00), (-5.00, -25.00),
            (-35.00, -25.00), (-35.00, -50.00),
        ]),
        "zoom": 9,
        "name": "South Atlantic West",
    },
    "SAE": {
        "polygon": _parse_polygon("S_ATLANTIC_E_POLYGON", [
            (-5.00, 0.00), (-5.00, 15.00),
            (-30.00, 15.00), (-30.00, 0.00),
        ]),
        "zoom": 9,
        "name": "South Atlantic East",
    },
    "PHI": {
        "polygon": _parse_polygon("PHILIPPINE_SEA_POLYGON", [
            (20.00, 120.00), (20.00, 135.00),
            (5.00, 135.00), (5.00, 120.00),
        ]),
        "zoom": 9,
        "name": "Philippine Sea",
    },
    "NWP": {
        "polygon": _parse_polygon("N_PACIFIC_POLYGON", [
            (50.00, 155.00), (50.00, -175.00),
            (30.00, -175.00), (30.00, 155.00),
        ]),
        "zoom": 9,
        "name": "North Pacific",
    },
    "NOR": {
        "polygon": _parse_polygon("NORWEGIAN_SEA_POLYGON", [
            (72.00, -10.00), (72.00, 15.00),
            (62.00, 15.00), (62.00, -10.00),
        ]),
        "zoom": 9,
        "name": "Norwegian Sea",
    },
    "COR": {
        "polygon": _parse_polygon("CORAL_SEA_POLYGON", [
            (-10.00, 145.00), (-10.00, 175.00),
            (-35.00, 175.00), (-35.00, 145.00),
        ]),
        "zoom": 9,
        "name": "Coral Sea / Tasman",
    },
    "SIO": {
        "polygon": _parse_polygon("S_INDIAN_OCEAN_POLYGON", [
            (-15.00, 40.00), (-15.00, 80.00),
            (-40.00, 80.00), (-40.00, 40.00),
        ]),
        "zoom": 9,
        "name": "South Indian Ocean",
    },
    "NAM": {
        "polygon": _parse_polygon("N_ATLANTIC_M_POLYGON", [
            (45.00, -50.00), (45.00, -25.00),
            (30.00, -25.00), (30.00, -50.00),
        ]),
        "zoom": 9,
        "name": "North Atlantic Mid",
    },
    "SEP": {
        "polygon": _parse_polygon("SE_PACIFIC_POLYGON", [
            (-5.00, -90.00), (-5.00, -70.00),
            (-35.00, -70.00), (-35.00, -90.00),
        ]),
        "zoom": 9,
        "name": "Southeast Pacific",
    },
    "CEP": {
        "polygon": _parse_polygon("C_E_PACIFIC_POLYGON", [
            (30.00, -150.00), (30.00, -120.00),
            (5.00, -120.00), (5.00, -150.00),
        ]),
        "zoom": 9,
        "name": "Central East Pacific",
    },
    "SPO": {
        "polygon": _parse_polygon("S_PACIFIC_POLYGON", [
            (-15.00, 175.00), (-15.00, -140.00),
            (-40.00, -140.00), (-40.00, 175.00),
        ]),
        "zoom": 9,
        "name": "South Pacific",
    },
    "SOA": {
        "polygon": _parse_polygon("S_ATLANTIC_C_POLYGON", [
            (-10.00, -25.00), (-10.00, 0.00),
            (-35.00, 0.00), (-35.00, -25.00),
        ]),
        "zoom": 9,
        "name": "South Atlantic Central",
    },
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

    # ═══════════════════════════════════════════════════════════════════════
    # Expansion regions — Tier 1 / 2 / 3
    # ═══════════════════════════════════════════════════════════════════════

    # ── Zoom 12: Dense straits (expansion) ─────────────────────────────────
    "TSU": {
        "polygon": _parse_polygon("TSUGARU_POLYGON", [
            (41.80, 139.80), (41.80, 141.00),
            (41.20, 141.00), (41.20, 139.80),
        ]),
        "zoom": 12,
        "name": "Strait of Tsugaru",
    },
    "TOR": {
        "polygon": _parse_polygon("TORRES_POLYGON", [
            (-9.50, 141.50), (-9.50, 143.50),
            (-10.80, 143.50), (-10.80, 141.50),
        ]),
        "zoom": 12,
        "name": "Torres Strait",
    },
    "SHA": {
        "polygon": _parse_polygon("SHANGHAI_POLYGON", [
            (31.80, 121.00), (31.80, 122.60),
            (30.50, 122.60), (30.50, 121.00),
        ]),
        "zoom": 12,
        "name": "Shanghai / Yangtze Approach",
    },
    "BNF": {
        "polygon": _parse_polygon("BONIFACIO_POLYGON", [
            (41.40, 8.60), (41.40, 9.50),
            (40.90, 9.50), (40.90, 8.60),
        ]),
        "zoom": 12,
        "name": "Strait of Bonifacio",
    },
    "CK": {
        "polygon": _parse_polygon("COOK_POLYGON", [
            (-41.00, 174.00), (-41.00, 175.00),
            (-41.70, 175.00), (-41.70, 174.00),
        ]),
        "zoom": 12,
        "name": "Cook Strait",
    },
    "ORE": {
        "polygon": _parse_polygon("ORESUND_POLYGON", [
            (56.10, 12.40), (56.10, 13.00),
            (55.50, 13.00), (55.50, 12.40),
        ]),
        "zoom": 12,
        "name": "Oresund",
    },
    "MAR": {
        "polygon": _parse_polygon("MARMARA_POLYGON", [
            (41.10, 27.40), (41.10, 29.40),
            (40.30, 29.40), (40.30, 27.40),
        ]),
        "zoom": 12,
        "name": "Sea of Marmara",
    },

    # ── Zoom 11: Wide straits (expansion) ──────────────────────────────────
    "TSM": {
        "polygon": _parse_polygon("TSUSHIMA_POLYGON", [
            (35.00, 128.00), (35.00, 129.50),
            (33.50, 129.50), (33.50, 128.00),
        ]),
        "zoom": 11,
        "name": "Tsushima Strait",
    },
    "HOU": {
        "polygon": _parse_polygon("HOUSTON_POLYGON", [
            (30.00, -95.50), (30.00, -93.50),
            (28.50, -93.50), (28.50, -95.50),
        ]),
        "zoom": 11,
        "name": "Houston / Texas Coast",
    },
    "BAS": {
        "polygon": _parse_polygon("BASS_POLYGON", [
            (-38.00, 143.50), (-38.00, 148.50),
            (-40.50, 148.50), (-40.50, 143.50),
        ]),
        "zoom": 11,
        "name": "Bass Strait",
    },
    "WIN": {
        "polygon": _parse_polygon("WINDWARD_POLYGON", [
            (20.50, -74.50), (20.50, -72.50),
            (18.50, -72.50), (18.50, -74.50),
        ]),
        "zoom": 11,
        "name": "Windward Passage",
    },
    "MON": {
        "polygon": _parse_polygon("MONA_POLYGON", [
            (19.50, -68.50), (19.50, -66.50),
            (17.50, -66.50), (17.50, -68.50),
        ]),
        "zoom": 11,
        "name": "Mona Passage",
    },
    "LUZ": {
        "polygon": _parse_polygon("LUZON_POLYGON", [
            (22.00, 119.00), (22.00, 122.50),
            (18.50, 122.50), (18.50, 119.00),
        ]),
        "zoom": 11,
        "name": "Luzon Strait",
    },
    "GSA": {
        "polygon": _parse_polygon("GULF_SUEZ_APPROACH_POLYGON", [
            (29.80, 32.40), (29.80, 34.50),
            (27.50, 34.50), (27.50, 32.40),
        ]),
        "zoom": 11,
        "name": "Gulf of Suez Approach",
    },
    "SAN": {
        "polygon": _parse_polygon("SANTOS_POLYGON", [
            (-23.00, -46.80), (-23.00, -44.50),
            (-25.00, -44.50), (-25.00, -46.80),
        ]),
        "zoom": 11,
        "name": "Santos / SE Brazil Coast",
    },

    # ── Zoom 10: Regional corridors (expansion) ───────────────────────────
    "CHR": {
        "polygon": _parse_polygon("CAPE_HORN_POLYGON", [
            (-54.00, -72.00), (-54.00, -63.00),
            (-58.00, -63.00), (-58.00, -72.00),
        ]),
        "zoom": 10,
        "name": "Cape Horn / Drake Passage",
    },
    "BFS": {
        "polygon": _parse_polygon("BANDA_FLORES_POLYGON", [
            (-5.00, 117.00), (-5.00, 128.00),
            (-9.00, 128.00), (-9.00, 117.00),
        ]),
        "zoom": 10,
        "name": "Banda Sea / Flores Sea",
    },

    # ── Zoom 9: Open ocean (expansion) ─────────────────────────────────────
    "ARC": {
        "polygon": _parse_polygon("ARCTIC_POLYGON", [
            (78.00, 30.00), (78.00, 100.00),
            (68.00, 100.00), (68.00, 30.00),
        ]),
        "zoom": 9,
        "name": "Northern Sea Route / Arctic",
    },
    "NNC": {
        "polygon": _parse_polygon("NORW_CORRIDOR_POLYGON", [
            (62.00, -5.00), (62.00, 10.00),
            (55.00, 10.00), (55.00, -5.00),
        ]),
        "zoom": 9,
        "name": "North Sea–Norwegian Corridor",
    },
    "WAO": {
        "polygon": _parse_polygon("W_AFRICA_OFFSHORE_POLYGON", [
            (-4.00, 8.00), (-4.00, 15.00),
            (-18.00, 15.00), (-18.00, 8.00),
        ]),
        "zoom": 9,
        "name": "West Africa Offshore (Angola)",
    },
    "EAF": {
        "polygon": _parse_polygon("E_AFRICA_COAST_POLYGON", [
            (0.00, 38.00), (0.00, 48.00),
            (-12.00, 48.00), (-12.00, 38.00),
        ]),
        "zoom": 9,
        "name": "East Africa Coast",
    },
    "NEP": {
        "polygon": _parse_polygon("NE_PACIFIC_POLYGON", [
            (60.00, -150.00), (60.00, -125.00),
            (48.00, -125.00), (48.00, -150.00),
        ]),
        "zoom": 9,
        "name": "NE Pacific (Alaska–BC)",
    },
}
