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
            # Clockwise: south along Borneo east coast, cross at south,
            # north along Sulawesi west coast, close at north.
            (-0.50, 117.00),  # NW — off NE Borneo coast
            (-2.00, 116.80),  # W — East Kalimantan coast (Samarinda)
            (-4.00, 117.50),  # SW — SE Borneo coast (south of Balikpapan)
            (-5.20, 119.00),  # S — southern strait opening
            (-5.00, 119.80),  # SE — SW Sulawesi (Makassar city)
            (-3.00, 119.50),  # E — West Sulawesi coast
            (-1.00, 120.50),  # NE — Central Sulawesi coast
            (-0.50, 119.50),  # N — northern strait
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
            # North Sea — generous water coverage.
            (61.00, -2.00),   # Shetland Islands (south)
            (59.00, -3.00),   # Orkney Islands
            (58.60, -3.20),   # N Scotland (Pentland Firth)
            (57.70, -1.80),   # Aberdeen coast
            (56.30, -1.80),   # Dundee / Firth of Tay
            (56.00, -2.50),   # Edinburgh / Firth of Forth
            (55.50, -1.50),   # NE England (Newcastle)
            (54.60, -0.50),   # Yorkshire coast (Whitby)
            (53.60, 0.20),    # Humber estuary
            (52.50, 1.80),    # East Anglia (Great Yarmouth)
            (51.40, 1.50),    # Thames Estuary
            (51.00, 2.50),    # Dover / Calais
            (51.40, 3.50),    # Belgian coast (Zeebrugge)
            (51.90, 4.50),    # Rotterdam / Hook of Holland
            (53.00, 5.00),    # Dutch coast (Den Helder)
            (53.50, 6.50),    # Frisian Islands
            (54.00, 7.50),    # German Bight (Borkum)
            (54.30, 8.50),    # Schleswig coast
            (55.00, 8.50),    # S Denmark (Esbjerg)
            (55.80, 8.20),    # Jutland west coast
            (57.00, 8.30),    # NW Jutland (Thyboron)
            (57.70, 10.50),   # Skagen tip
            (58.20, 6.50),    # S Norway coast (Kristiansand)
            (59.00, 5.50),    # Stavanger approach
            (60.40, 5.00),    # Bergen approach
            (61.00, 4.50),    # Sognefjord entrance
            (62.00, 4.00),    # Alesund approach
            (62.00, -1.00),   # Open sea boundary (NW)
        ]),
        "zoom": 10,
        "name": "North Sea",
    },
    "BLK": {
        "polygon": _parse_polygon("BLACK_SEA_POLYGON", [
            # Black Sea — generous coastline tracing to capture all water.
            (41.30, 29.10),   # Bosporus N entrance
            (42.00, 28.00),   # Bulgarian coast (Burgas)
            (43.20, 28.00),   # Bulgarian coast (Varna)
            (44.40, 29.00),   # Romanian coast (Constanta approach)
            (45.30, 29.80),   # Danube Delta
            (46.00, 30.80),   # Ukrainian coast (Odesa)
            (46.30, 32.00),   # Kherson approach
            (46.20, 33.80),   # Crimean west (Yevpatoria)
            (45.50, 33.50),   # Crimean SW (Sevastopol)
            (44.40, 34.00),   # Crimean S coast (Yalta)
            (45.00, 36.50),   # Kerch Strait approach
            (45.30, 37.50),   # Sea of Azov entrance
            (44.60, 38.00),   # Novorossiysk coast
            (44.00, 39.00),   # Russian coast (Tuapse)
            (43.40, 40.00),   # Sochi coast
            (42.50, 41.50),   # Georgian coast (Poti)
            (41.80, 41.80),   # Georgian coast (Batumi)
            (41.30, 41.00),   # Turkey NE coast (Rize)
            (41.00, 40.50),   # Turkey coast (Trabzon)
            (41.30, 38.00),   # Turkey coast (Giresun)
            (41.70, 36.00),   # Turkey coast (Samsun)
            (42.00, 34.00),   # Turkey coast (Sinop)
            (41.70, 32.50),   # Turkey coast (Zonguldak)
            (41.20, 30.00),   # Turkey coast (Istanbul approach)
        ]),
        "zoom": 10,
        "name": "Black Sea",
    },
    "GOT": {
        "polygon": _parse_polygon("GULF_OF_THAILAND_POLYGON", [
            # Gulf of Thailand + S. China Sea approach — generous coverage.
            (13.50, 100.20),  # Upper Gulf (Bangkok approach)
            (13.20, 100.90),  # Eastern upper Gulf
            (12.60, 101.00),  # Pattaya coast
            (11.50, 102.50),  # Cambodian coast (Sihanoukville)
            (10.50, 104.30),  # S Cambodia / Phu Quoc
            (9.50, 105.50),   # Mekong Delta
            (8.50, 106.70),   # SE Vietnam coast
            (6.50, 106.50),   # Southern approach
            (4.00, 104.50),   # East coast Malay Peninsula (Kuantan approach)
            (2.00, 104.50),   # E Malaysia (Tioman area)
            (1.50, 103.80),   # Singapore NE approach
            (1.50, 103.30),   # Singapore/Malacca boundary
            (2.50, 102.00),   # E Malay coast (Mersing)
            (4.00, 101.50),   # E Malay coast (Kuala Terengganu)
            (6.00, 100.50),   # Thai-Malay border (Kota Bharu)
            (7.50, 99.50),    # Southern Thai coast (Nakhon)
            (9.50, 99.50),    # Chumphon coast
            (10.50, 99.00),   # Gulf of Thailand west
            (12.50, 99.80),   # Upper Gulf west
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
            # South China Sea — generous water coverage including Gulf of Tonkin.
            (21.50, 107.50),  # N Vietnam coast (Haiphong)
            (20.00, 106.50),  # Gulf of Tonkin west
            (18.00, 106.50),  # Central Vietnam coast (Vinh)
            (16.00, 108.00),  # Vietnam coast (Da Nang)
            (12.00, 109.30),  # SE Vietnam coast (Nha Trang)
            (10.00, 107.00),  # S Vietnam coast (Vung Tau)
            (8.50, 106.80),   # Mekong Delta approach
            (4.00, 108.00),   # S approach
            (2.00, 109.50),   # Borneo NW coast (Kuching)
            (3.00, 110.50),   # Sarawak coast
            (5.00, 113.00),   # Brunei approach
            (7.00, 116.50),   # Spratly area
            (8.00, 117.50),   # Palawan approach
            (10.50, 118.00),  # Palawan north
            (12.50, 120.00),  # Mindoro Strait
            (14.50, 119.50),  # W Luzon coast (Manila)
            (18.50, 118.00),  # NW Luzon coast
            (20.50, 117.00),  # S Taiwan Strait
            (21.50, 114.00),  # Hong Kong / Macau
            (22.00, 113.00),  # Pearl River Delta
            (21.00, 110.50),  # Hainan east coast
            (19.50, 109.00),  # Hainan south coast
            (20.00, 108.50),  # Hainan SW
        ]),
        "zoom": 10,
        "name": "South China Sea",
    },
    "RS": {
        "polygon": _parse_polygon("RED_SEA_POLYGON", [
            # Red Sea — follows both coastlines generously seaward.
            (29.50, 32.50),   # Gulf of Suez north (Suez city)
            (28.00, 33.50),   # Gulf of Suez
            (27.80, 34.00),   # Hurghada coast
            (26.50, 34.00),   # Egyptian coast
            (24.00, 35.30),   # Egyptian coast (Marsa Alam)
            (22.50, 36.50),   # Egyptian/Sudanese border coast
            (21.00, 37.50),   # Sudanese coast (Port Sudan)
            (19.50, 37.50),   # Sudanese coast
            (18.50, 38.50),   # Eritrean coast
            (16.00, 40.00),   # Eritrean coast (Massawa)
            (14.50, 42.00),   # Eritrea / Djibouti approach
            (12.80, 43.30),   # Bab al-Mandab approach
            (13.50, 43.50),   # Yemen coast (Mocha)
            (15.00, 42.50),   # Yemen coast
            (17.50, 41.50),   # Saudi / Yemen border coast
            (19.50, 40.50),   # Saudi coast (Farasan Islands)
            (21.50, 39.00),   # Saudi coast (Jeddah)
            (23.00, 38.50),   # Saudi coast
            (25.00, 37.00),   # Saudi coast (Yanbu)
            (26.50, 36.50),   # Saudi coast (Duba)
            (28.00, 35.00),   # Gulf of Aqaba mouth
            (29.50, 34.80),   # Gulf of Aqaba (Eilat/Aqaba)
        ]),
        "zoom": 10,
        "name": "Red Sea",
    },
    "PG": {
        "polygon": _parse_polygon("PERSIAN_GULF_POLYGON", [
            # Persian Gulf — clockwise: Arabian shore (NW→SE),
            # Hormuz, Iranian shore (SE→NW).
            # ── Arabian shore (NW → SE) ──
            (30.00, 48.50),   # Shatt al-Arab (NW head of Gulf)
            (29.30, 48.50),   # Kuwait Bay
            (29.00, 48.80),   # Kuwait coast
            (28.50, 49.50),   # Saudi coast (Jubail)
            (27.00, 49.80),   # Saudi coast (Dammam)
            (26.00, 50.30),   # Bahrain approach
            (25.50, 50.80),   # Qatar west coast
            (25.30, 51.50),   # Qatar east coast (Doha)
            (24.80, 51.80),   # Qatar south
            (24.50, 53.00),   # UAE coast (Abu Dhabi approach)
            (24.00, 54.50),   # Abu Dhabi coast
            (24.50, 55.50),   # Dubai approach
            (25.50, 56.50),   # Fujairah / Hormuz
            # ── Strait of Hormuz ──
            (26.50, 56.50),   # Strait of Hormuz
            # ── Iranian shore (SE → NW) ──
            (27.20, 56.00),   # Iran (Bandar Abbas)
            (27.50, 54.00),   # Iran (Kish Island)
            (28.00, 52.00),   # Iran (Bushehr)
            (29.00, 50.50),   # Iran (Kharg Island)
            (30.00, 49.50),   # Iran (Abadan)
        ]),
        "zoom": 10,
        "name": "Persian Gulf",
    },
    "GA": {
        "polygon": _parse_polygon("GULF_OF_ADEN_POLYGON", [
            # Gulf of Aden — traces Yemen coast (N) and Somalia coast (S).
            (12.60, 43.30),   # Bab al-Mandab entrance
            (12.80, 44.50),   # Yemeni coast
            (13.50, 45.50),   # Yemeni coast (Aden)
            (14.00, 47.00),   # Yemeni coast (Mukalla)
            (14.50, 49.00),   # Yemeni coast
            (13.80, 50.50),   # E Yemen (Socotra approach)
            (12.50, 54.00),   # Socotra Island (generous)
            (12.00, 52.00),   # Cape Guardafui
            (11.50, 50.50),   # Horn of Africa (tip)
            (10.50, 49.00),   # Somali coast
            (10.80, 47.00),   # Somali coast (Bosaso)
            (11.00, 45.00),   # Somali coast
            (11.50, 43.30),   # Djibouti coast
        ]),
        "zoom": 10,
        "name": "Gulf of Aden",
    },
    "ECS": {
        "polygon": _parse_polygon("E_CHINA_SEA_POLYGON", [
            # East China Sea — generous water coverage.
            (34.00, 120.50),  # Jiangsu coast
            (32.50, 121.50),  # Yangtze Delta / Shanghai
            (30.50, 122.50),  # Zhoushan Islands
            (29.00, 122.00),  # Zhejiang coast (Wenzhou)
            (27.50, 121.00),  # Fujian coast (Fuzhou)
            (25.50, 120.00),  # Taiwan Strait N entrance
            (25.00, 122.50),  # Taiwan NE coast
            (26.00, 125.00),  # Ryukyu chain (south)
            (28.00, 127.50),  # Amami Islands
            (30.00, 128.50),  # Ryukyu N / Kyushu S approach
            (31.50, 128.00),  # Open ECS (E)
            (33.00, 127.00),  # Jeju Island approach
            (34.50, 126.00),  # SW Korean coast
            (34.50, 124.00),  # Yellow Sea border
        ]),
        "zoom": 10,
        "name": "East China Sea",
    },
    "MZ": {
        "polygon": _parse_polygon("MOZAMBIQUE_POLYGON", [
            # Mozambique Channel — traces both coastlines generously.
            (-11.00, 40.00),  # N Mozambique (Pemba)
            (-12.50, 40.80),  # N Mozambique (Ilha de Mocambique)
            (-14.00, 40.50),  # Mozambique coast
            (-16.00, 39.50),  # Quelimane coast
            (-18.00, 36.50),  # Beira approach
            (-20.00, 35.00),  # S Mozambique (Save River)
            (-23.00, 35.30),  # Inhambane coast
            (-25.00, 34.00),  # Maputo approach
            (-26.50, 35.00),  # S Mozambique
            (-26.50, 40.00),  # Open ocean (S)
            (-25.00, 44.00),  # S Madagascar approach
            (-23.00, 44.00),  # SW Madagascar (Toliara)
            (-20.00, 44.50),  # W Madagascar (Morondava)
            (-17.00, 44.00),  # NW Madagascar (Maintirano)
            (-15.50, 45.50),  # NW Madagascar (Mahajanga)
            (-12.50, 48.50),  # N Madagascar tip (Nosy Be)
            (-11.50, 44.00),  # Comoros Islands
            (-11.00, 42.00),  # N Channel entrance
        ]),
        "zoom": 10,
        "name": "Mozambique Channel",
    },
    "CG": {
        "polygon": _parse_polygon("CAPE_POLYGON", [
            # Cape of Good Hope — generous offshore extension to capture shipping lanes.
            (-31.50, 17.50),  # W coast (Saldanha Bay)
            (-33.00, 17.50),  # Table Bay (Cape Town)
            (-34.00, 18.50),  # Cape of Good Hope
            (-34.80, 20.00),  # Cape Agulhas (southernmost point)
            (-34.00, 22.00),  # Mossel Bay
            (-34.00, 24.00),  # Garden Route coast
            (-33.80, 26.00),  # Port Elizabeth
            (-33.00, 27.50),  # East London coast
            (-31.50, 29.50),  # Durban approach
            (-30.00, 31.50),  # KZN coast (generous)
            (-34.00, 32.00),  # Offshore (SE, shipping lane)
            (-37.00, 28.00),  # Deep offshore (S)
            (-37.00, 18.00),  # Deep offshore (SW)
            (-34.00, 14.00),  # Open ocean (W)
            (-31.50, 15.00),  # W coast approach
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
            # Yellow Sea + Bohai Sea — generous coastline following.
            (40.50, 120.50),  # Bohai Strait west (Penglai)
            (39.00, 118.50),  # Tianjin coast
            (38.50, 117.50),  # Bohai Bay (inner)
            (38.00, 118.00),  # Shandong N coast (Dongying)
            (37.50, 119.50),  # Shandong NE coast (Weifang)
            (37.30, 122.50),  # Shandong E tip (Weihai)
            (36.00, 120.50),  # Qingdao coast
            (35.00, 119.50),  # Jiangsu N coast (Lianyungang)
            (34.00, 120.00),  # Jiangsu coast
            (33.50, 121.00),  # Yangtze approach
            (34.00, 125.50),  # Central Yellow Sea
            (34.00, 126.50),  # Jeju SW approach
            (35.00, 126.50),  # SW Korean coast
            (36.00, 126.50),  # W Korean coast (Gunsan)
            (37.00, 126.00),  # W Korean coast (Incheon)
            (38.00, 125.00),  # Korean Bay
            (39.00, 124.50),  # N Korean coast (Sinuiju)
            (40.00, 124.50),  # Yalu River mouth
            (40.30, 122.50),  # Liaodong Peninsula tip
            (40.00, 121.50),  # Dalian coast
            (40.50, 121.50),  # Bohai Strait east
        ]),
        "zoom": 10,
        "name": "Yellow Sea",
    },

    # ── Zoom 9: Open ocean shipping lanes ────────────────────────────────
    "GOM": {
        "polygon": _parse_polygon("GULF_OF_MEXICO_POLYGON", [
            # Gulf of Mexico — generous coastal tracing + Cuba N coast.
            (30.30, -88.50),   # Mississippi coast (Gulfport)
            (30.20, -86.50),   # Alabama coast (Pensacola)
            (29.90, -85.00),   # Florida panhandle
            (29.00, -83.50),   # W Florida coast (Cedar Key)
            (27.00, -83.00),   # W Florida coast (Tampa)
            (26.00, -82.00),   # SW Florida (Fort Myers)
            (25.00, -81.50),   # Florida Keys (west)
            (24.50, -83.00),   # Dry Tortugas / deep Florida Strait
            (23.00, -82.00),   # Cuba N coast (Havana)
            (22.00, -84.00),   # Cuba W coast
            (21.50, -87.00),   # Yucatan Channel
            (20.50, -87.00),   # Yucatan NE coast (Cancun)
            (20.00, -90.50),   # Yucatan N coast (Merida)
            (19.00, -92.50),   # Campeche Bay
            (18.50, -94.50),   # Coatzacoalcos coast
            (19.50, -96.00),   # Veracruz coast
            (21.50, -97.50),   # Tampico coast
            (24.50, -97.50),   # S Texas coast (Brownsville)
            (26.50, -97.00),   # Texas coast (Corpus Christi)
            (28.50, -96.00),   # Texas coast (Matagorda)
            (29.30, -94.50),   # Texas coast (Galveston)
            (29.80, -93.50),   # Louisiana coast (Sabine)
            (29.50, -92.00),   # Louisiana coast (Vermilion Bay)
            (29.20, -90.00),   # Mississippi Delta
            (29.00, -89.00),   # SE Louisiana
            (30.00, -89.50),   # Mississippi coast (Biloxi)
        ]),
        "zoom": 9,
        "name": "Gulf of Mexico",
    },
    "CAR": {
        "polygon": _parse_polygon("CARIBBEAN_POLYGON", [
            # Caribbean Sea — generous coverage including all island arcs.
            (18.50, -88.00),   # Belize coast
            (17.50, -86.00),   # Honduras coast (Roatan)
            (16.00, -86.00),   # Honduras coast
            (15.00, -84.00),   # Mosquito Coast
            (13.00, -83.50),   # Nicaragua coast
            (12.00, -83.80),   # Costa Rica coast
            (10.00, -83.00),   # Costa Rica / Panama
            (9.50, -80.00),    # Panama (Caribbean side)
            (9.00, -77.00),    # Colombia (Gulf of Uraba)
            (10.00, -76.00),   # Colombia coast (Cartagena approach)
            (11.50, -72.00),   # Colombia coast (Santa Marta)
            (12.00, -70.00),   # Venezuela (Coro)
            (10.50, -67.00),   # Venezuela coast (Caracas)
            (10.50, -62.00),   # Venezuela coast (Barcelona)
            (10.00, -61.00),   # Trinidad approach
            (11.00, -60.00),   # Lesser Antilles south
            (14.00, -60.50),   # Windward Islands
            (16.00, -61.00),   # Guadeloupe
            (18.00, -62.00),   # Leeward Islands
            (19.00, -65.00),   # Virgin Islands
            (19.50, -68.00),   # Mona Passage
            (20.00, -73.00),   # Haiti / Cuba south
            (19.50, -77.50),   # Jamaica south
            (18.50, -78.00),   # Jamaica
            (19.50, -81.00),   # Cayman Islands
            (20.00, -85.00),   # Yucatan Channel south
        ]),
        "zoom": 9,
        "name": "Caribbean Sea",
    },
    "USE": {
        "polygon": _parse_polygon("US_EAST_COAST_POLYGON", [
            # US East Coast — generous offshore extent for shipping lanes.
            (43.50, -66.00),   # Maine coast (Eastport)
            (42.00, -70.00),   # Cape Cod
            (41.00, -71.00),   # Rhode Island / Long Island
            (40.50, -74.00),   # New York / NJ coast
            (39.00, -74.50),   # Delaware Bay
            (37.50, -76.00),   # Chesapeake Bay entrance
            (36.00, -75.50),   # Virginia Beach
            (35.00, -76.00),   # Cape Hatteras
            (34.00, -77.50),   # NC coast (Wilmington)
            (33.00, -79.00),   # SC coast (Myrtle Beach)
            (32.00, -80.50),   # SC coast (Charleston)
            (30.50, -81.00),   # Jacksonville coast
            (28.50, -80.50),   # Cape Canaveral
            (27.00, -80.00),   # SE Florida (West Palm Beach)
            (25.80, -80.00),   # Miami coast
            (25.00, -80.00),   # Florida Keys east
            (25.00, -65.00),   # Open ocean (SE, Bermuda corridor)
            (30.00, -65.00),   # Open ocean (E)
            (38.00, -65.00),   # Open ocean (NE)
            (43.50, -63.00),   # Nova Scotia approach
        ]),
        "zoom": 9,
        "name": "US East Coast",
    },
    "USW": {
        "polygon": _parse_polygon("US_WEST_COAST_POLYGON", [
            # US/Canada Pacific coast — generous coastline + offshore.
            (51.00, -131.00),  # Haida Gwaii / Queen Charlottes
            (50.50, -128.50),  # N Vancouver Island
            (49.00, -126.50),  # W Vancouver Island
            (48.50, -125.00),  # Cape Flattery (WA)
            (47.00, -124.50),  # Washington coast
            (46.00, -124.20),  # Columbia River mouth
            (44.50, -124.50),  # Oregon coast (Newport)
            (43.00, -124.50),  # Oregon coast (Coos Bay)
            (42.00, -124.50),  # OR/CA border (Crescent City)
            (40.50, -124.50),  # N California (Eureka)
            (38.50, -123.50),  # Point Reyes / SF approach
            (37.00, -122.50),  # San Francisco Bay
            (36.00, -122.00),  # Monterey Bay
            (34.50, -121.00),  # Point Conception
            (34.00, -119.50),  # Santa Barbara Channel
            (33.50, -118.50),  # Los Angeles / Long Beach
            (32.70, -117.50),  # San Diego coast
            (30.00, -116.50),  # Baja California (Ensenada)
            (30.00, -135.00),  # Open ocean (SW)
            (40.00, -135.00),  # Open ocean (W)
            (50.00, -135.00),  # Open ocean (NW)
        ]),
        "zoom": 9,
        "name": "US/Canada West Coast",
    },
    "BS": {
        "polygon": _parse_polygon("BALTIC_SEA_POLYGON", [
            # Baltic Sea — single clockwise loop. Start at Skagen, go east
            # along south coast, up east coast, across top of Bothnia,
            # down Swedish coast, back to Skagen. NO back-tracking.
            # ── South coast (W → E) ──
            (57.80, 10.50),   # Skagen tip (entrance)
            (56.50, 10.50),   # Kattegat (Aarhus)
            (55.80, 11.00),   # Great Belt (S Denmark)
            (55.00, 12.50),   # Gedser / Falster
            (54.30, 12.50),   # German coast (Rostock)
            (54.00, 13.50),   # Rugen Island
            (54.40, 14.80),   # Oder estuary (Swinoujscie)
            (54.50, 16.50),   # Polish coast (Kolobrzeg)
            (54.60, 18.80),   # Gdansk Bay (Hel Peninsula)
            (54.80, 20.00),   # Kaliningrad coast
            # ── East coast (S → N) ──
            (55.30, 21.00),   # Lithuanian coast (Klaipeda)
            (56.00, 21.20),   # Latvian coast (Liepaja)
            (57.10, 22.00),   # Latvian coast (Ventspils)
            (57.80, 24.50),   # Gulf of Riga
            (58.50, 24.00),   # Estonian coast (Parnu)
            (59.00, 25.50),   # Estonian coast (Tallinn)
            (59.70, 28.50),   # Gulf of Finland (Narva Bay)
            (60.00, 30.00),   # St. Petersburg approach
            # ── Finnish coast (SE → N) ──
            (60.30, 28.00),   # Finnish coast (Kotka)
            (60.30, 25.00),   # Helsinki
            (60.00, 23.00),   # Hanko Peninsula
            (60.70, 22.00),   # Turku archipelago
            (61.00, 21.50),   # Finnish coast (Rauma)
            (62.00, 21.50),   # Finnish coast (Pori)
            (63.50, 22.50),   # Finnish coast (Vaasa)
            (65.20, 25.00),   # Finnish coast (Oulu)
            # ── Top of Bothnia (E → W) ──
            (65.80, 24.50),   # Tornio / northernmost
            (65.20, 23.50),   # Lulea
            # ── Swedish coast (N → S) ──
            (63.80, 20.00),   # High Coast (Umea)
            (63.00, 18.50),   # Harnosand
            (61.50, 17.50),   # Sundsvall
            (60.70, 17.50),   # Gavle
            (59.50, 18.50),   # Stockholm archipelago
            (58.50, 17.00),   # Gotland west
            (57.50, 16.50),   # Oland / Kalmar
            (56.50, 16.00),   # Karlskrona
            (55.50, 14.50),   # Bornholm south
            (55.30, 13.50),   # Bornholm approach
            (56.20, 12.80),   # Oresund (Malmo/Copenhagen)
            (57.70, 11.80),   # Kattegat (Gothenburg)
        ]),
        "zoom": 9,
        "name": "Baltic Sea",
    },
    "GG": {
        "polygon": _parse_polygon("GULF_OF_GUINEA_POLYGON", [
            # Gulf of Guinea — generous coastal tracing W Africa.
            (7.50, -12.00),    # Sierra Leone coast (Freetown)
            (6.50, -10.50),    # Liberia coast
            (5.50, -8.50),     # Liberia coast (Monrovia)
            (4.50, -7.50),     # Cote d'Ivoire (San Pedro)
            (5.20, -4.00),     # Cote d'Ivoire (Abidjan)
            (5.00, -1.50),     # Ghana coast (Takoradi)
            (5.50, 0.00),      # Ghana coast (Accra)
            (6.20, 1.50),      # Togo / Benin coast (Lome)
            (6.40, 2.50),      # Benin coast (Cotonou)
            (6.50, 3.50),      # Lagos approach
            (5.50, 5.50),      # Niger Delta (west)
            (4.50, 6.50),      # Niger Delta (center)
            (4.00, 8.00),      # Niger Delta (east)
            (4.00, 9.00),      # Cameroon coast (Douala)
            (3.50, 9.50),      # Cameroon coast (Kribi)
            (2.00, 9.80),      # Equatorial Guinea (Bata)
            (0.50, 9.50),      # Gabon coast (Libreville)
            (-0.50, 9.00),     # Gabon coast (Port Gentil)
            (-3.00, 10.00),    # Congo coast
            (-4.50, 11.50),    # Congo coast (Pointe Noire)
            (-6.00, 12.00),    # Angola (Cabinda coast)
            (-6.50, 8.00),     # Open ocean (S)
            (-5.00, 2.00),     # Open ocean (SW)
            (-2.00, -3.00),    # Open ocean (W)
            (2.00, -8.00),     # Open ocean (NW)
            (5.00, -12.00),    # Open ocean (NW approach)
        ]),
        "zoom": 9,
        "name": "Gulf of Guinea",
    },
    "SAW": {
        "polygon": _parse_polygon("S_ATLANTIC_W_POLYGON", [
            # S Atlantic West — clockwise: coast (N→S), ocean (S→N).
            # ── Brazil coast (N → S) ──
            (-5.00, -35.00),   # NE Brazil (Natal)
            (-8.00, -34.50),   # Recife coast
            (-13.00, -38.50),  # Salvador coast
            (-23.00, -42.00),  # Rio de Janeiro coast
            (-28.00, -48.50),  # Florianopolis
            (-33.00, -52.00),  # Uruguay approach
            # ── Ocean (S → E → N) ──
            (-35.00, -52.00),  # Open ocean (SW)
            (-35.00, -40.00),  # Open ocean (S)
            (-35.00, -30.00),  # Open ocean (SE)
            (-20.00, -25.00),  # Mid-Atlantic
            (-5.00, -30.00),   # Equatorial Atlantic
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
            # Philippine Sea — generous coverage E of Philippines.
            (20.50, 122.00),  # NE Luzon coast (Aparri)
            (18.50, 122.50),  # Luzon east coast (Tuguegarao)
            (16.50, 122.00),  # Luzon coast (Baler)
            (14.50, 122.00),  # SE Luzon (Naga)
            (13.00, 124.00),  # Catanduanes / Samar
            (11.50, 125.00),  # Leyte coast
            (10.00, 126.00),  # Samar / Leyte Gulf
            (8.50, 126.50),   # E Mindanao (Davao approach)
            (7.00, 126.50),   # SE Mindanao
            (5.50, 127.00),   # SW approach (Celebes Sea)
            (5.00, 132.00),   # Open ocean (SE)
            (8.00, 135.00),   # Palau approach
            (12.00, 136.00),  # Open ocean (E)
            (18.00, 135.00),  # Mariana Islands area
            (22.00, 132.00),  # Open ocean (NE)
            (22.00, 126.00),  # Taiwan E coast approach
        ]),
        "zoom": 9,
        "name": "Philippine Sea",
    },
    "NOR": {
        "polygon": _parse_polygon("NORWEGIAN_SEA_POLYGON", [
            # Norwegian Sea — generous coverage, Faroe-Iceland gap to Barents.
            (62.00, -2.00),   # Shetland N approach
            (62.00, -7.00),   # Faroe Islands approach
            (63.50, -10.00),  # Faroe-Iceland gap
            (65.00, -14.00),  # Iceland E coast approach
            (67.00, -14.00),  # Iceland NE shelf
            (69.00, -10.00),  # Jan Mayen area
            (71.50, -5.00),   # Open ocean (N)
            (73.00, 5.00),    # Bear Island area
            (72.00, 16.00),   # Hammerfest offshore
            (71.00, 18.00),   # N Norway coast (generous)
            (70.00, 17.00),   # Lofoten Islands (N)
            (68.50, 15.50),   # Lofoten (S)
            (67.50, 14.50),   # Bodo coast
            (66.00, 12.50),   # Helgeland coast
            (64.50, 11.00),   # Trondheim approach
            (63.50, 8.00),    # More coast
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
            # Western Mediterranean basin.  North coast: Spain → France →
            # Italy.  South coast: Tunisia → Algeria → Morocco.
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
            (37.50, 15.50),   # Sicily (NE tip)
            (36.70, 14.50),   # Sicily (south coast)
            (36.00, 11.50),   # Cap Bon / Tunisian NE coast
            (36.80, 8.00),    # Annaba / NE Algeria coast
            (36.70, 4.00),    # Algiers coast
            (35.80, 0.00),    # Oran coast
            (35.20, -2.50),   # Morocco Rif coast
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
            (35.00, 23.50),   # S of Crete (west end)
            (35.00, 26.50),   # S of Crete (east end)
            (36.50, 27.50),   # Dodecanese / E Aegean
            (37.00, 30.00),   # Turkish SW coast (Antalya)
            (36.50, 35.00),   # Turkish S coast (Mersin)
            (35.00, 35.50),   # Cyprus / Syria coast
            (33.00, 35.00),   # Lebanese coast
            (31.50, 34.00),   # Israeli coast
            (31.00, 32.50),   # Egyptian coast (Port Said)
            (31.00, 28.00),   # Egyptian coast (Alexandria)
            (32.00, 23.00),   # Libyan coast (Benghazi)
            (32.50, 18.00),   # Gulf of Sidra (Libya)
            (33.50, 15.00),   # Libyan coast / Sicilian channel
        ]),
        "zoom": 9,
        "name": "Mediterranean East",
    },
    "ARS": {
        "polygon": _parse_polygon("ARABIAN_SEA_POLYGON", [
            # Arabian Sea — generous coverage, Oman to India west coast.
            (25.50, 57.00),   # Oman N coast (Musandam)
            (23.50, 58.50),   # Oman coast (Muscat)
            (21.00, 59.50),   # Oman coast (Sur)
            (20.00, 60.00),   # Oman SE coast
            (17.00, 55.00),   # Oman S coast (Salalah)
            (15.00, 52.00),   # Yemen east coast
            (12.50, 51.50),   # Socotra approach
            (10.00, 55.00),   # Open ocean (S)
            (5.00, 60.00),    # Open ocean (Maldives NW)
            (5.00, 72.00),    # Maldives / Laccadive Sea
            (8.00, 77.00),    # Kerala coast (Trivandrum)
            (10.00, 76.00),   # Kerala coast (Kochi)
            (12.50, 74.80),   # Karnataka coast (Mangalore)
            (15.50, 73.50),   # Goa coast
            (17.00, 73.00),   # Maharashtra coast
            (19.00, 73.00),   # Mumbai approach
            (21.00, 70.00),   # Gujarat coast (Porbandar)
            (23.00, 68.50),   # Kutch coast
            (24.50, 67.00),   # Karachi approach
            (25.50, 63.00),   # Makran coast (Pakistan)
            (25.00, 61.00),   # Makran coast (Iran/Pak border)
        ]),
        "zoom": 9,
        "name": "Arabian Sea",
    },
    "BOB": {
        "polygon": _parse_polygon("BAY_OF_BENGAL_POLYGON", [
            # Bay of Bengal — generous coverage including Andaman Sea.
            (22.00, 88.50),   # Bangladesh coast (Ganges Delta)
            (21.50, 90.00),   # Bangladesh coast (Chittagong)
            (20.50, 92.50),   # Myanmar coast (Sittwe)
            (18.00, 94.00),   # Myanmar coast (Irrawaddy Delta)
            (16.00, 94.50),   # Myanmar coast (Rangoon)
            (14.00, 97.00),   # Myanmar coast / Andaman Sea
            (12.00, 98.00),   # Andaman Sea (Myeik)
            (10.00, 98.00),   # Andaman Sea (Mergui)
            (8.00, 98.50),    # Andaman Sea (S Myanmar)
            (7.00, 95.00),    # Nicobar Islands (generous)
            (5.00, 93.00),    # Open ocean (S Andaman Sea)
            (4.00, 85.00),    # Open ocean (S)
            (6.00, 80.00),    # Sri Lanka S tip approach
            (7.50, 79.50),    # Sri Lanka W coast
            (9.50, 80.00),    # Sri Lanka N tip (Jaffna)
            (10.50, 80.00),   # SE India (Nagapattinam)
            (13.00, 80.50),   # Chennai coast
            (15.00, 80.00),   # Andhra coast
            (16.50, 82.50),   # Andhra coast (Visakhapatnam)
            (18.50, 84.50),   # Odisha coast
            (20.00, 86.50),   # Odisha coast (Puri)
            (21.50, 87.50),   # West Bengal coast (Digha)
        ]),
        "zoom": 9,
        "name": "Bay of Bengal",
    },
    "WP": {
        "polygon": _parse_polygon("W_PACIFIC_POLYGON", [
            # Western Pacific — clockwise loop: Japan coast (N→S),
            # then offshore (S→N) back to start.
            # ── Japan coast (N → S) ──
            (43.50, 146.00),  # Hokkaido E coast (Kushiro)
            (42.00, 145.00),  # Hokkaido SE coast
            (41.50, 141.00),  # Hokkaido S (Hakodate)
            (40.00, 140.00),  # N Honshu (Akita)
            (38.00, 139.50),  # Niigata coast
            (35.50, 141.00),  # E Japan (Choshi)
            (35.00, 140.00),  # Tokyo Bay approach
            (34.50, 138.00),  # Shizuoka coast
            (33.50, 136.00),  # Kii Peninsula
            (33.00, 133.00),  # Shikoku south coast
            (31.50, 131.00),  # Kyushu SE coast
            (30.50, 131.00),  # Kyushu S coast (Kagoshima)
            (28.00, 129.50),  # Amami Islands
            (26.00, 128.00),  # Okinawa
            (24.50, 125.00),  # Yaeyama Islands
            # ── Offshore (S → N) ──
            (24.50, 130.00),  # Open ocean (S)
            (26.00, 135.00),  # Open ocean (S-central)
            (28.00, 142.00),  # Bonin Islands
            (32.00, 145.00),  # Open Pacific
            (38.00, 146.00),  # Open Pacific (NE)
            (43.50, 148.00),  # Hokkaido far offshore
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
            # Clockwise: coast south, then offshore west, close north.
            (-23.00, -45.00),   # Rio coast approach
            (-23.80, -44.30),   # Ilha Grande Bay
            (-24.50, -45.00),   # Santos coast
            (-25.50, -46.00),   # Paranagua / offshore S
            (-25.50, -47.00),   # Offshore SW
            (-24.00, -47.00),   # Offshore W
            (-23.00, -46.00),   # Santos port approach
        ]),
        "zoom": 11,
        "name": "Santos / SE Brazil Coast",
    },

    # ── Zoom 10: Regional corridors (expansion) ───────────────────────────
    "CHR": {
        "polygon": _parse_polygon("CAPE_HORN_POLYGON", [
            # Cape Horn / Drake Passage — generous water coverage.
            (-52.50, -75.00),  # W Patagonia (Gulf of Penas)
            (-53.00, -73.00),  # Strait of Magellan west
            (-53.50, -71.00),  # Strait of Magellan
            (-54.00, -69.00),  # Tierra del Fuego S coast
            (-55.00, -67.00),  # Beagle Channel
            (-56.00, -67.00),  # Cape Horn (generous)
            (-56.00, -63.00),  # Drake Passage (E)
            (-58.50, -62.00),  # Drake Passage (SE)
            (-60.00, -65.00),  # Deep Drake Passage
            (-60.00, -72.00),  # Drake Passage (SW)
            (-58.00, -75.00),  # Open Pacific (S)
            (-55.00, -76.00),  # Open Pacific approach
            (-52.50, -76.00),  # W Patagonia offshore
        ]),
        "zoom": 10,
        "name": "Cape Horn / Drake Passage",
    },
    "BFS": {
        "polygon": _parse_polygon("BANDA_FLORES_POLYGON", [
            # Banda Sea / Flores Sea — Indonesian inner seas.
            (-5.00, 117.50),  # E Java Sea / Makassar S approach
            (-6.50, 118.00),  # Flores west coast
            (-7.50, 118.50),  # Sumbawa N coast approach
            (-8.00, 119.00),  # Sumbawa N coast
            (-8.50, 120.00),  # Sumba approach
            (-8.50, 122.00),  # Flores S coast
            (-8.00, 123.50),  # Flores E tip
            (-8.50, 125.00),  # Timor N coast approach
            (-8.00, 127.50),  # Wetar Strait
            (-6.50, 128.50),  # Banda Sea (E)
            (-4.50, 128.00),  # Seram S coast
            (-4.00, 126.00),  # Buru Island approach
            (-3.50, 124.00),  # Banda Sea center
            (-4.00, 122.00),  # SE Sulawesi approach
            (-5.00, 120.00),  # Flores Sea center
            (-5.00, 118.50),  # Flores Sea (W)
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
    
    
    "NNC": {
        "polygon": _parse_polygon("NORW_CORRIDOR_POLYGON", [
            # N Sea–Norwegian Corridor — shipping lane from Shetland to Skagerrak.
            (62.00, -1.00),   # Shetland NE approach
            (61.00, 1.00),    # Norwegian sector
            (60.50, 5.00),    # Bergen offshore
            (59.00, 5.50),    # Stavanger offshore
            (58.00, 7.00),    # Southern Norway (Kristiansand)
            (57.80, 10.00),   # Skagerrak entrance
            (57.00, 9.00),    # Central North Sea E
            (56.00, 8.00),    # Jutland W coast
            (55.50, 6.00),    # Dogger Bank (E)
            (55.00, 3.00),    # Dogger Bank (W)
            (56.00, -1.00),   # Central North Sea W
            (58.00, -3.50),   # Orkney approach
            (59.50, -2.00),   # Shetland approach
            (61.00, -2.00),   # Shetland W
        ]),
        "zoom": 9,
        "name": "North Sea–Norwegian Corridor",
    },
    "WAO": {
        "polygon": _parse_polygon("W_AFRICA_OFFSHORE_POLYGON", [
            # West Africa Offshore — Angola/Namibia coast + offshore.
            (-4.50, 11.50),    # Congo-Brazzaville coast
            (-5.50, 12.00),    # Cabinda coast
            (-6.50, 12.00),    # Angola coast (Soyo)
            (-8.50, 13.20),    # Angola coast (Luanda)
            (-10.00, 13.50),   # Angola coast (Lobito)
            (-12.50, 13.50),   # Angola coast (Benguela)
            (-15.00, 12.00),   # Angola/Namibia border
            (-17.00, 11.80),   # Namibia (Skeleton Coast)
            (-19.00, 12.00),   # Namibia (Walvis Bay approach)
            (-19.00, 8.00),    # Offshore (SW)
            (-15.00, 5.00),    # Offshore (W)
            (-8.00, 4.00),     # Offshore (NW)
            (-4.50, 8.00),     # Open ocean (N approach)
        ]),
        "zoom": 9,
        "name": "West Africa Offshore (Angola)",
    },
    "EAF": {
        "polygon": _parse_polygon("E_AFRICA_COAST_POLYGON", [
            # East Africa Coast — Kenya/Tanzania/N Mozambique.
            (2.00, 41.00),     # S Somalia coast
            (0.00, 41.50),     # Kenya coast (Lamu)
            (-2.00, 40.50),    # Kenya coast (Malindi)
            (-4.00, 39.70),    # Mombasa coast
            (-5.00, 39.50),    # Tanzania (Tanga)
            (-6.50, 39.50),    # Dar es Salaam coast
            (-8.00, 39.50),    # Tanzania (Kilwa)
            (-10.00, 40.00),   # S Tanzania (Mtwara)
            (-11.00, 40.50),   # N Mozambique (Pemba)
            (-13.00, 41.00),   # Comoros W approach
            (-12.50, 44.50),   # Comoros E approach
            (-11.00, 47.50),   # Open ocean (E, Seychelles approach)
            (-5.00, 48.00),    # Open ocean (NE)
            (-1.00, 47.00),    # Open ocean (N)
            (2.00, 45.00),     # Somali coast approach
        ]),
        "zoom": 9,
        "name": "East Africa Coast",
    },
    "NEP": {
        "polygon": _parse_polygon("NE_PACIFIC_POLYGON", [
            # NE Pacific — Alaska to BC coast + generous offshore.
            (61.00, -146.00),  # Prince William Sound
            (60.00, -148.00),  # Gulf of Alaska
            (59.00, -152.00),  # Kodiak Island approach
            (57.00, -155.00),  # Alaska Peninsula
            (55.00, -160.00),  # Aleutian chain E approach
            (52.00, -150.00),  # Open ocean (SW)
            (48.50, -140.00),  # Open ocean (S)
            (48.50, -128.00),  # Vancouver Island W approach
            (50.50, -128.50),  # N Vancouver Island
            (52.00, -131.00),  # Haida Gwaii
            (54.50, -133.50),  # Haida Gwaii N
            (56.00, -135.00),  # SE Alaska (Sitka)
            (57.50, -136.00),  # Glacier Bay approach
            (59.00, -140.00),  # Yakutat coast
            (60.50, -144.00),  # Cordova coast
        ]),
        "zoom": 9,
        "name": "NE Pacific (Alaska–BC)",
    },
}
