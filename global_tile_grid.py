"""Global Web-Mercator capture grid for MarineScraper.

The grid is aligned to Web-Mercator pixel coordinates, not to analytics
regions. Regions can seed higher zoom coverage, but every captured marker is
owned by exactly one deterministic tile.
"""

import math
import os
from collections import defaultdict

from grid import (
    lat_to_pixel_y,
    lon_to_pixel_x,
    pixel_x_to_lon,
    pixel_y_to_lat,
    _point_in_polygon,
)
from regions import load_bbox_regions


DEFAULT_GLOBAL_GRID_BBOX = (-60.0, -180.0, 75.0, 180.0)
DEFAULT_GLOBAL_GRID_DEFAULT_ZOOM = 9
DEFAULT_TILE_ACCEPT_BUFFER_PX = 8


def parse_global_bbox(raw=None):
    """Return (min_lat, min_lon, max_lat, max_lon)."""
    value = raw if raw is not None else os.getenv("GLOBAL_GRID_BBOX")
    if not value:
        return DEFAULT_GLOBAL_GRID_BBOX
    parts = [float(p.strip()) for p in str(value).split(",")]
    if len(parts) != 4:
        raise ValueError("GLOBAL_GRID_BBOX must be min_lat,min_lon,max_lat,max_lon")
    min_lat, min_lon, max_lat, max_lon = parts
    if min_lat > max_lat:
        min_lat, max_lat = max_lat, min_lat
    if min_lon > max_lon:
        min_lon, max_lon = max_lon, min_lon
    return min_lat, min_lon, max_lat, max_lon


def normalize_lon(lon):
    """Normalize longitude to [-180, 180]."""
    value = ((float(lon) + 180.0) % 360.0) - 180.0
    if value == -180.0 and float(lon) > 0:
        return 180.0
    return value


def global_tile_id(zoom, row, col):
    return f"g_z{int(zoom)}_r{int(row)}_c{int(col)}"


def _bbox_to_pixels(bbox, zoom):
    min_lat, min_lon, max_lat, max_lon = bbox
    return {
        "west_x": lon_to_pixel_x(min_lon, zoom),
        "east_x": lon_to_pixel_x(max_lon, zoom),
        "north_y": lat_to_pixel_y(max_lat, zoom),
        "south_y": lat_to_pixel_y(min_lat, zoom),
    }


def _geo_bounds_from_px(px_bounds, zoom):
    west = float(px_bounds["west_x"])
    east = float(px_bounds["east_x"])
    north = float(px_bounds["north_y"])
    south = float(px_bounds["south_y"])
    return {
        "min_lat": pixel_y_to_lat(south, zoom),
        "min_lon": pixel_x_to_lon(west, zoom),
        "max_lat": pixel_y_to_lat(north, zoom),
        "max_lon": pixel_x_to_lon(east, zoom),
    }


def _bounds_intersect(a, b):
    return not (
        a["east_x"] <= b["west_x"]
        or a["west_x"] >= b["east_x"]
        or a["south_y"] <= b["north_y"]
        or a["north_y"] >= b["south_y"]
    )


def _merge_bbox(a, b):
    return (
        max(a[0], b[0]),
        max(a[1], b[1]),
        min(a[2], b[2]),
        min(a[3], b[3]),
    )


def _bbox_valid(bbox):
    return bbox[0] < bbox[2] and bbox[1] < bbox[3]


def _tile_from_row_col(row, col, zoom, viewport_width, viewport_height,
                       global_bbox, source, priority=0, seed_regions=None,
                       schedule_minutes=None, enabled=True):
    capture_px = {
        "west_x": float(col * viewport_width),
        "east_x": float((col + 1) * viewport_width),
        "north_y": float(row * viewport_height),
        "south_y": float((row + 1) * viewport_height),
    }
    global_px = _bbox_to_pixels(global_bbox, zoom)
    owner_px = {
        "west_x": max(capture_px["west_x"], global_px["west_x"]),
        "east_x": min(capture_px["east_x"], global_px["east_x"]),
        "north_y": max(capture_px["north_y"], global_px["north_y"]),
        "south_y": min(capture_px["south_y"], global_px["south_y"]),
    }
    if not _bounds_intersect(capture_px, global_px):
        return None
    center_x = (capture_px["west_x"] + capture_px["east_x"]) / 2.0
    center_y = (capture_px["north_y"] + capture_px["south_y"]) / 2.0
    tile = {
        "tile_id": global_tile_id(zoom, row, col),
        "zoom": int(zoom),
        "row": int(row),
        "col": int(col),
        "enabled": bool(enabled),
        "schedule_minutes": int(schedule_minutes or os.getenv(
            "SCRAPE_INTERVAL_MINUTES", "60"
        )),
        "priority": int(priority),
        "source": source,
        "seed_regions": list(seed_regions or []),
        "center_lat": pixel_y_to_lat(center_y, zoom),
        "center_lon": pixel_x_to_lon(center_x, zoom),
        "capture_bounds_px": capture_px,
        "owner_bounds_px": owner_px,
        "capture_bounds": _geo_bounds_from_px(capture_px, zoom),
        "tile_bounds": _geo_bounds_from_px(owner_px, zoom),
    }
    return tile


def iter_global_tiles_for_bbox(bbox, zoom, viewport_width, viewport_height,
                               global_bbox=None, source="global_default",
                               priority=0, seed_regions=None,
                               schedule_minutes=None):
    """Yield deterministic, global-pixel-aligned tiles intersecting bbox."""
    global_bbox = global_bbox or bbox
    bbox = _merge_bbox(bbox, global_bbox)
    if not _bbox_valid(bbox):
        return
    px = _bbox_to_pixels(bbox, zoom)
    col_start = int(math.floor(px["west_x"] / viewport_width))
    col_end = int(math.ceil(px["east_x"] / viewport_width)) - 1
    row_start = int(math.floor(px["north_y"] / viewport_height))
    row_end = int(math.ceil(px["south_y"] / viewport_height)) - 1
    for row in range(row_start, row_end + 1):
        cols = range(col_start, col_end + 1)
        if row % 2:
            cols = range(col_end, col_start - 1, -1)
        for col in cols:
            tile = _tile_from_row_col(
                row,
                col,
                zoom,
                viewport_width,
                viewport_height,
                global_bbox,
                source,
                priority=priority,
                seed_regions=seed_regions,
                schedule_minutes=schedule_minutes,
            )
            if tile:
                yield tile


def build_global_tile_manifest(
    viewport_width,
    viewport_height,
    global_bbox=None,
    default_zoom=None,
    schedule_minutes=None,
    seed_regions=True,
):
    """Build the v1 global manifest with region-seeded high-zoom tiles."""
    global_bbox = global_bbox or parse_global_bbox()
    default_zoom = int(default_zoom or os.getenv(
        "GLOBAL_GRID_DEFAULT_ZOOM", DEFAULT_GLOBAL_GRID_DEFAULT_ZOOM
    ))
    tiles = {}

    for tile in iter_global_tiles_for_bbox(
        global_bbox,
        default_zoom,
        viewport_width,
        viewport_height,
        global_bbox=global_bbox,
        source="global_default",
        priority=0,
        schedule_minutes=schedule_minutes,
    ):
        tiles[tile["tile_id"]] = tile

    if seed_regions:
        for code, cfg in load_bbox_regions(use_bbox_tiling=True).items():
            zoom = int(cfg.get("zoom", default_zoom))
            if zoom <= default_zoom:
                continue
            bbox = cfg.get("bbox")
            if not bbox:
                continue
            region_bbox = (
                float(bbox["min_lat"]),
                float(bbox["min_lon"]),
                float(bbox["max_lat"]),
                float(bbox["max_lon"]),
            )
            for tile in iter_global_tiles_for_bbox(
                region_bbox,
                zoom,
                viewport_width,
                viewport_height,
                global_bbox=global_bbox,
                source="region_seed",
                priority=zoom,
                seed_regions=[code],
                schedule_minutes=schedule_minutes,
            ):
                existing = tiles.get(tile["tile_id"])
                if existing:
                    seeds = set(existing.get("seed_regions", []))
                    seeds.add(code)
                    existing["seed_regions"] = sorted(seeds)
                    existing["priority"] = max(existing.get("priority", 0), zoom)
                    existing["source"] = "region_seed"
                else:
                    tiles[tile["tile_id"]] = tile

    return sorted(
        tiles.values(),
        key=lambda t: (int(t["zoom"]), int(t["row"]), int(t["col"]), t["tile_id"]),
    )


def manifest_summary(tiles):
    by_zoom = defaultdict(int)
    by_source = defaultdict(int)
    for tile in tiles:
        by_zoom[int(tile["zoom"])] += 1
        by_source[tile.get("source", "unknown")] += 1
    return {
        "total_tiles": len(tiles),
        "by_zoom": dict(sorted(by_zoom.items())),
        "by_source": dict(sorted(by_source.items())),
    }


def tile_to_geojson_feature(tile):
    bounds = tile["tile_bounds"]
    ring = [
        [bounds["min_lon"], bounds["max_lat"]],
        [bounds["max_lon"], bounds["max_lat"]],
        [bounds["max_lon"], bounds["min_lat"]],
        [bounds["min_lon"], bounds["min_lat"]],
        [bounds["min_lon"], bounds["max_lat"]],
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "tile_id": tile["tile_id"],
            "zoom": tile["zoom"],
            "row": tile["row"],
            "col": tile["col"],
            "enabled": tile.get("enabled", True),
            "schedule_minutes": tile.get("schedule_minutes"),
            "priority": tile.get("priority", 0),
            "source": tile.get("source"),
            "seed_regions": tile.get("seed_regions", []),
            "center_lat": tile.get("center_lat"),
            "center_lon": tile.get("center_lon"),
        },
    }


def _tile_bbox_overlap(tile, bbox):
    t = tile["tile_bounds"]
    return not (
        t["max_lon"] <= bbox[1]
        or t["min_lon"] >= bbox[3]
        or t["max_lat"] <= bbox[0]
        or t["min_lat"] >= bbox[2]
    )


def tile_intersects_polygon(tile, polygon):
    """Return True if a tile's owner bounds intersects a polygon."""
    if not polygon:
        return False
    lats = [float(p[0]) for p in polygon]
    lons = [float(p[1]) for p in polygon]
    bbox = (min(lats), min(lons), max(lats), max(lons))
    if not _tile_bbox_overlap(tile, bbox):
        return False
    bounds = tile["tile_bounds"]
    corners = [
        (bounds["max_lat"], bounds["min_lon"]),
        (bounds["max_lat"], bounds["max_lon"]),
        (bounds["min_lat"], bounds["max_lon"]),
        (bounds["min_lat"], bounds["min_lon"]),
    ]
    if any(_point_in_polygon(lat, lon, polygon) for lat, lon in corners):
        return True
    if any(
        bounds["min_lat"] <= float(lat) <= bounds["max_lat"]
        and bounds["min_lon"] <= float(lon) <= bounds["max_lon"]
        for lat, lon in polygon
    ):
        return True
    center = (tile["center_lat"], normalize_lon(tile["center_lon"]))
    return _point_in_polygon(center[0], center[1], polygon)


class GlobalTileIndex:
    """Fast owner lookup for global tile markers."""

    def __init__(self, tiles, viewport_width, viewport_height,
                 global_bbox=None, accept_buffer_px=None):
        self.tiles = {t["tile_id"]: t for t in tiles if t.get("enabled", True)}
        self.viewport_width = int(viewport_width)
        self.viewport_height = int(viewport_height)
        self.global_bbox = global_bbox or parse_global_bbox()
        self.accept_buffer_px = int(
            DEFAULT_TILE_ACCEPT_BUFFER_PX
            if accept_buffer_px is None
            else accept_buffer_px
        )
        self.by_zoom_row_col = {
            (int(t["zoom"]), int(t["row"]), int(t["col"])): t
            for t in self.tiles.values()
        }
        self.zooms_desc = sorted(
            {int(t["zoom"]) for t in self.tiles.values()},
            reverse=True,
        )

    def contains_global_bbox(self, lat, lon):
        min_lat, min_lon, max_lat, max_lon = self.global_bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def _contains_tile(self, tile, x, y):
        b = tile["owner_bounds_px"]
        buf = self.accept_buffer_px
        return (
            b["west_x"] - buf <= x <= b["east_x"] + buf
            and b["north_y"] - buf <= y <= b["south_y"] + buf
        )

    def owner_tile_id(self, lat, lon):
        lat = float(lat)
        lon = normalize_lon(lon)
        if not self.contains_global_bbox(lat, lon):
            return None
        for zoom in self.zooms_desc:
            x = lon_to_pixel_x(lon, zoom)
            y = lat_to_pixel_y(lat, zoom)
            base_col = int(math.floor(x / self.viewport_width))
            base_row = int(math.floor(y / self.viewport_height))
            candidates = []
            for row in range(base_row - 1, base_row + 2):
                for col in range(base_col - 1, base_col + 2):
                    tile = self.by_zoom_row_col.get((zoom, row, col))
                    if not tile or not self._contains_tile(tile, x, y):
                        continue
                    cx = (
                        tile["capture_bounds_px"]["west_x"]
                        + tile["capture_bounds_px"]["east_x"]
                    ) / 2.0
                    cy = (
                        tile["capture_bounds_px"]["north_y"]
                        + tile["capture_bounds_px"]["south_y"]
                    ) / 2.0
                    dist2 = (x - cx) ** 2 + (y - cy) ** 2
                    candidates.append((dist2, tile["tile_id"]))
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]))
                return candidates[0][1]
        return None

    def filter_markers_for_tile(self, tile_id, markers):
        accepted = []
        rejected = 0
        for marker in markers or []:
            lat = marker.get("lat")
            lon = marker.get("lon")
            if lat is None or lon is None:
                rejected += 1
                continue
            owner = self.owner_tile_id(lat, lon)
            if owner != tile_id:
                rejected += 1
                continue
            copy = dict(marker)
            copy["lon"] = normalize_lon(lon)
            copy["tile_id"] = tile_id
            accepted.append(copy)
        return accepted, rejected
