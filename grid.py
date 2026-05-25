"""Mercator projection utilities and tile grid computation.

Converts lat/lon ↔ pixel coordinates in the Web Mercator projection
(EPSG:3857) used by Leaflet / MarineTraffic.  Computes tile grids for
viewport-based map capture and can auto-generate region grids covering
large ocean bounding boxes.
"""

import math


def lat_to_pixel_y(lat, zoom):
    """Convert latitude to pixel Y in Web Mercator (Y increases downward)."""
    lat_rad = math.radians(lat)
    merc_y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0
    return merc_y * 256 * (2 ** zoom)


def pixel_y_to_lat(pixel_y, zoom):
    """Convert pixel Y back to latitude."""
    merc_y = pixel_y / (256 * (2 ** zoom))
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * merc_y))))


def lon_to_pixel_x(lon, zoom):
    """Convert longitude to pixel X in Web Mercator."""
    return (lon + 180.0) / 360.0 * 256 * (2 ** zoom)


def pixel_x_to_lon(pixel_x, zoom):
    """Convert pixel X back to longitude."""
    return pixel_x / (256 * (2 ** zoom)) * 360.0 - 180.0


def get_tile_bounds(center_lat, center_lon, zoom, viewport_width, viewport_height):
    """Return the four corner (lat, lon) of a viewport-sized tile.

    Mirrors how the scraper positions a tile: take the center in Web Mercator
    pixel space, expand ±viewport/2, then project the corners back to lat/lon.
    Order is NW, NE, SE, SW (suitable for closing into a polygon ring).
    """
    cx = lon_to_pixel_x(center_lon, zoom)
    cy = lat_to_pixel_y(center_lat, zoom)
    half_w = viewport_width / 2
    half_h = viewport_height / 2

    lon_w = pixel_x_to_lon(cx - half_w, zoom)
    lon_e = pixel_x_to_lon(cx + half_w, zoom)
    lat_n = pixel_y_to_lat(cy - half_h, zoom)
    lat_s = pixel_y_to_lat(cy + half_h, zoom)

    return [(lat_n, lon_w), (lat_n, lon_e), (lat_s, lon_e), (lat_s, lon_w)]


def _point_in_polygon(lat, lon, polygon):
    """Ray-casting point-in-polygon test.

    Casts a horizontal ray eastward from (lat, lon) and counts how many
    polygon edges it crosses.  Odd crossings = inside, even = outside.
    Works for any simple polygon (convex or concave).

    Args:
        lat, lon: test point coordinates
        polygon: list of (lat, lon) tuples defining the polygon boundary
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def get_tile_centers(polygon, zoom, viewport_width, viewport_height):
    """
    Compute non-overlapping tile centers covering a polygon's bounding box,
    then filter out tiles whose center falls outside the polygon.

    For simple rectangular (4-vertex) polygons every tile is kept.  For
    concave coastline-following polygons, tiles over land are skipped.

    Args:
        polygon: list of (lat, lon) tuples defining the region
        zoom: map zoom level
        viewport_width: browser viewport width in pixels
        viewport_height: browser viewport height in pixels

    Returns:
        (tiles, grid_info) where:
        - tiles: list of (row, col, center_lat, center_lon)
        - grid_info: dict with metadata for stitching/masking
    """
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    total_pixels = 256 * (2 ** zoom)
    lon_span = viewport_width * 360.0 / total_pixels

    n_cols = max(1, math.ceil((lon_max - lon_min) / lon_span))

    y_top = lat_to_pixel_y(lat_max, zoom)
    y_bot = lat_to_pixel_y(lat_min, zoom)
    n_rows = max(1, math.ceil((y_bot - y_top) / viewport_height))

    bbox_tiles = []
    for row in range(n_rows):
        # Snake/boustrophedon ordering: even rows L→R, odd rows R→L
        # This minimizes pan distance between consecutive tiles
        cols = range(n_cols) if row % 2 == 0 else range(n_cols - 1, -1, -1)
        for col in cols:
            center_lon = lon_min + lon_span / 2 + col * lon_span
            center_y = y_top + viewport_height / 2 + row * viewport_height
            center_lat = pixel_y_to_lat(center_y, zoom)
            bbox_tiles.append((row, col, center_lat, center_lon))

    # Filter tiles whose center falls outside the polygon.
    # For simple rectangles (≤4 vertices) this is a no-op.
    # For small grids (≤4 tiles) skip filtering too — the viewport already
    # covers the entire bounding box, so removing tiles is counterproductive
    # (tile centers can overshoot narrow concave polygons).
    is_rect = len(polygon) <= 4
    small_grid = len(bbox_tiles) <= 4
    if is_rect or small_grid:
        tiles = bbox_tiles
    else:
        tiles = [t for t in bbox_tiles
                 if _point_in_polygon(t[2], t[3], polygon)]
        # Fallback: when all tiles are filtered out, place one tile at
        # the polygon centroid so the region isn't skipped entirely.
        if not tiles and bbox_tiles:
            clat = sum(p[0] for p in polygon) / len(polygon)
            clon = sum(p[1] for p in polygon) / len(polygon)
            tiles = [(0, 0, clat, clon)]

    grid_info = {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "y_top": y_top,
        "lon_min": lon_min,
        "lon_span": lon_span,
        "total_bbox_tiles": len(bbox_tiles),
    }
    return tiles, grid_info


def polygon_to_pixel_coords(polygon, grid_info, zoom):
    """
    Convert polygon vertices (lat, lon) to pixel coordinates
    in composite image space.

    Returns list of (x, y) tuples.
    """
    y_top = grid_info["y_top"]
    lon_min = grid_info["lon_min"]
    px_per_deg_lon = 256 * (2 ** zoom) / 360.0

    coords = []
    for lat, lon in polygon:
        x = (lon - lon_min) * px_per_deg_lon
        y = lat_to_pixel_y(lat, zoom) - y_top
        coords.append((x, y))

    # Sort by angle from centroid to guarantee convex, non-self-intersecting order
    cx = sum(x for x, y in coords) / len(coords)
    cy = sum(y for x, y in coords) / len(coords)
    coords.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

    return coords


def generate_ocean_grid(lat_min, lat_max, lon_min, lon_max,
                        zoom, viewport_width, viewport_height):
    """Auto-generate rectangular region polygons tiling a bounding box.

    Instead of hand-defining polygons for huge ocean areas, this function
    subdivides a bounding box into viewport-sized regions.  Each returned
    region is exactly one tile at the given zoom/viewport — meaning one
    screenshot covers it entirely with no panning needed.

    Args:
        lat_min, lat_max: latitude range (degrees, south negative)
        lon_min, lon_max: longitude range (degrees, west negative)
        zoom: map zoom level for these regions
        viewport_width, viewport_height: browser viewport in pixels

    Returns:
        list of dicts: [{"name": "GRID_r0c0", "polygon": [(lat,lon), ...], "zoom": zoom}, ...]
    """
    total_pixels = 256 * (2 ** zoom)
    lon_span = viewport_width * 360.0 / total_pixels

    y_top = lat_to_pixel_y(lat_max, zoom)
    y_bot = lat_to_pixel_y(lat_min, zoom)

    n_cols = max(1, math.ceil((lon_max - lon_min) / lon_span))
    n_rows = max(1, math.ceil((y_bot - y_top) / viewport_height))

    regions = []
    for row in range(n_rows):
        for col in range(n_cols):
            tile_lon_min = lon_min + col * lon_span
            tile_lon_max = min(tile_lon_min + lon_span, lon_max)

            tile_y_top = y_top + row * viewport_height
            tile_y_bot = min(tile_y_top + viewport_height, y_bot)

            tile_lat_max = pixel_y_to_lat(tile_y_top, zoom)
            tile_lat_min = pixel_y_to_lat(tile_y_bot, zoom)

            polygon = [
                (tile_lat_max, tile_lon_min),
                (tile_lat_max, tile_lon_max),
                (tile_lat_min, tile_lon_max),
                (tile_lat_min, tile_lon_min),
            ]
            regions.append({
                "name": f"GRID_r{row}c{col}",
                "polygon": polygon,
                "zoom": zoom,
            })

    return regions


def tile_area_km2(zoom, viewport_width, viewport_height, latitude=0.0):
    """Estimate the area (km²) covered by one viewport tile at a given latitude.

    Useful for capacity planning — how many tiles are needed to cover X km².
    Area varies with latitude because Mercator stretches toward poles.
    """
    total_px = 256 * (2 ** zoom)
    lon_deg = viewport_width * 360.0 / total_px
    km_per_deg_lon = 111.32 * math.cos(math.radians(latitude))

    y_center = lat_to_pixel_y(latitude, zoom)
    lat_top = pixel_y_to_lat(y_center - viewport_height / 2, zoom)
    lat_bot = pixel_y_to_lat(y_center + viewport_height / 2, zoom)
    lat_deg = lat_top - lat_bot
    km_per_deg_lat = 110.574

    return lon_deg * km_per_deg_lon * lat_deg * km_per_deg_lat
