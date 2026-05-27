"""Coastline registration for screenshot projection correction.

Data source choice: Natural Earth land polygons. Natural Earth is public
domain, compact enough for anchor-tile matching, and can be read here as
GeoJSON or as the official .shp/.zip shapefile download without extra GIS
dependencies. Set COASTLINE_DATA_PATH/COASTLINE_GEOJSON to the file path, or
place the file at:

    data/coastline/ne_10m_land.geojson

The matcher is intentionally best-effort. It never fetches data at runtime and
never blocks scraping when the data file is missing or a tile lacks enough land
signal for a reliable registration.
"""

from __future__ import annotations

import json
import math
import os
import struct
import zipfile
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


DEFAULT_COASTLINE_PATH = Path("data/coastline/ne_10m_land.geojson")
COASTLINE_ENABLED = os.getenv("COASTLINE_ALIGNMENT", "1") == "1"
COASTLINE_PATH = Path(
    os.getenv("COASTLINE_DATA_PATH")
    or os.getenv("COASTLINE_GEOJSON", str(DEFAULT_COASTLINE_PATH))
)
MASK_SCALE = max(1, int(os.getenv("COASTLINE_MASK_SCALE", "4")))
ANCHOR_INTERVAL_TILES = max(1, int(os.getenv("COASTLINE_ANCHOR_INTERVAL_TILES", "3")))
MAX_REUSE_TILES = max(0, int(os.getenv("COASTLINE_MAX_REUSE_TILES", "8")))
MIN_CONFIDENCE = float(os.getenv("COASTLINE_MIN_CONFIDENCE", "0.28"))
MIN_REUSE_CONFIDENCE = float(os.getenv("COASTLINE_MIN_REUSE_CONFIDENCE", "0.18"))
MIN_LAND_RATIO = float(os.getenv("COASTLINE_MIN_LAND_RATIO", "0.006"))
MAX_LAND_RATIO = float(os.getenv("COASTLINE_MAX_LAND_RATIO", "0.92"))
MIN_EDGE_PIXELS = int(os.getenv("COASTLINE_MIN_EDGE_PIXELS", "180"))
COARSE_SEARCH_PX = int(os.getenv("COASTLINE_COARSE_SEARCH_PX", "96"))
COARSE_STEP_PX = max(1, int(os.getenv("COASTLINE_COARSE_STEP_PX", "16")))
FINE_SEARCH_PX = int(os.getenv("COASTLINE_FINE_SEARCH_PX", "16"))
FINE_STEP_PX = max(1, int(os.getenv("COASTLINE_FINE_STEP_PX", "4")))
SMOOTHING_ALPHA = float(os.getenv("COASTLINE_SMOOTHING_ALPHA", "0.65"))


def _total_pixels(zoom: float) -> float:
    return 256.0 * (2.0 ** float(zoom))


def _lon_to_pixel_x(lon: float, zoom: float) -> float:
    return (lon + 180.0) / 360.0 * _total_pixels(zoom)


def _pixel_x_to_lon(pixel_x: float, zoom: float) -> float:
    return pixel_x / _total_pixels(zoom) * 360.0 - 180.0


def _lat_to_pixel_y(lat: float, zoom: float) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = math.radians(lat)
    merc_y = (
        1.0
        - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi
    ) / 2.0
    return merc_y * _total_pixels(zoom)


def _pixel_y_to_lat(pixel_y: float, zoom: float) -> float:
    merc_y = pixel_y / _total_pixels(zoom)
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * merc_y))))


def _normalize_lon_for_center(lon: float, center_lon: float) -> float:
    while lon - center_lon > 180.0:
        lon -= 360.0
    while lon - center_lon < -180.0:
        lon += 360.0
    return lon


def _meters_for_delta(center_lat: float, dlat: float, dlon: float) -> float:
    lat_m = dlat * 110_574.0
    lon_m = dlon * 111_320.0 * math.cos(math.radians(center_lat))
    return math.hypot(lat_m, lon_m)


def _center_delta_from_shift(
    center_lat: float,
    center_lon: float,
    zoom: float,
    dx_px: float,
    dy_px: float,
) -> tuple[float, float, float]:
    center_x = _lon_to_pixel_x(center_lon, zoom)
    center_y = _lat_to_pixel_y(center_lat, zoom)
    actual_lon = _pixel_x_to_lon(center_x - dx_px, zoom)
    actual_lat = _pixel_y_to_lat(center_y - dy_px, zoom)
    dlat = actual_lat - center_lat
    dlon = actual_lon - center_lon
    return dlat, dlon, _meters_for_delta(center_lat, dlat, dlon)


@dataclass
class CoastlinePolygon:
    rings: list[list[tuple[float, float]]]
    bbox: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


@dataclass
class CoastlineDataset:
    path: str
    available: bool
    polygons: list[CoastlinePolygon]
    reason: str = ""


@dataclass
class CoastlineFit:
    available: bool
    usable: bool
    source: str
    confidence: float
    dx_px: float = 0.0
    dy_px: float = 0.0
    raw_dx_px: float | None = None
    raw_dy_px: float | None = None
    delta_lat: float = 0.0
    delta_lon: float = 0.0
    meters: float = 0.0
    observed_land_ratio: float = 0.0
    expected_land_ratio: float = 0.0
    observed_edge_pixels: int = 0
    expected_edge_pixels: int = 0
    score: float = 0.0
    reason: str = ""
    tile: tuple[int, int] | None = None
    data_path: str = ""

    def as_center_offset(self, image_w: int, image_h: int, zoom: float) -> dict:
        """Return a seer.py-compatible projection offset dict."""
        return {
            "center_x": (image_w / 2.0) + self.dx_px,
            "center_y": (image_h / 2.0) + self.dy_px,
            "map_lat": None,
            "map_lng": None,
            "map_zoom": zoom,
            "dpr": 1.0,
            "source": self.source,
            "confidence": self.confidence,
            "dx_px": self.dx_px,
            "dy_px": self.dy_px,
        }

    def to_log_dict(self) -> dict:
        return {
            "available": bool(self.available),
            "usable": bool(self.usable),
            "source": self.source,
            "confidence": round(float(self.confidence), 4),
            "dx_px": round(float(self.dx_px), 2),
            "dy_px": round(float(self.dy_px), 2),
            "raw_dx_px": None if self.raw_dx_px is None else round(float(self.raw_dx_px), 2),
            "raw_dy_px": None if self.raw_dy_px is None else round(float(self.raw_dy_px), 2),
            "delta_lat": round(float(self.delta_lat), 7),
            "delta_lon": round(float(self.delta_lon), 7),
            "meters": round(float(self.meters), 1),
            "observed_land_ratio": round(float(self.observed_land_ratio), 4),
            "expected_land_ratio": round(float(self.expected_land_ratio), 4),
            "observed_edge_pixels": int(self.observed_edge_pixels),
            "expected_edge_pixels": int(self.expected_edge_pixels),
            "score": round(float(self.score), 4),
            "reason": self.reason,
            "tile": list(self.tile) if self.tile else None,
            "data_path": self.data_path,
        }


def _iter_feature_geometries(payload: dict) -> Iterable[dict]:
    if payload.get("type") == "FeatureCollection":
        for feature in payload.get("features", []):
            geom = feature.get("geometry")
            if geom:
                yield geom
    elif payload.get("type") == "Feature":
        geom = payload.get("geometry")
        if geom:
            yield geom
    else:
        yield payload


def _geometry_to_polygons(geom: dict) -> Iterable[list[list[tuple[float, float]]]]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not gtype or coords is None:
        return
    if gtype == "Polygon":
        yield [
            [(float(lon), float(lat)) for lon, lat, *_ in ring]
            for ring in coords
            if len(ring) >= 4
        ]
    elif gtype == "MultiPolygon":
        for poly in coords:
            rings = [
                [(float(lon), float(lat)) for lon, lat, *_ in ring]
                for ring in poly
                if len(ring) >= 4
            ]
            if rings:
                yield rings
    elif gtype == "GeometryCollection":
        for child in geom.get("geometries", []):
            yield from _geometry_to_polygons(child)


def _bbox_for_rings(rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    lons = [lon for ring in rings for lon, _ in ring]
    lats = [lat for ring in rings for _, lat in ring]
    return min(lons), min(lats), max(lons), max(lats)


def _read_zip_member(path: Path, suffixes: tuple[str, ...]) -> bytes | None:
    with zipfile.ZipFile(path) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(suffixes)]
        if not names:
            return None
        with zf.open(names[0]) as f:
            return f.read()


def _load_geojson_polygons(path: Path, payload_bytes: bytes | None = None) -> list[CoastlinePolygon]:
    if payload_bytes is None:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.loads(payload_bytes.decode("utf-8"))

    polygons: list[CoastlinePolygon] = []
    for geom in _iter_feature_geometries(payload):
        for rings in _geometry_to_polygons(geom):
            if rings:
                polygons.append(CoastlinePolygon(rings, _bbox_for_rings(rings)))
    return polygons


def _load_shapefile_polygons(path: Path, shp_bytes: bytes | None = None) -> list[CoastlinePolygon]:
    """Read Polygon records from a .shp file without external GIS deps.

    Natural Earth land uses ordinary Polygon records. We render each ring as
    land; lake holes are harmless for coastline-offset registration and this
    keeps the parser intentionally small.
    """
    data = shp_bytes if shp_bytes is not None else path.read_bytes()
    polygons: list[CoastlinePolygon] = []
    pos = 100  # fixed-size shapefile header
    while pos + 8 <= len(data):
        try:
            _record_no, content_words = struct.unpack(">2i", data[pos:pos + 8])
            pos += 8
            end = pos + content_words * 2
            if end > len(data) or pos + 44 > len(data):
                break
            shape_type = struct.unpack("<i", data[pos:pos + 4])[0]
            if shape_type not in (5, 15, 25):  # Polygon, PolygonZ, PolygonM
                pos = end
                continue
            num_parts, num_points = struct.unpack("<2i", data[pos + 36:pos + 44])
            if num_parts <= 0 or num_points <= 0:
                pos = end
                continue
            parts_offset = pos + 44
            points_offset = parts_offset + 4 * num_parts
            if points_offset + 16 * num_points > end:
                pos = end
                continue
            parts = list(struct.unpack(
                f"<{num_parts}i", data[parts_offset:points_offset]
            ))
            parts.append(num_points)
            points = [
                struct.unpack(
                    "<2d",
                    data[points_offset + i * 16:points_offset + (i + 1) * 16],
                )
                for i in range(num_points)
            ]
            for idx in range(len(parts) - 1):
                ring = [
                    (float(lon), float(lat))
                    for lon, lat in points[parts[idx]:parts[idx + 1]]
                ]
                if len(ring) >= 4:
                    polygons.append(CoastlinePolygon([ring], _bbox_for_rings([ring])))
            pos = end
        except Exception:
            break
    return polygons


@lru_cache(maxsize=2)
def load_coastline_dataset(path_str: str | None = None) -> CoastlineDataset:
    path = Path(path_str or str(COASTLINE_PATH))
    if path_str is None and not path.exists():
        for candidate in (
            DEFAULT_COASTLINE_PATH.with_suffix(".zip"),
            DEFAULT_COASTLINE_PATH.with_suffix(".shp"),
        ):
            if candidate.exists():
                path = candidate
                break
    if not COASTLINE_ENABLED:
        return CoastlineDataset(str(path), False, [], "disabled")
    if not path.exists():
        return CoastlineDataset(str(path), False, [], "missing_file")

    try:
        suffix = path.suffix.lower()
        if suffix == ".zip":
            geojson_bytes = _read_zip_member(path, (".geojson", ".json"))
            if geojson_bytes:
                polygons = _load_geojson_polygons(path, geojson_bytes)
            else:
                shp_bytes = _read_zip_member(path, (".shp",))
                if not shp_bytes:
                    return CoastlineDataset(str(path), False, [], "zip_no_supported_member")
                polygons = _load_shapefile_polygons(path, shp_bytes)
        elif suffix == ".shp":
            polygons = _load_shapefile_polygons(path)
        else:
            polygons = _load_geojson_polygons(path)
        if not polygons:
            return CoastlineDataset(str(path), False, [], "no_polygons")
        return CoastlineDataset(str(path), True, polygons, "")
    except Exception as exc:
        return CoastlineDataset(str(path), False, [], f"load_error:{exc}")


def coastline_source_status(path_str: str | None = None) -> dict:
    dataset = load_coastline_dataset(path_str)
    return {
        "enabled": COASTLINE_ENABLED,
        "available": dataset.available,
        "path": dataset.path,
        "polygons": len(dataset.polygons),
        "reason": dataset.reason,
        "source": "Natural Earth land polygons",
    }


def _view_bbox(
    center_lat: float,
    center_lon: float,
    zoom: float,
    width: int,
    height: int,
    margin_px: int,
) -> tuple[float, float, float, float]:
    cx = _lon_to_pixel_x(center_lon, zoom)
    cy = _lat_to_pixel_y(center_lat, zoom)
    west = _pixel_x_to_lon(cx - width / 2 - margin_px, zoom)
    east = _pixel_x_to_lon(cx + width / 2 + margin_px, zoom)
    north = _pixel_y_to_lat(cy - height / 2 - margin_px, zoom)
    south = _pixel_y_to_lat(cy + height / 2 + margin_px, zoom)
    return min(west, east), min(south, north), max(west, east), max(south, north)


def _bbox_intersects(
    bbox: tuple[float, float, float, float],
    view: tuple[float, float, float, float],
) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    vmin_lon, vmin_lat, vmax_lon, vmax_lat = view
    if max_lat < vmin_lat or min_lat > vmax_lat:
        return False
    for shift in (0.0, 360.0, -360.0):
        smin_lon = min_lon + shift
        smax_lon = max_lon + shift
        if not (smax_lon < vmin_lon or smin_lon > vmax_lon):
            return True
    return False


def render_expected_land_mask(
    dataset: CoastlineDataset,
    center_lat: float,
    center_lon: float,
    zoom: float,
    image_w: int,
    image_h: int,
    scale: int = MASK_SCALE,
) -> np.ndarray:
    small_w = max(1, int(round(image_w / scale)))
    small_h = max(1, int(round(image_h / scale)))
    mask = np.zeros((small_h, small_w), dtype=np.uint8)
    margin_px = COARSE_SEARCH_PX + FINE_SEARCH_PX + 32
    view = _view_bbox(center_lat, center_lon, zoom, image_w, image_h, margin_px)
    center_x = _lon_to_pixel_x(center_lon, zoom)
    center_y = _lat_to_pixel_y(center_lat, zoom)

    def project_ring(ring: list[tuple[float, float]]) -> np.ndarray | None:
        pts = []
        for lon, lat in ring:
            adj_lon = _normalize_lon_for_center(lon, center_lon)
            x = (_lon_to_pixel_x(adj_lon, zoom) - center_x + image_w / 2) / scale
            y = (_lat_to_pixel_y(lat, zoom) - center_y + image_h / 2) / scale
            pts.append((int(round(x)), int(round(y))))
        if len(pts) < 3:
            return None
        return np.asarray(pts, dtype=np.int32)

    for poly in dataset.polygons:
        if not _bbox_intersects(poly.bbox, view):
            continue
        outer = project_ring(poly.rings[0])
        if outer is None:
            continue
        cv2.fillPoly(mask, [outer], 255)
        for hole in poly.rings[1:]:
            pts = project_ring(hole)
            if pts is not None:
                cv2.fillPoly(mask, [pts], 0)

    return mask


def observed_land_mask_from_image(img_bytes: bytes, scale: int = MASK_SCALE) -> np.ndarray | None:
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return None
    h, w = image.shape[:2]
    small_w = max(1, int(round(w / scale)))
    small_h = max(1, int(round(h / scale)))
    small = cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    # Dark MarineTraffic water is typically blue/cyan and saturated. Land is
    # lower saturation gray/olive. Very bright labels and colored ship markers
    # are excluded before morphology.
    water = (hue >= 82) & (hue <= 125) & (sat >= 32) & (val <= 185)
    bright_labels = val >= 185
    red_markers = ((hue <= 12) | (hue >= 165)) & (sat >= 80) & (val >= 70)
    green_markers = (hue >= 38) & (hue <= 95) & (sat >= 95) & (val >= 70)
    candidate = (val >= 18) & ~water & ~bright_labels & ~red_markers & ~green_markers
    mask = candidate.astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def _edge_mask(mask: np.ndarray) -> np.ndarray:
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    edge = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, kernel)
    return (edge > 0).astype(np.uint8)


def _land_ratio(mask: np.ndarray) -> float:
    return float(np.count_nonzero(mask)) / float(mask.size or 1)


def _overlap_slices(
    shape: tuple[int, int],
    dx: int,
    dy: int,
) -> tuple[slice, slice, slice, slice] | None:
    h, w = shape
    if dx >= 0:
        exp_x0, obs_x0 = 0, dx
        width = w - dx
    else:
        exp_x0, obs_x0 = -dx, 0
        width = w + dx
    if dy >= 0:
        exp_y0, obs_y0 = 0, dy
        height = h - dy
    else:
        exp_y0, obs_y0 = -dy, 0
        height = h + dy
    if width <= 0 or height <= 0:
        return None
    return (
        slice(obs_y0, obs_y0 + height),
        slice(obs_x0, obs_x0 + width),
        slice(exp_y0, exp_y0 + height),
        slice(exp_x0, exp_x0 + width),
    )


def _score_shift(
    obs_land: np.ndarray,
    exp_land: np.ndarray,
    obs_edge: np.ndarray,
    exp_edge: np.ndarray,
    dx: int,
    dy: int,
) -> float:
    slices = _overlap_slices(obs_land.shape, dx, dy)
    if slices is None:
        return 0.0
    oy, ox, ey, ex = slices
    obs_e = obs_edge[oy, ox].astype(bool)
    exp_e = exp_edge[ey, ex].astype(bool)
    edge_hit = np.count_nonzero(obs_e & exp_e)
    edge_denom = math.sqrt(
        max(1, np.count_nonzero(obs_e)) * max(1, np.count_nonzero(exp_e))
    )
    edge_score = edge_hit / edge_denom

    obs_l = obs_land[oy, ox].astype(bool)
    exp_l = exp_land[ey, ex].astype(bool)
    inter = np.count_nonzero(obs_l & exp_l)
    union = np.count_nonzero(obs_l | exp_l)
    land_score = inter / union if union else 0.0
    return 0.75 * edge_score + 0.25 * land_score


def _search_offsets(
    obs_land: np.ndarray,
    exp_land: np.ndarray,
) -> tuple[int, int, float, int, int]:
    obs_edge = _edge_mask(obs_land)
    exp_edge = _edge_mask(exp_land)
    obs_edges = int(np.count_nonzero(obs_edge))
    exp_edges = int(np.count_nonzero(exp_edge))
    if obs_edges < MIN_EDGE_PIXELS or exp_edges < MIN_EDGE_PIXELS:
        return 0, 0, 0.0, obs_edges, exp_edges

    coarse_radius = max(1, int(round(COARSE_SEARCH_PX / MASK_SCALE)))
    coarse_step = max(1, int(round(COARSE_STEP_PX / MASK_SCALE)))
    fine_radius = max(1, int(round(FINE_SEARCH_PX / MASK_SCALE)))
    fine_step = max(1, int(round(FINE_STEP_PX / MASK_SCALE)))

    best_dx = 0
    best_dy = 0
    best_score = -1.0

    for dy in range(-coarse_radius, coarse_radius + 1, coarse_step):
        for dx in range(-coarse_radius, coarse_radius + 1, coarse_step):
            score = _score_shift(obs_land, exp_land, obs_edge, exp_edge, dx, dy)
            if score > best_score:
                best_dx, best_dy, best_score = dx, dy, score

    fine_best_dx = best_dx
    fine_best_dy = best_dy
    fine_best_score = best_score
    for dy in range(best_dy - fine_radius, best_dy + fine_radius + 1, fine_step):
        for dx in range(best_dx - fine_radius, best_dx + fine_radius + 1, fine_step):
            score = _score_shift(obs_land, exp_land, obs_edge, exp_edge, dx, dy)
            if score > fine_best_score:
                fine_best_dx, fine_best_dy, fine_best_score = dx, dy, score

    return fine_best_dx, fine_best_dy, max(0.0, fine_best_score), obs_edges, exp_edges


class CoastlineOffsetTracker:
    """Stateful smoother for per-region coastline-derived projection offsets."""

    def __init__(
        self,
        region_name: str,
        logger=None,
        path: str | None = None,
        enabled: bool | None = None,
    ):
        self.region_name = region_name
        self.logger = logger
        self.dataset = load_coastline_dataset(path)
        self.enabled = COASTLINE_ENABLED if enabled is None else bool(enabled)
        self.last_fit: CoastlineFit | None = None
        self.tiles_since_anchor = MAX_REUSE_TILES + 1
        self.stats = {
            "fit": 0,
            "reused": 0,
            "fallback": 0,
            "low_confidence": 0,
            "low_signal": 0,
            "disabled": 0,
            "missing_data": 0,
        }

    def source_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.dataset.available,
            "path": self.dataset.path,
            "polygons": len(self.dataset.polygons),
            "reason": self.dataset.reason if self.enabled else "disabled",
            "source": "Natural Earth land polygons",
        }

    def _base_fit(self, source: str, reason: str, tile=None) -> CoastlineFit:
        if source in self.stats:
            self.stats[source] += 1
        elif source == "missing_file":
            self.stats["missing_data"] += 1
        return CoastlineFit(
            available=self.dataset.available,
            usable=False,
            source=source,
            confidence=0.0,
            reason=reason,
            tile=tile,
            data_path=self.dataset.path,
        )

    def _reuse_or_fallback(
        self,
        center_lat: float,
        center_lon: float,
        zoom: float,
        reason: str,
        tile=None,
    ) -> CoastlineFit:
        self.tiles_since_anchor += 1
        if self.last_fit and self.tiles_since_anchor <= MAX_REUSE_TILES:
            confidence = self.last_fit.confidence * (0.88 ** self.tiles_since_anchor)
            usable = confidence >= MIN_REUSE_CONFIDENCE
            if usable:
                self.stats["reused"] += 1
                return replace(
                    self.last_fit,
                    source="reused",
                    usable=True,
                    confidence=confidence,
                    reason=reason,
                    tile=tile,
                )
        self.stats["fallback"] += 1
        return CoastlineFit(
            available=self.dataset.available,
            usable=False,
            source="fallback",
            confidence=0.0,
            reason=reason,
            tile=tile,
            data_path=self.dataset.path,
        )

    def estimate(
        self,
        img_bytes: bytes,
        center_lat: float,
        center_lon: float,
        zoom: float,
        tile: tuple[int, int] | None = None,
    ) -> CoastlineFit:
        if not self.enabled:
            return self._base_fit("disabled", "ENABLE_COASTLINE_CALIBRATION=0", tile)
        if not self.dataset.available:
            return self._base_fit("missing_data", self.dataset.reason, tile)

        if self.last_fit and self.tiles_since_anchor < ANCHOR_INTERVAL_TILES:
            return self._reuse_or_fallback(
                center_lat, center_lon, zoom, "between_anchor_tiles", tile
            )

        obs_land = observed_land_mask_from_image(img_bytes, MASK_SCALE)
        if obs_land is None:
            return self._reuse_or_fallback(center_lat, center_lon, zoom, "decode_failed", tile)
        image_h = obs_land.shape[0] * MASK_SCALE
        image_w = obs_land.shape[1] * MASK_SCALE
        exp_land = render_expected_land_mask(
            self.dataset,
            center_lat,
            center_lon,
            zoom,
            image_w,
            image_h,
            MASK_SCALE,
        )

        obs_ratio = _land_ratio(obs_land)
        exp_ratio = _land_ratio(exp_land)
        land_signal_ok = (
            MIN_LAND_RATIO <= obs_ratio <= MAX_LAND_RATIO
            and MIN_LAND_RATIO <= exp_ratio <= MAX_LAND_RATIO
        )
        if not land_signal_ok:
            self.stats["low_signal"] += 1
            return self._reuse_or_fallback(
                center_lat,
                center_lon,
                zoom,
                f"land_signal obs={obs_ratio:.4f} exp={exp_ratio:.4f}",
                tile,
            )

        dx_small, dy_small, score, obs_edges, exp_edges = _search_offsets(obs_land, exp_land)
        raw_dx = dx_small * MASK_SCALE
        raw_dy = dy_small * MASK_SCALE
        confidence = min(1.0, max(0.0, score))
        dlat, dlon, meters = _center_delta_from_shift(
            center_lat, center_lon, zoom, raw_dx, raw_dy
        )

        fit = CoastlineFit(
            available=True,
            usable=confidence >= MIN_CONFIDENCE,
            source="fit" if confidence >= MIN_CONFIDENCE else "low_confidence",
            confidence=confidence,
            dx_px=raw_dx,
            dy_px=raw_dy,
            raw_dx_px=raw_dx,
            raw_dy_px=raw_dy,
            delta_lat=dlat,
            delta_lon=dlon,
            meters=meters,
            observed_land_ratio=obs_ratio,
            expected_land_ratio=exp_ratio,
            observed_edge_pixels=obs_edges,
            expected_edge_pixels=exp_edges,
            score=score,
            reason="ok" if confidence >= MIN_CONFIDENCE else "score_below_threshold",
            tile=tile,
            data_path=self.dataset.path,
        )

        if not fit.usable:
            self.stats["low_confidence"] += 1
            self.tiles_since_anchor += 1
            return fit

        if self.last_fit:
            smoothed_dx = (
                SMOOTHING_ALPHA * fit.dx_px
                + (1.0 - SMOOTHING_ALPHA) * self.last_fit.dx_px
            )
            smoothed_dy = (
                SMOOTHING_ALPHA * fit.dy_px
                + (1.0 - SMOOTHING_ALPHA) * self.last_fit.dy_px
            )
            dlat, dlon, meters = _center_delta_from_shift(
                center_lat, center_lon, zoom, smoothed_dx, smoothed_dy
            )
            fit = replace(
                fit,
                dx_px=smoothed_dx,
                dy_px=smoothed_dy,
                delta_lat=dlat,
                delta_lon=dlon,
                meters=meters,
            )

        self.stats["fit"] += 1
        self.last_fit = fit
        self.tiles_since_anchor = 0
        return fit

    def summary(self) -> dict:
        last = self.last_fit.to_log_dict() if self.last_fit else None
        return {
            "source": self.source_status(),
            "stats": dict(self.stats),
            "last_fit": last,
        }
