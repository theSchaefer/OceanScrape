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
    "TSU": "1", "TSM": "1", "TOR": "1", "CHR": "1", "HOU": "1", "SHA": "1",
    "BNF": "1", "NWP": "1",
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
            (-0.50, 117.50),  # NE Borneo coast
            (-1.50, 118.80),  # East Borneo (Balikpapan approach)
            (-3.00, 118.50),  # SE Borneo
            (-4.00, 119.50),  # Southern strait
            (-5.00, 119.80),  # SW Sulawesi (Makassar city)
            (-3.50, 120.00),  # West Sulawesi coast
            (-1.50, 120.50),  # Central Sulawesi coast
            (-0.50, 119.50),  # North Sulawesi approach
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
            (35.00, 128.00), (35.00, 130.50),
            (33.50, 130.50), (33.50, 128.00),
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
            # North Sea water body.  West: follows UK east coast.
            # East: follows Norwegian/Danish/German/Dutch coast.
            (58.50, -3.50),   # Northern Scotland (Moray Firth)
            (57.50, -1.50),   # NE Scotland (Aberdeen)
            (56.00, -2.00),   # SE Scotland (Edinburgh)
            (55.00, -1.00),   # NE England (Newcastle)
            (53.50, 0.50),    # East England (Humber)
            (52.00, 1.50),    # East Anglia (Norfolk)
            (51.00, 2.00),    # Thames Estuary / Dover
            (51.50, 3.50),    # Belgian/Dutch coast
            (53.50, 6.00),    # Netherlands / Frisian Islands
            (54.50, 8.00),    # German Bight
            (56.00, 8.50),    # Danish west coast (Jutland)
            (58.00, 6.00),    # Norwegian south coast (Stavanger)
            (60.50, 5.00),    # Norwegian west coast (Bergen)
            (62.00, 5.00),    # Norwegian coast (Alesund)
            (62.00, -1.00),   # Norwegian Sea boundary
        ]),
        "zoom": 10,
        "name": "North Sea",
    },
    "BLK": {
        "polygon": _parse_polygon("BLACK_SEA_POLYGON", [
            # Black Sea water body, tracing the coastline to exclude
            # Turkey (south), Caucasus (east), Ukraine/Romania (north-west).
            (43.50, 28.50),   # Romanian coast (Constanta)
            (46.00, 30.50),   # Ukrainian coast (Odesa)
            (46.50, 33.50),   # Crimean west
            (45.00, 36.00),   # Crimean south / Sea of Azov entrance
            (44.50, 38.00),   # Russian coast (Novorossiysk)
            (43.00, 40.50),   # Georgian coast (Batumi)
            (42.00, 41.00),   # Georgian coast
            (41.50, 40.50),   # NE Turkey coast (Trabzon)
            (41.50, 36.00),   # N Turkey coast (Samsun)
            (41.80, 32.50),   # N Turkey coast (Sinop)
            (41.20, 29.50),   # Bosporus approach
            (43.00, 28.50),   # Bulgarian coast (Varna)
        ]),
        "zoom": 10,
        "name": "Black Sea",
    },
    "GOT": {
        "polygon": _parse_polygon("GULF_OF_THAILAND_POLYGON", [
            # Traces the Gulf of Thailand water body, excluding the Thai,
            # Cambodian, and Malaysian peninsulas.
            (13.50, 100.00),  # Bangkok coast
            (12.50, 101.00),  # Eastern Gulf (Pattaya area)
            (10.50, 104.00),  # Cambodian coast
            (8.50, 106.50),   # Southern Vietnam (Mekong Delta)
            (6.00, 106.00),   # SE approach
            (5.00, 104.00),   # South China Sea approach
            (2.50, 104.50),   # East Malaysia approach
            (1.50, 103.50),   # Singapore approach
            (2.50, 101.50),   # Malay Peninsula (east coast)
            (5.50, 100.50),   # Thai-Malay border coast
            (8.00, 99.50),    # Southern Thai coast
            (10.00, 99.00),   # Gulf of Thailand (west side)
            (13.00, 99.50),   # Upper Gulf (west side)
        ]),
        "zoom": 10,
        "name": "Gulf of Thailand",
    },
    "RLP": {
        "polygon": _parse_polygon("RIO_DE_LA_PLATA_POLYGON", [
            (-34.00, -58.40),  # Buenos Aires
            (-34.50, -57.50),  # Montevideo approach
            (-34.80, -55.00),  # Uruguayan coast
            (-35.50, -53.50),  # Outer estuary
            (-37.00, -54.00),  # SE open ocean
            (-37.00, -57.00),  # S Argentine shelf
            (-36.00, -57.50),  # Mar del Plata approach
            (-35.00, -58.00),  # Argentine coast
        ]),
        "zoom": 10,
        "name": "Rio de la Plata",
    },
    "SCS": {
        "polygon": _parse_polygon("S_CHINA_SEA_POLYGON", [
            (22.00, 110.00),  # Hainan / S China coast
            (21.00, 113.00),  # Hong Kong approach
            (20.00, 117.00),  # Taiwan Strait south
            (15.00, 118.00),  # Luzon (west coast)
            (10.00, 117.50),  # Palawan approach
            (7.00, 115.00),   # Spratly area
            (5.00, 112.00),   # Borneo NW coast
            (3.00, 110.00),   # Sarawak coast
            (2.00, 108.00),   # South approach
            (10.00, 107.00),  # Vietnam south coast
            (16.00, 108.00),  # Vietnam central coast
            (20.00, 107.00),  # Gulf of Tonkin
        ]),
        "zoom": 10,
        "name": "South China Sea",
    },
    "RS": {
        "polygon": _parse_polygon("RED_SEA_POLYGON", [
            # Red Sea — narrow body of water between Egypt/Sudan and
            # Saudi Arabia.  Tightly follows both coastlines.
            (28.00, 33.50),   # Gulf of Suez (north)
            (27.50, 34.00),   # Egyptian coast (Hurghada)
            (24.00, 35.50),   # Egyptian coast
            (22.00, 36.50),   # Sudanese coast
            (20.00, 38.50),   # Southern Red Sea (Eritrea approach)
            (20.00, 40.50),   # Southern Red Sea (Saudi side)
            (22.50, 39.00),   # Saudi coast (Jeddah)
            (25.00, 37.00),   # Saudi coast
            (27.00, 36.00),   # Saudi coast (Tabuk)
            (28.00, 35.00),   # Gulf of Aqaba approach
        ]),
        "zoom": 10,
        "name": "Red Sea",
    },
    "PG": {
        "polygon": _parse_polygon("PERSIAN_GULF_POLYGON", [
            # Persian Gulf — wider polygon to ensure tile centers over
            # water are kept.  Excludes deep interior of Iran/Arabia.
            (30.50, 47.50),   # NW corner (Kuwait/Iraq coast)
            (30.00, 50.00),   # Northern Gulf
            (27.50, 52.00),   # Central Gulf (Iranian side)
            (26.50, 56.50),   # Strait of Hormuz
            (24.00, 55.00),   # UAE coast
            (23.50, 51.00),   # Qatar / Saudi approach
            (26.50, 49.50),   # Saudi coast (Dammam)
            (29.00, 48.00),   # Kuwait approach
        ]),
        "zoom": 10,
        "name": "Persian Gulf",
    },
    "GA": {
        "polygon": _parse_polygon("GULF_OF_ADEN_POLYGON", [
            (12.60, 43.30),  # Bab al-Mandab (south)
            (14.50, 44.00),  # Yemeni coast
            (14.00, 48.00),  # Yemeni coast (central)
            (13.50, 50.50),  # Eastern Yemen / Socotra approach
            (12.00, 51.00),  # Cape Guardafui (Horn of Africa)
            (11.00, 49.00),  # Somali coast
            (11.00, 45.00),  # Djibouti approach
            (11.50, 43.50),  # Djibouti coast
        ]),
        "zoom": 10,
        "name": "Gulf of Aden",
    },
    "ECS": {
        "polygon": _parse_polygon("E_CHINA_SEA_POLYGON", [
            (33.50, 121.00),  # Shanghai / Yangtze
            (33.00, 126.00),  # Jeju approach
            (30.00, 128.00),  # Ryukyu Islands (north)
            (27.00, 127.00),  # Okinawa approach
            (25.50, 123.00),  # Taiwan NE tip
            (26.00, 120.50),  # Taiwan Strait north
            (28.00, 121.00),  # Zhejiang coast
            (31.00, 122.00),  # Yangtze Delta
        ]),
        "zoom": 10,
        "name": "East China Sea",
    },
    "MZ": {
        "polygon": _parse_polygon("MOZAMBIQUE_POLYGON", [
            (-12.00, 40.50),  # N Mozambique coast (Comoros approach)
            (-12.50, 44.00),  # NW Madagascar
            (-16.00, 45.50),  # W Madagascar coast
            (-20.00, 44.00),  # SW Madagascar
            (-23.50, 44.00),  # S Madagascar approach
            (-25.00, 40.00),  # S Mozambique Channel
            (-23.00, 35.50),  # S Mozambique coast
            (-18.00, 36.00),  # Central Mozambique coast
            (-14.00, 40.50),  # N Mozambique coast
        ]),
        "zoom": 10,
        "name": "Mozambique Channel",
    },
    "CG": {
        "polygon": _parse_polygon("CAPE_POLYGON", [
            (-33.00, 17.50),  # Table Bay (Cape Town)
            (-34.00, 18.50),  # Cape of Good Hope
            (-34.80, 20.00),  # Cape Agulhas
            (-35.50, 22.00),  # Mossel Bay
            (-34.50, 26.00),  # Port Elizabeth approach
            (-33.50, 26.50),  # Algoa Bay
            (-33.50, 28.00),  # East London approach
            (-33.00, 28.50),  # SE corner (open ocean)
            (-36.50, 25.00),  # Southern offshore
            (-36.50, 16.00),  # SW offshore
            (-34.00, 15.00),  # West coast approach
        ]),
        "zoom": 10,
        "name": "Cape of Good Hope",
    },
    "JV": {
        "polygon": _parse_polygon("JAVA_SEA_POLYGON", [
            # Java Sea water body between Sumatra, Borneo, and Java.
            # Excludes deep interior of each island.
            (-2.50, 105.50),  # SE Sumatra coast
            (-1.50, 107.50),  # Bangka/Belitung islands
            (-1.00, 109.00),  # West Borneo coast
            (-1.50, 111.00),  # SW Borneo coast
            (-3.00, 113.00),  # S Borneo coast
            (-3.50, 114.50),  # SE Borneo approach
            (-5.00, 114.00),  # Java Sea center-east
            (-6.50, 112.50),  # North Java coast (Surabaya)
            (-6.80, 110.50),  # North Java coast (Semarang)
            (-6.50, 108.50),  # North Java coast (Cirebon)
            (-5.80, 106.00),  # Sunda Strait approach
            (-3.50, 105.00),  # SE Sumatra
        ]),
        "zoom": 10,
        "name": "Java Sea",
    },
    "YS": {
        "polygon": _parse_polygon("YELLOW_SEA_POLYGON", [
            (39.50, 119.50),  # Bohai Strait approach
            (38.00, 121.00),  # Shandong Peninsula (east)
            (35.00, 126.00),  # Central Yellow Sea
            (34.00, 126.50),  # SW Korean coast (Jeju approach)
            (36.00, 126.50),  # W Korean coast
            (38.00, 125.00),  # Korean Bay
            (39.50, 124.50),  # Dalian approach
            (40.00, 122.00),  # Liaodong Peninsula
            (39.00, 120.00),  # NW coast
        ]),
        "zoom": 10,
        "name": "Yellow Sea",
    },

    # ── Zoom 9: Open ocean shipping lanes ────────────────────────────────
    "GOM": {
        "polygon": _parse_polygon("GULF_OF_MEXICO_POLYGON", [
            # Gulf of Mexico water body.  Traces US Gulf coast (north),
            # Mexican coast (west/south), and Florida coast (east).
            (30.00, -88.00),   # Mississippi coast
            (29.50, -85.00),   # Florida panhandle
            (28.50, -83.00),   # West Florida coast
            (25.50, -82.00),   # Florida Keys (west)
            (22.00, -82.00),   # Straits of Florida / Cuba
            (20.00, -87.00),   # Yucatan Channel
            (19.00, -92.00),   # Southern Gulf (Campeche)
            (20.00, -96.50),   # Mexican coast (Veracruz)
            (22.00, -97.50),   # Mexican coast (Tampico)
            (26.00, -97.00),   # Texas coast (S)
            (28.50, -95.50),   # Texas coast (Houston)
            (29.50, -93.50),   # Louisiana coast
        ]),
        "zoom": 9,
        "name": "Gulf of Mexico",
    },
    "CAR": {
        "polygon": _parse_polygon("CARIBBEAN_POLYGON", [
            # Caribbean Sea.  North boundary at 18°N (below GOM).
            # Excludes Central American interior; traces coast.
            (18.00, -85.00),   # NW corner (Honduras approach)
            (18.00, -60.00),   # NE corner (Lesser Antilles)
            (10.00, -60.00),   # SE corner (Trinidad approach)
            (10.00, -76.00),   # Colombian coast (east)
            (11.00, -75.00),   # Colombian coast (Cartagena)
            (12.00, -82.00),   # Nicaraguan coast
            (15.00, -83.50),   # Honduras coast
            (16.00, -85.00),   # Honduras/Belize approach
        ]),
        "zoom": 9,
        "name": "Caribbean Sea",
    },
    "USE": {
        "polygon": _parse_polygon("US_EAST_COAST_POLYGON", [
            # US East Coast — ocean side only.
            # West boundary follows the coastline to exclude inland.
            (40.00, -74.00),   # New York / New Jersey coast
            (40.00, -65.00),   # NE corner (open ocean)
            (25.00, -65.00),   # SE corner (open ocean)
            (25.00, -80.50),   # Florida Keys
            (27.00, -80.50),   # SE Florida coast
            (30.50, -81.00),   # Jacksonville coast
            (33.00, -79.00),   # South Carolina coast
            (35.00, -76.00),   # Cape Hatteras
            (37.00, -76.00),   # Chesapeake Bay entrance
            (39.00, -74.50),   # Delaware Bay / NJ coast
        ]),
        "zoom": 9,
        "name": "US East Coast",
    },
    "USW": {
        "polygon": _parse_polygon("US_WEST_COAST_POLYGON", [
            # US/Canada Pacific coast — ocean side only.
            # East boundary follows the coastline to exclude land.
            (50.00, -135.00),  # NW corner (open ocean)
            (50.00, -126.00),  # Vancouver Island approach
            (48.50, -125.00),  # Washington coast
            (46.00, -124.50),  # Oregon coast
            (42.00, -124.50),  # Oregon/California border
            (38.00, -123.50),  # Northern California
            (34.50, -121.00),  # Central California
            (33.00, -118.00),  # Southern California (LA)
            (30.00, -117.50),  # Mexican border
            (30.00, -135.00),  # SW corner (open ocean)
        ]),
        "zoom": 9,
        "name": "US/Canada West Coast",
    },
    "BS": {
        "polygon": _parse_polygon("BALTIC_SEA_POLYGON", [
            # Traces the Baltic coastline to exclude Scandinavian interior.
            # Starts at Skagerrak, follows Swedish coast east, then Finnish
            # coast south-east, down through the Gulf of Finland and Baltic
            # proper, returning along the southern (Polish/German) shore.
            (58.00, 10.00),   # Skagerrak entrance
            (59.00, 11.00),   # Swedish west coast (Gothenburg)
            (59.50, 18.00),   # Stockholm archipelago
            (60.50, 19.50),   # Aland Sea
            (63.50, 20.50),   # Gulf of Bothnia (Swedish side)
            (65.50, 24.00),   # Northern Gulf of Bothnia
            (63.50, 25.50),   # Gulf of Bothnia (Finnish side)
            (60.20, 25.00),   # Gulf of Finland (Helsinki)
            (59.50, 28.00),   # Gulf of Finland (east end)
            (57.50, 27.00),   # Estonian coast
            (56.00, 21.00),   # Latvian/Lithuanian coast
            (54.50, 19.50),   # Gdansk Bay
            (54.00, 14.00),   # German/Polish coast
            (55.50, 10.50),   # Danish straits (south)
        ]),
        "zoom": 9,
        "name": "Baltic Sea",
    },
    "GG": {
        "polygon": _parse_polygon("GULF_OF_GUINEA_POLYGON", [
            (6.00, -8.00),    # Sierra Leone / Liberia coast
            (5.00, -5.00),    # Cote d'Ivoire (Abidjan)
            (5.00, -1.50),    # Ghana coast (Accra)
            (6.00, 1.50),     # Togo / Benin coast
            (6.50, 3.50),     # Lagos approach
            (4.50, 7.00),     # Niger Delta
            (4.00, 9.00),     # Cameroon coast
            (2.00, 9.50),     # Equatorial Guinea
            (0.50, 9.00),     # Gabon coast
            (-1.00, 9.00),    # Gabon / Congo coast
            (-4.00, 10.50),   # SW Congo coast
            (-6.00, 8.00),    # Open ocean (south)
            (-4.00, 0.00),    # Open ocean (SW)
            (0.00, -5.00),    # Open ocean (W)
            (4.00, -8.00),    # Open ocean (NW)
        ]),
        "zoom": 9,
        "name": "Gulf of Guinea",
    },
    "SAW": {
        "polygon": _parse_polygon("S_ATLANTIC_W_POLYGON", [
            (-5.00, -35.00),   # NE Brazil coast (Natal)
            (-8.00, -34.50),   # Recife coast
            (-13.00, -38.50),  # Salvador coast
            (-23.00, -42.00),  # Rio de Janeiro coast
            (-28.00, -48.50),  # S Brazil coast (Florianopolis)
            (-33.00, -52.00),  # Uruguay approach
            (-35.00, -48.00),  # Open ocean (S)
            (-35.00, -30.00),  # Open ocean (SE)
            (-20.00, -25.00),  # Mid-Atlantic
            (-5.00, -28.00),   # Equatorial Atlantic
        ]),
        "zoom": 9,
        "name": "South Atlantic West",
    },
    "SAE": {
        "polygon": _parse_polygon("S_ATLANTIC_E_POLYGON", [
            (-5.00, 10.50),   # Gabon / Congo coast
            (-8.00, 13.00),   # Angola coast (Luanda)
            (-12.00, 13.50),  # Angola coast
            (-17.00, 11.50),  # Namibia north coast
            (-22.00, 14.00),  # Namibia central coast
            (-28.00, 15.50),  # Namibia south coast
            (-30.00, 12.00),  # Open ocean (S)
            (-28.00, 2.00),   # Open ocean (SW)
            (-10.00, 0.00),   # Open ocean (W)
            (-5.00, 5.00),    # Gulf of Guinea approach
        ]),
        "zoom": 9,
        "name": "South Atlantic East",
    },
    "PHI": {
        "polygon": _parse_polygon("PHILIPPINE_SEA_POLYGON", [
            (20.00, 122.00),  # Luzon NE coast
            (18.00, 125.00),  # Philippine Sea (NW)
            (15.00, 130.00),  # Open ocean (N)
            (10.00, 135.00),  # Open ocean (E)
            (5.00, 132.00),   # Open ocean (SE)
            (5.00, 127.00),   # Mindanao east approach
            (8.00, 126.50),   # E Mindanao coast
            (10.00, 125.50),  # Leyte / Samar
            (13.00, 124.00),  # Luzon SE coast
            (18.00, 122.00),  # Luzon east coast
        ]),
        "zoom": 9,
        "name": "Philippine Sea",
    },
    "NOR": {
        "polygon": _parse_polygon("NORWEGIAN_SEA_POLYGON", [
            (62.00, -5.00),   # Shetland / Faroe gap
            (64.00, -8.00),   # Iceland SE approach
            (67.00, -10.00),  # Iceland NE approach
            (71.00, -8.00),   # Jan Mayen area
            (72.00, 5.00),    # Svalbard approach
            (71.00, 15.00),   # N Norway coast (Hammerfest)
            (69.00, 16.00),   # Lofoten Islands
            (67.00, 14.50),   # Bodo coast
            (64.00, 10.00),   # Trondheim coast
            (62.50, 5.50),    # Alesund coast
        ]),
        "zoom": 9,
        "name": "Norwegian Sea",
    },
    "COR": {
        "polygon": _parse_polygon("CORAL_SEA_POLYGON", [
            (-10.00, 145.00),  # Torres Strait / PNG
            (-10.00, 155.00),  # Solomon Sea approach
            (-15.00, 165.00),  # Vanuatu approach
            (-20.00, 170.00),  # New Caledonia east
            (-25.00, 175.00),  # Open Tasman (NE)
            (-35.00, 175.00),  # NZ approach (north)
            (-37.00, 170.00),  # Tasman Sea
            (-38.00, 155.00),  # SE Australia approach
            (-33.00, 152.00),  # Sydney coast
            (-25.00, 153.50),  # Brisbane coast
            (-18.00, 147.00),  # Townsville / GBR
            (-14.00, 145.50),  # Cairns / GBR north
        ]),
        "zoom": 9,
        "name": "Coral Sea / Tasman",
    },
    "SIO": {
        "polygon": _parse_polygon("S_INDIAN_OCEAN_POLYGON", [
            (-15.00, 42.00),  # Madagascar south coast
            (-15.00, 55.00),  # Mauritius / Reunion approach
            (-15.00, 75.00),  # Central Indian Ocean
            (-20.00, 80.00),  # Open ocean (NE)
            (-35.00, 80.00),  # Open ocean (SE)
            (-40.00, 60.00),  # Southern Ocean boundary
            (-40.00, 42.00),  # Open ocean (SW)
            (-30.00, 40.00),  # Mozambique Channel south
            (-20.00, 40.00),  # E Africa offshore
        ]),
        "zoom": 9,
        "name": "South Indian Ocean",
    },
    "NAM": {
        "polygon": _parse_polygon("N_ATLANTIC_M_POLYGON", [
            (45.00, -50.00),  # NW (Grand Banks approach)
            (45.00, -30.00),  # NE (open ocean)
            (40.00, -25.00),  # E (Azores approach)
            (30.00, -25.00),  # SE (Canary Islands approach)
            (30.00, -45.00),  # SW (open ocean)
            (35.00, -50.00),  # W (Bermuda approach)
        ]),
        "zoom": 9,
        "name": "North Atlantic Mid",
    },
    "SEP": {
        "polygon": _parse_polygon("SE_PACIFIC_POLYGON", [
            (-5.00, -82.00),   # Ecuador coast
            (-6.00, -81.00),   # N Peru coast
            (-12.00, -77.50),  # Lima / Callao
            (-18.00, -71.50),  # S Peru / N Chile coast
            (-23.00, -70.50),  # Antofagasta coast
            (-30.00, -71.50),  # Central Chile coast
            (-35.00, -72.00),  # S Chile coast
            (-35.00, -85.00),  # Open ocean (SW)
            (-20.00, -90.00),  # Open ocean (W)
            (-5.00, -88.00),   # Galapagos area
        ]),
        "zoom": 9,
        "name": "Southeast Pacific",
    },
    "CEP": {
        "polygon": _parse_polygon("C_E_PACIFIC_POLYGON", [
            (30.00, -150.00),  # NW (open ocean)
            (30.00, -125.00),  # NE (off California)
            (20.00, -118.00),  # Hawaii–Mexico corridor
            (10.00, -115.00),  # Central American approach
            (5.00, -120.00),   # SE (equatorial)
            (5.00, -145.00),   # SW (open ocean)
            (15.00, -150.00),  # Hawaii south approach
        ]),
        "zoom": 9,
        "name": "Central East Pacific",
    },
    "SOA": {
        "polygon": _parse_polygon("S_ATLANTIC_C_POLYGON", [
            (-10.00, -25.00),  # NW (mid-Atlantic)
            (-8.00, -14.00),   # Ascension Island area
            (-10.00, -5.00),   # NE (approach Gulf of Guinea)
            (-20.00, 0.00),    # E (open ocean)
            (-35.00, 0.00),    # SE (open ocean)
            (-35.00, -20.00),  # S (open ocean)
            (-25.00, -25.00),  # SW (open ocean)
        ]),
        "zoom": 9,
        "name": "South Atlantic Central",
    },
    "NAE": {
        "polygon": _parse_polygon("N_ATLANTIC_E_POLYGON", [
            (55.00, -25.00),  # NW (open ocean)
            (55.00, -12.00),  # NE (Ireland west coast)
            (52.00, -10.00),  # SW Ireland
            (48.00, -8.00),   # Brest / Biscay approach
            (43.00, -9.50),   # NW Spain (Galicia)
            (40.00, -10.00),  # Portuguese coast
            (40.00, -20.00),  # SW (open ocean)
            (45.00, -25.00),  # W (Azores north)
        ]),
        "zoom": 9,
        "name": "North Atlantic East",
    },
    "NAW": {
        "polygon": _parse_polygon("N_ATLANTIC_W_POLYGON", [
            (45.00, -66.00),  # Nova Scotia coast
            (43.50, -60.00),  # Grand Banks approach
            (42.00, -50.00),  # Open ocean (NE)
            (30.00, -50.00),  # Open ocean (SE)
            (30.00, -70.00),  # Bermuda–Florida corridor
            (33.00, -78.00),  # Cape Hatteras approach
            (38.00, -75.00),  # Mid-Atlantic coast
            (41.00, -70.00),  # New England coast
            (43.00, -66.00),  # Gulf of Maine
        ]),
        "zoom": 9,
        "name": "North Atlantic West",
    },
    "MEW": {
        "polygon": _parse_polygon("MED_WEST_POLYGON", [
            # Traces the western Mediterranean basin.  North coast follows
            # Spain → France → Italy; south coast follows Morocco → Algeria
            # → Tunisia.  Excludes deep inland areas on both shores.
            (36.20, -5.30),   # Strait of Gibraltar (north)
            (36.70, -2.00),   # SE Spain (Almeria)
            (38.00, 0.00),    # Valencia coast
            (39.50, 2.50),    # Balearic Islands
            (41.50, 3.00),    # French coast (Montpellier)
            (43.30, 5.50),    # French Riviera (Marseille)
            (43.80, 7.50),    # Nice / Monaco
            (43.50, 10.00),   # Ligurian Sea (La Spezia)
            (41.00, 13.00),   # Italian west coast (south of Rome)
            (39.00, 14.50),   # Naples / Tyrrhenian Sea
            (37.50, 15.00),   # Sicily (NE tip)
            (36.80, 14.00),   # Sicily (south coast)
            (35.50, 11.00),   # Tunisian coast
            (34.00, 8.00),    # Eastern Algeria coast
            (35.50, 2.00),    # Western Algeria coast
            (35.80, -2.00),   # Northern Morocco coast
            (35.80, -5.30),   # Strait of Gibraltar (south)
        ]),
        "zoom": 9,
        "name": "Mediterranean West",
    },
    "MEE": {
        "polygon": _parse_polygon("MED_EAST_POLYGON", [
            # Eastern Mediterranean.  North coast: Italy boot → Greece →
            # Turkey.  South coast: Libya → Egypt → Israel → Lebanon.
            (38.00, 15.00),   # Southern Italy (Calabria)
            (40.00, 18.50),   # Adriatic (Puglia heel)
            (39.50, 20.00),   # W Greece (Ionian)
            (37.50, 21.00),   # Peloponnese
            (35.00, 24.00),   # Crete (south)
            (36.50, 27.50),   # Dodecanese / E Aegean
            (37.00, 30.00),   # Turkish SW coast (Antalya)
            (36.50, 35.00),   # Turkish S coast (Mersin)
            (35.00, 35.50),   # Cyprus / Syria coast
            (33.00, 35.00),   # Lebanese coast
            (31.50, 34.00),   # Israeli coast
            (31.00, 32.50),   # Egyptian coast (Port Said)
            (31.00, 28.00),   # Egyptian coast (Alexandria)
            (32.50, 23.00),   # Libyan coast (Benghazi)
            (33.00, 18.00),   # Gulf of Sidra (Libya)
            (35.50, 15.00),   # Tunisian/Sicilian channel
        ]),
        "zoom": 9,
        "name": "Mediterranean East",
    },
    "ARS": {
        "polygon": _parse_polygon("ARABIAN_SEA_POLYGON", [
            (24.50, 57.00),  # Oman coast (Muscat)
            (22.00, 60.00),  # Oman SE coast
            (20.00, 63.00),  # Open ocean (Oman east)
            (16.00, 72.00),  # India west coast (Mumbai approach)
            (12.00, 74.00),  # India west coast (Goa)
            (8.00, 76.00),   # Kerala coast
            (8.00, 68.00),   # Lakshadweep Sea
            (10.00, 60.00),  # Central Arabian Sea
            (12.50, 52.00),  # Socotra approach
            (15.00, 52.50),  # Yemen east coast
            (20.00, 57.00),  # Oman north coast
        ]),
        "zoom": 9,
        "name": "Arabian Sea",
    },
    "BOB": {
        "polygon": _parse_polygon("BAY_OF_BENGAL_POLYGON", [
            (20.00, 87.00),  # Bangladesh coast (Chittagong)
            (16.00, 94.00),  # Myanmar coast
            (14.00, 95.00),  # Andaman Sea approach
            (10.00, 93.00),  # Andaman Islands
            (6.00, 92.00),   # Nicobar Islands
            (5.00, 82.00),   # Sri Lanka SE approach
            (7.00, 80.00),   # Sri Lanka west coast
            (10.00, 80.00),  # SE India coast (Chennai)
            (13.00, 80.50),  # Andhra coast
            (16.00, 82.00),  # East India coast
            (19.00, 85.00),  # Odisha coast
        ]),
        "zoom": 9,
        "name": "Bay of Bengal",
    },
    "WP": {
        "polygon": _parse_polygon("W_PACIFIC_POLYGON", [
            (40.00, 130.00),  # Sea of Japan (east)
            (38.00, 135.00),  # Japan west coast
            (35.00, 140.00),  # Tokyo Bay approach
            (32.00, 133.00),  # Shikoku south coast
            (30.00, 131.00),  # Kyushu SE coast
            (25.00, 130.00),  # Ryukyu Islands (south)
            (25.00, 140.00),  # Bonin Islands approach
            (30.00, 145.00),  # Open Pacific (E)
            (35.00, 145.00),  # NE Japan offshore
            (40.00, 143.00),  # Hokkaido SE coast
        ]),
        "zoom": 9,
        "name": "Western Pacific",
    },
    "IO": {
        "polygon": _parse_polygon("INDIAN_OCEAN_POLYGON", [
            (0.00, 55.00),    # Somali basin
            (0.00, 65.00),    # Central Indian Ocean
            (-5.00, 75.00),   # Maldives / Sri Lanka approach
            (-10.00, 80.00),  # Open ocean (NE)
            (-15.00, 80.00),  # Open ocean (E)
            (-15.00, 65.00),  # Diego Garcia area
            (-12.00, 55.00),  # Seychelles / Madagascar approach
            (-5.00, 50.00),   # Somali coast approach
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
            (41.80, 139.80),  # Honshu NW (Aomori)
            (41.80, 140.80),  # Strait center
            (41.60, 141.00),  # Hokkaido SW
            (41.20, 141.00),  # Hokkaido coast
            (41.20, 140.00),  # Strait center (south)
            (41.40, 139.80),  # Honshu coast
        ]),
        "zoom": 12,
        "name": "Strait of Tsugaru",
    },
    "TOR": {
        "polygon": _parse_polygon("TORRES_POLYGON", [
            (-9.50, 142.00),  # PNG south coast
            (-9.50, 143.50),  # NE corner (water)
            (-10.50, 143.00), # Cape York tip approach
            (-10.80, 142.00), # Cape York east coast
            (-10.50, 141.50), # Torres Strait west
            (-9.80, 141.80),  # PNG coast
        ]),
        "zoom": 12,
        "name": "Torres Strait",
    },
    "SHA": {
        "polygon": _parse_polygon("SHANGHAI_POLYGON", [
            (31.80, 121.20),  # Yangtze mouth (north)
            (31.50, 122.60),  # Offshore east
            (30.50, 122.60),  # Offshore SE
            (30.50, 121.80),  # Hangzhou Bay
            (30.80, 121.50),  # Zhoushan Islands
            (31.20, 121.00),  # Shanghai coast
        ]),
        "zoom": 12,
        "name": "Shanghai / Yangtze Approach",
    },
    "BNF": {
        "polygon": _parse_polygon("BONIFACIO_POLYGON", [
            (41.30, 8.70),   # Corsica south coast
            (41.30, 9.40),   # Corsica SE tip
            (41.00, 9.50),   # Strait center (east)
            (40.90, 9.30),   # Sardinia NE tip
            (41.00, 8.80),   # Strait center (west)
            (41.15, 8.60),   # Corsica SW coast
        ]),
        "zoom": 12,
        "name": "Strait of Bonifacio",
    },
    "CK": {
        "polygon": _parse_polygon("COOK_POLYGON", [
            (-41.00, 174.30),  # Wellington approach
            (-41.00, 175.00),  # NE (open water)
            (-41.70, 175.00),  # SE (open water)
            (-41.70, 174.00),  # SW approach
            (-41.30, 174.00),  # N Island south coast
        ]),
        "zoom": 12,
        "name": "Cook Strait",
    },
    "ORE": {
        "polygon": _parse_polygon("ORESUND_POLYGON", [
            (56.10, 12.50),  # Helsingor (Denmark)
            (56.10, 12.90),  # Helsingborg (Sweden)
            (55.70, 13.00),  # Malmo approach
            (55.50, 12.80),  # Southern Oresund
            (55.60, 12.40),  # Copenhagen approach
            (55.90, 12.50),  # Central strait
        ]),
        "zoom": 12,
        "name": "Oresund",
    },
    "MAR": {
        "polygon": _parse_polygon("MARMARA_POLYGON", [
            (41.10, 27.50),  # Gallipoli coast
            (41.00, 29.00),  # Bosporus SW approach
            (40.70, 29.40),  # Izmit Bay
            (40.30, 29.00),  # Anatolian coast
            (40.50, 27.50),  # Dardanelles NE approach
            (40.80, 27.40),  # Thracian coast
        ]),
        "zoom": 12,
        "name": "Sea of Marmara",
    },

    # ── Zoom 11: Wide straits (expansion) ──────────────────────────────────
    "TSM": {
        "polygon": _parse_polygon("TSUSHIMA_POLYGON", [
            (35.00, 128.50),  # S Korean coast (Busan)
            (34.50, 129.50),  # Tsushima Island (east)
            (33.50, 129.50),  # SE water
            (33.50, 128.50),  # SW water
            (34.00, 128.00),  # Korean coast
            (34.80, 128.30),  # Busan approach
        ]),
        "zoom": 11,
        "name": "Tsushima Strait",
    },
    "HOU": {
        "polygon": _parse_polygon("HOUSTON_POLYGON", [
            (29.80, -95.20),  # Houston Ship Channel
            (29.50, -94.00),  # Galveston Bay
            (28.80, -93.50),  # Offshore (SE)
            (28.50, -94.50),  # Offshore (S)
            (28.80, -95.50),  # Freeport approach
            (29.50, -95.50),  # Texas coast
        ]),
        "zoom": 11,
        "name": "Houston / Texas Coast",
    },
    "BAS": {
        "polygon": _parse_polygon("BASS_POLYGON", [
            (-38.30, 144.00),  # Victoria coast (Cape Otway)
            (-38.00, 147.00),  # Wilsons Promontory
            (-38.50, 148.50),  # Victoria east coast
            (-40.50, 148.00),  # Tasmania NE coast
            (-41.00, 145.00),  # Tasmania NW coast
            (-40.00, 143.50),  # King Island
            (-38.80, 143.50),  # Cape Otway offshore
        ]),
        "zoom": 11,
        "name": "Bass Strait",
    },
    "WIN": {
        "polygon": _parse_polygon("WINDWARD_POLYGON", [
            (20.20, -74.00),  # Cuba SE coast
            (20.00, -73.00),  # Passage center (N)
            (19.00, -72.50),  # Haiti NW coast
            (18.50, -73.50),  # Haiti coast
            (19.00, -74.50),  # Jamaica NE approach
            (20.00, -74.50),  # Cuba south coast
        ]),
        "zoom": 11,
        "name": "Windward Passage",
    },
    "MON": {
        "polygon": _parse_polygon("MONA_POLYGON", [
            (19.00, -67.00),  # Dominican Republic east coast
            (19.50, -66.50),  # NE water
            (18.50, -66.50),  # Puerto Rico NW coast
            (17.50, -67.00),  # SE water
            (17.80, -68.00),  # SW water
            (18.50, -68.50),  # Dominican Republic SE coast
        ]),
        "zoom": 11,
        "name": "Mona Passage",
    },
    "LUZ": {
        "polygon": _parse_polygon("LUZON_POLYGON", [
            (22.00, 120.00),  # Taiwan south coast
            (21.50, 122.00),  # Bashi Channel
            (20.00, 122.50),  # Babuyan Islands
            (18.50, 121.50),  # NW Luzon coast
            (18.50, 119.50),  # South China Sea
            (20.00, 119.00),  # Pratas approach
        ]),
        "zoom": 11,
        "name": "Luzon Strait",
    },
    "GSA": {
        "polygon": _parse_polygon("GULF_SUEZ_APPROACH_POLYGON", [
            (29.80, 32.50),  # Suez Canal south end
            (29.50, 33.50),  # W Sinai coast
            (28.00, 34.50),  # Sharm el-Sheikh approach
            (27.50, 34.00),  # Red Sea entrance
            (27.80, 33.00),  # Hurghada coast
            (29.00, 32.50),  # Egyptian coast
        ]),
        "zoom": 11,
        "name": "Gulf of Suez Approach",
    },
    "SAN": {
        "polygon": _parse_polygon("SANTOS_POLYGON", [
            (-23.00, -45.00),  # Rio coast approach
            (-23.50, -44.50),  # Ilha Grande Bay
            (-24.00, -45.50),  # Santos coast
            (-25.00, -46.50),  # Offshore (S)
            (-25.00, -46.00),  # Paranagua approach
            (-24.00, -46.80),  # Offshore (W)
            (-23.50, -46.50),  # Santos port
        ]),
        "zoom": 11,
        "name": "Santos / SE Brazil Coast",
    },

    # ── Zoom 10: Regional corridors (expansion) ───────────────────────────
    "CHR": {
        "polygon": _parse_polygon("CAPE_HORN_POLYGON", [
            (-54.00, -72.00),  # Magellan Strait approach
            (-54.50, -68.00),  # Beagle Channel
            (-55.00, -65.00),  # Cape Horn
            (-58.00, -65.00),  # Drake Passage (south)
            (-58.00, -72.00),  # Drake Passage (SW)
            (-56.00, -70.00),  # Open ocean
        ]),
        "zoom": 10,
        "name": "Cape Horn / Drake Passage",
    },
    "BFS": {
        "polygon": _parse_polygon("BANDA_FLORES_POLYGON", [
            (-5.00, 118.00),  # Flores Sea (west)
            (-5.50, 121.00),  # Flores coast
            (-6.00, 124.00),  # Flores Sea (east)
            (-5.50, 128.00),  # Banda Sea (NE)
            (-7.00, 128.00),  # Banda Sea (SE)
            (-8.50, 125.00),  # Timor approach
            (-8.50, 120.00),  # Timor Sea (W)
            (-7.50, 117.50),  # S Borneo / E Java approach
        ]),
        "zoom": 10,
        "name": "Banda Sea / Flores Sea",
    },

    # ── Zoom 9: Open ocean (expansion) ─────────────────────────────────────
    "NWP": {
        "polygon": _parse_polygon("N_PACIFIC_POLYGON", [
            (50.00, 155.00),  # Kamchatka SE approach
            (45.00, 165.00),  # NW Pacific (Aleutian arc)
            (40.00, 175.00),  # Open ocean (E)
            (30.00, 170.00),  # Central Pacific (mid)
            (30.00, 155.00),  # Open ocean (S)
            (35.00, 145.00),  # Japan east approach
            (42.00, 148.00),  # Hokkaido SE offshore
        ]),
        "zoom": 9,
        "name": "North Pacific",
    },
    "SPO": {
        "polygon": _parse_polygon("S_PACIFIC_POLYGON", [
            (-15.00, 175.00),  # Fiji / Tonga area
            (-20.00, -175.00), # Dateline crossing (east side)
            (-25.00, -165.00), # Central South Pacific
            (-35.00, -150.00), # SE Pacific
            (-40.00, -140.00), # Open ocean (SE)
            (-40.00, 175.00),  # NZ south approach
            (-35.00, 175.00),  # NZ north approach
            (-20.00, 175.00),  # Fiji approach
        ]),
        "zoom": 9,
        "name": "South Pacific",
    },
    "ARC": {
        "polygon": _parse_polygon("ARCTIC_POLYGON", [
            (78.00, 30.00),   # Svalbard / Barents Sea
            (76.00, 50.00),   # Novaya Zemlya approach
            (74.00, 70.00),   # Kara Sea
            (72.00, 80.00),   # Yamal coast
            (71.00, 100.00),  # Laptev approach
            (68.00, 90.00),   # Yenisei estuary
            (68.00, 70.00),   # Ob estuary
            (70.00, 50.00),   # Russian Arctic coast
            (72.00, 40.00),   # Barents Sea (south)
            (76.00, 30.00),   # Svalbard approach
        ]),
        "zoom": 9,
        "name": "Northern Sea Route / Arctic",
    },
    "NNC": {
        "polygon": _parse_polygon("NORW_CORRIDOR_POLYGON", [
            (62.00, 0.00),    # Norwegian Sea approach
            (60.50, 5.00),    # Bergen coast
            (58.50, 6.00),    # Stavanger coast
            (57.50, 8.00),    # Southern Norway coast
            (55.50, 8.50),    # Jutland (Denmark west)
            (55.00, 4.00),    # Dogger Bank area
            (56.00, -2.00),   # North Sea (W)
            (58.00, -4.00),   # Orkney / Shetland approach
            (60.00, -3.00),   # Shetland east
        ]),
        "zoom": 9,
        "name": "North Sea–Norwegian Corridor",
    },
    "WAO": {
        "polygon": _parse_polygon("W_AFRICA_OFFSHORE_POLYGON", [
            (-4.00, 10.50),   # Gabon coast
            (-6.00, 12.00),   # Congo/Angola border
            (-9.00, 13.00),   # Luanda coast
            (-12.00, 13.50),  # Angola coast
            (-15.50, 12.00),  # Namibia border
            (-18.00, 11.50),  # Namibia north coast
            (-18.00, 8.00),   # Open ocean (SW)
            (-10.00, 5.00),   # Open ocean (W)
            (-4.00, 7.00),    # Open ocean (NW)
        ]),
        "zoom": 9,
        "name": "West Africa Offshore (Angola)",
    },
    "EAF": {
        "polygon": _parse_polygon("E_AFRICA_COAST_POLYGON", [
            (0.00, 41.00),    # Kenya / Somali border coast
            (-1.00, 41.50),   # Mombasa approach
            (-4.00, 39.50),   # Tanzania coast (Dar es Salaam)
            (-8.00, 39.50),   # S Tanzania coast
            (-10.50, 40.50),  # Mozambique north coast
            (-12.00, 44.00),  # Comoros approach
            (-8.00, 48.00),   # Open ocean (E)
            (-2.00, 48.00),   # Open ocean (NE)
            (0.00, 45.00),    # Somali coast
        ]),
        "zoom": 9,
        "name": "East Africa Coast",
    },
    "NEP": {
        "polygon": _parse_polygon("NE_PACIFIC_POLYGON", [
            (60.00, -148.00),  # Gulf of Alaska (central)
            (58.00, -137.00),  # Glacier Bay approach
            (54.00, -133.00),  # Haida Gwaii
            (50.00, -128.00),  # Vancouver Island (N)
            (48.00, -126.00),  # Vancouver Island (S)
            (48.00, -140.00),  # Open ocean (SW)
            (55.00, -150.00),  # Kodiak approach
        ]),
        "zoom": 9,
        "name": "NE Pacific (Alaska–BC)",
    },
}
