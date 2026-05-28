"""Spatial de-duplication helpers for detected vessel markers."""

import math


def _marker_distance_deg(a, b):
    """Approximate point distance in degrees, with longitude scaled by latitude."""
    lat1 = float(a["lat"])
    lat2 = float(b["lat"])
    lon1 = float(a["lon"])
    lon2 = float(b["lon"])
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(mean_lat)
    return math.hypot(dlat, dlon)


def _merge_marker(existing, candidate):
    """Keep one marker, preferring a moving classification when duplicates differ."""
    if existing.get("motion") == "moving" or candidate.get("motion") != "moving":
        return existing
    merged = dict(existing)
    merged["motion"] = "moving"
    return merged


def dedup_markers_spatial(markers, eps_deg):
    """Collapse near-identical markers from overlapping captures or tiles.

    The same rendered vessel can be detected twice when capture tiles overlap,
    or when adjacent regions include the same water. Markers are considered
    duplicates only when they have the same ship type and their projected
    coordinates are within ``eps_deg``.
    """
    if eps_deg <= 0 or not markers:
        return list(markers or [])

    cells = {}
    kept = []
    eps = float(eps_deg)

    for marker in markers:
        if marker.get("lat") is None or marker.get("lon") is None:
            kept.append(marker)
            continue

        ship_type = marker.get("type", "unknown")
        lat_cell = math.floor(float(marker["lat"]) / eps)
        lon_cell = math.floor(float(marker["lon"]) / eps)
        duplicate_idx = None

        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                key = (lat_cell + dy, lon_cell + dx, ship_type)
                for idx in cells.get(key, []):
                    if _marker_distance_deg(kept[idx], marker) <= eps:
                        duplicate_idx = idx
                        break
                if duplicate_idx is not None:
                    break
            if duplicate_idx is not None:
                break

        if duplicate_idx is None:
            idx = len(kept)
            kept.append(marker)
            cells.setdefault((lat_cell, lon_cell, ship_type), []).append(idx)
        else:
            kept[duplicate_idx] = _merge_marker(kept[duplicate_idx], marker)

    return kept


def count_markers_by_type(markers):
    """Return scraper-compatible counts for marker dictionaries."""
    counts = {
        "stationary_tankers": 0,
        "moving_tankers": 0,
        "stationary_cargos": 0,
        "moving_cargos": 0,
    }
    for marker in markers or []:
        ship_type = marker.get("type")
        motion = marker.get("motion")
        if ship_type == "tanker":
            key = "moving_tankers" if motion == "moving" else "stationary_tankers"
        elif ship_type == "cargo":
            key = "moving_cargos" if motion == "moving" else "stationary_cargos"
        else:
            continue
        counts[key] += 1
    return counts
