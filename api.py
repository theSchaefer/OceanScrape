"""FastAPI backend for the Marine Traffic Monitoring Dashboard.

Connects to the same PostgreSQL database used by update_database.py and
serves vessel positions, region analytics, and pipeline status to the
single-file dashboard at /dashboard/index.html.

Run:
    uvicorn api:app --reload
"""

import json
import math
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from global_tile_grid import (
    build_global_tile_manifest,
    parse_global_bbox,
    tile_to_geojson_feature,
)
from marker_dedup import count_markers_by_type, dedup_markers_spatial
from regions import REGIONS
from update_database import (
    _SCHEMA_SQL,
    _sync_capture_tile_manifest,
    GLOBAL_GRID_DEFAULT_ZOOM as DB_GLOBAL_GRID_DEFAULT_ZOOM,
)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "7680"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "4320"))
GLOBAL_GRID_BBOX = parse_global_bbox(os.getenv("GLOBAL_GRID_BBOX"))
GLOBAL_GRID_DEFAULT_ZOOM = int(os.getenv(
    "GLOBAL_GRID_DEFAULT_ZOOM",
    str(DB_GLOBAL_GRID_DEFAULT_ZOOM),
))
# Default spatial dedup bucket for cross-region marker overlap. 0.003° ≈ 330 m
# at the equator — large enough to collapse the same ship detected by two
# overlapping regions (different zooms ⇒ different pixel→latlon roundings),
# small enough to keep neighboring distinct vessels separate.
VESSEL_DEDUP_EPS_DEG = float(os.getenv(
    "MARKER_DEDUP_EPS_DEG",
    os.getenv("VESSEL_DEDUP_EPS_DEG", "0.003"),
))

app = FastAPI(title="MarineScraper Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable is required")
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_regions (
    id              BIGSERIAL PRIMARY KEY,
    code            VARCHAR(16) UNIQUE NOT NULL,
    name            VARCHAR(128) NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    polygon         JSONB NOT NULL,
    zoom            SMALLINT NOT NULL DEFAULT 12,
    high_threshold  INTEGER,
    low_threshold   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    region_code     VARCHAR(16) NOT NULL,
    alert_type      VARCHAR(32) NOT NULL,
    message         TEXT NOT NULL,
    value           INTEGER,
    threshold       INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alerts_region
    ON alerts (region_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_unacked
    ON alerts (acknowledged_at) WHERE acknowledged_at IS NULL;
"""


@app.on_event("startup")
def _startup():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
            cur.execute(_EXTRA_SCHEMA)
            _sync_capture_tile_manifest(cur)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _polygon_area_km2(polygon):
    """Approximate area of a lat/lon polygon in km² using Shoelace formula."""
    n = len(polygon)
    if n < 3:
        return 0.0
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    avg_lat = sum(lats) / n
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.32 * math.cos(math.radians(avg_lat))
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        xi = lons[i] * km_per_deg_lon
        yi = lats[i] * km_per_deg_lat
        xj = lons[j] * km_per_deg_lon
        yj = lats[j] * km_per_deg_lat
        area += xi * yj - xj * yi
    return abs(area) / 2.0


def _polygon_centroid(polygon):
    """Return (lat, lon) centroid of a polygon."""
    n = len(polygon)
    if n == 0:
        return (0, 0)
    return (
        sum(p[0] for p in polygon) / n,
        sum(p[1] for p in polygon) / n,
    )


def _polygon_bbox(polygon):
    """Return [min_lon, min_lat, max_lon, max_lat] bounding box."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return [min(lons), min(lats), max(lons), max(lats)]


def _point_in_polygon(lat, lon, polygon):
    """Ray-casting point-in-polygon test. Polygon is list of (lat, lon)."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _custom_region_stats_from_vp(cur, polygon, capture_ids):
    """Count vessels inside a polygon from vessel_positions for given capture_ids.
    Returns dict with tankers, cargos, moving_tankers, moving_cargos counts.
    """
    if not capture_ids:
        return None
    # Use bounding box for initial SQL filter, then refine with point-in-polygon
    bbox = _polygon_bbox(polygon)  # [min_lon, min_lat, max_lon, max_lat]
    placeholders = ",".join(["%s"] * len(capture_ids))
    cur.execute(f"""
        SELECT vp.lat, vp.lon, vp.ship_type, vp.motion, c.captured_at
        FROM vessel_positions vp
        JOIN captures c ON c.id = vp.capture_id
        WHERE vp.capture_id IN ({placeholders})
              AND vp.lat BETWEEN %s AND %s
              AND vp.lon BETWEEN %s AND %s
    """, capture_ids + [bbox[1], bbox[3], bbox[0], bbox[2]])
    rows = cur.fetchall()

    tankers = cargos = moving_tankers = moving_cargos = 0
    for r in rows:
        if not _point_in_polygon(r["lat"], r["lon"], polygon):
            continue
        is_tanker = r["ship_type"] == "tanker"
        is_moving = r["motion"] == "moving"
        if is_tanker and is_moving:
            moving_tankers += 1
        elif is_tanker:
            tankers += 1
        elif is_moving:
            moving_cargos += 1
        else:
            cargos += 1
    return {
        "tankers": tankers, "cargos": cargos,
        "moving_tankers": moving_tankers, "moving_cargos": moving_cargos,
        "total_ships": tankers + cargos + moving_tankers + moving_cargos,
    }


def _resolve_region(cur, code):
    """Return (name, polygon, is_custom) for predefined or custom regions."""
    region_def = REGIONS.get(code)
    if region_def:
        return region_def["name"], region_def["polygon"], False
    cur.execute("SELECT name, polygon FROM custom_regions WHERE code = %s", (code,))
    row = cur.fetchone()
    if row:
        return row["name"], row["polygon"], True
    return None, None, False


def _latest_global_snapshot(cur, timestamp=None):
    if timestamp:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        cur.execute("""
            SELECT captured_at
            FROM tile_captures
            ORDER BY ABS(EXTRACT(EPOCH FROM captured_at - %s))
            LIMIT 1
        """, (ts,))
    else:
        cur.execute("SELECT MAX(captured_at) AS captured_at FROM tile_captures")
    row = cur.fetchone()
    return row["captured_at"] if row else None


def _global_rows_for_polygon(cur, polygon, snapshot_ts=None,
                             start_dt=None, end_dt=None,
                             type_filter=None, motion_filter=None,
                             bucket_trunc=None):
    bbox = _polygon_bbox(polygon)
    select_bucket = ", date_trunc(%s, gvp.captured_at) AS bucket" if bucket_trunc else ""
    args = []
    if bucket_trunc:
        args.append(bucket_trunc)
    sql = f"""
        SELECT gvp.tile_id, gvp.captured_at, gvp.lat, gvp.lon,
               gvp.ship_type, gvp.motion{select_bucket}
        FROM global_vessel_positions gvp
        WHERE gvp.lat BETWEEN %s AND %s
          AND gvp.lon BETWEEN %s AND %s
    """
    args.extend([bbox[1], bbox[3], bbox[0], bbox[2]])
    if snapshot_ts is not None:
        sql += " AND gvp.captured_at = %s"
        args.append(snapshot_ts)
    if start_dt is not None:
        sql += " AND gvp.captured_at >= %s"
        args.append(start_dt)
    if end_dt is not None:
        sql += " AND gvp.captured_at <= %s"
        args.append(end_dt)
    if type_filter:
        sql += " AND gvp.ship_type = %s"
        args.append(type_filter)
    if motion_filter:
        sql += " AND gvp.motion = %s"
        args.append(motion_filter)
    sql += " ORDER BY gvp.captured_at, gvp.id"
    cur.execute(sql, args)
    return [
        r for r in cur.fetchall()
        if _point_in_polygon(r["lat"], r["lon"], polygon)
    ]


def _count_global_rows(rows):
    tankers = cargos = moving_tankers = moving_cargos = 0
    for r in rows:
        ship_type = r.get("ship_type") or r.get("type")
        motion = r.get("motion")
        if ship_type == "tanker" and motion == "moving":
            moving_tankers += 1
        elif ship_type == "tanker":
            tankers += 1
        elif motion == "moving":
            moving_cargos += 1
        else:
            cargos += 1
    total = tankers + cargos + moving_tankers + moving_cargos
    return {
        "tankers": tankers,
        "cargos": cargos,
        "moving_tankers": moving_tankers,
        "moving_cargos": moving_cargos,
        "total_ships": total,
    }


def _global_stats_for_polygon(cur, polygon, snapshot_ts):
    if snapshot_ts is None:
        return None
    rows = _global_rows_for_polygon(cur, polygon, snapshot_ts=snapshot_ts)
    return _count_global_rows(rows)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CustomRegionCreate(BaseModel):
    code: str
    name: str
    description: str = ""
    polygon: list  # [[lat, lon], ...]
    zoom: int = 12
    high_threshold: Optional[int] = None
    low_threshold: Optional[int] = None


class CustomRegionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    polygon: Optional[list] = None
    zoom: Optional[int] = None
    high_threshold: Optional[int] = None
    low_threshold: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers – active region codes
# ---------------------------------------------------------------------------

def _valid_region_codes():
    """Return the set of region codes that currently exist (predefined + custom)."""
    codes = set(REGIONS.keys())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM custom_regions")
            codes.update(r[0] for r in cur.fetchall())
    return codes


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    return {"mapbox_token": MAPBOX_TOKEN}


@app.get("/api/status")
def get_status():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(captured_at) AS last_scrape FROM tile_captures")
            row = cur.fetchone()
            last_scrape = row["last_scrape"] if row else None

            cur.execute("SELECT COUNT(*) AS total FROM tile_captures")
            total_captures = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM global_vessel_positions")
            total_markers = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS cnt FROM capture_tiles WHERE enabled")
            regions_active = cur.fetchone()["cnt"]

            last_run_stats = None
            if last_scrape:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE status = 'success') AS regions_ok,
                           COUNT(*) FILTER (WHERE status != 'success') AS regions_failed,
                           SUM(tankers + cargos + moving_tankers + moving_cargos) AS total_ships
                    FROM tile_captures
                    WHERE captured_at = %s
                """, (last_scrape,))
                last_run_stats = dict(cur.fetchone())

    next_scheduled = None
    if last_scrape:
        next_scheduled = (last_scrape + timedelta(minutes=SCRAPE_INTERVAL_MINUTES)).isoformat()

    return {
        "last_scrape": last_scrape.isoformat() if last_scrape else None,
        "next_scheduled": next_scheduled,
        "interval_minutes": SCRAPE_INTERVAL_MINUTES,
        "total_captures": total_captures,
        "total_markers": total_markers,
        "regions_active": regions_active,
        "last_run_stats": last_run_stats,
    }


@app.get("/api/regions_legacy", include_in_schema=False)
def get_regions():
    regions_out = []

    # Predefined regions
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Sidebar shows each region's most recent known state, so the
            # "latest" here is intentionally per-region (mixed timestamps
            # across regions are expected for this meta-view). The map's
            # snapshot — which must be a single coherent cycle — is built
            # via /api/vessels instead.
            cur.execute("""
                SELECT DISTINCT ON (region)
                       region, captured_at, tankers, cargos,
                       moving_tankers, moving_cargos
                FROM captures
                ORDER BY region, captured_at DESC
            """)
            latest_map = {r["region"]: r for r in cur.fetchall()}

            # Custom regions
            cur.execute("SELECT * FROM custom_regions ORDER BY created_at")
            custom_rows = cur.fetchall()

            # Unacked alert counts per region
            cur.execute("""
                SELECT region_code, COUNT(*) AS cnt
                FROM alerts
                WHERE acknowledged_at IS NULL
                GROUP BY region_code
            """)
            alert_counts = {r["region_code"]: r["cnt"] for r in cur.fetchall()}

    for code, rdef in REGIONS.items():
        polygon = rdef["polygon"]
        area = _polygon_area_km2(polygon)
        latest = latest_map.get(code)
        total = 0
        latest_data = None
        if latest:
            total = (latest["tankers"] + latest["cargos"]
                     + latest["moving_tankers"] + latest["moving_cargos"])
            latest_data = {
                "captured_at": latest["captured_at"].isoformat(),
                "tankers": latest["tankers"],
                "cargos": latest["cargos"],
                "moving_tankers": latest["moving_tankers"],
                "moving_cargos": latest["moving_cargos"],
                "total_ships": total,
                "density": round(total / area, 2) if area > 0 else 0,
            }
        regions_out.append({
            "code": code,
            "name": rdef["name"],
            "type": "predefined",
            "polygon": [[p[0], p[1]] for p in polygon],
            "zoom": rdef["zoom"],
            "bbox": _polygon_bbox(polygon),
            "centroid": list(_polygon_centroid(polygon)),
            "area_km2": round(area, 1),
            "latest": latest_data,
            "thresholds": {"high": None, "low": None},
            "unacked_alerts": alert_counts.get(code, 0),
        })

    # For custom regions, compute latest stats from vessel_positions within polygon
    for cr in custom_rows:
        polygon = cr["polygon"]  # already JSONB list
        area = _polygon_area_km2(polygon)
        latest_data = None

        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Pin the custom-region "latest" to ONE cycle so stats stay
                # coherent. Without this, capture_ids span different cycles
                # per underlying predefined region and the same vessel can
                # be double-counted across cycles.
                valid = list(_valid_region_codes())
                cur.execute("""
                    SELECT MAX(captured_at) AS captured_at
                    FROM captures WHERE region = ANY(%s)
                """, (valid,))
                snapshot_row = cur.fetchone()
                snapshot_ts = snapshot_row["captured_at"] if snapshot_row else None
                if snapshot_ts is not None:
                    cur.execute("""
                        SELECT id FROM captures
                        WHERE captured_at = %s AND region = ANY(%s)
                    """, (snapshot_ts, valid))
                    capture_ids = [r["id"] for r in cur.fetchall()]
                    stats = _custom_region_stats_from_vp(cur, polygon, capture_ids)
                    if stats and stats["total_ships"] > 0:
                        latest_data = {
                            "captured_at": snapshot_ts.isoformat(),
                            **stats,
                            "density": round(stats["total_ships"] / area, 2) if area > 0 else 0,
                        }

        regions_out.append({
            "code": cr["code"],
            "name": cr["name"],
            "type": "custom",
            "polygon": polygon,
            "zoom": cr["zoom"],
            "bbox": _polygon_bbox(polygon),
            "centroid": list(_polygon_centroid(polygon)),
            "area_km2": round(area, 1),
            "latest": latest_data,
            "thresholds": {
                "high": cr["high_threshold"],
                "low": cr["low_threshold"],
            },
            "unacked_alerts": alert_counts.get(cr["code"], 0),
            "description": cr["description"],
            "id": cr["id"],
        })

    # Check alerts for custom regions with thresholds
    _check_alerts(regions_out)

    return {"regions": regions_out}


@app.get("/api/regions")
def get_regions_global():
    regions_out = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            snapshot_ts = _latest_global_snapshot(cur)
            cur.execute("SELECT * FROM custom_regions ORDER BY created_at")
            custom_rows = cur.fetchall()
            cur.execute("""
                SELECT region_code, COUNT(*) AS cnt
                FROM alerts
                WHERE acknowledged_at IS NULL
                GROUP BY region_code
            """)
            alert_counts = {r["region_code"]: r["cnt"] for r in cur.fetchall()}

            for code, rdef in REGIONS.items():
                polygon = rdef["polygon"]
                area = _polygon_area_km2(polygon)
                stats = _global_stats_for_polygon(cur, polygon, snapshot_ts)
                latest_data = None
                if stats and stats["total_ships"] > 0:
                    latest_data = {
                        "captured_at": snapshot_ts.isoformat(),
                        **stats,
                        "density": round(stats["total_ships"] / area, 2) if area > 0 else 0,
                    }
                regions_out.append({
                    "code": code,
                    "name": rdef["name"],
                    "type": "predefined",
                    "polygon": [[p[0], p[1]] for p in polygon],
                    "zoom": rdef["zoom"],
                    "bbox": _polygon_bbox(polygon),
                    "centroid": list(_polygon_centroid(polygon)),
                    "area_km2": round(area, 1),
                    "latest": latest_data,
                    "thresholds": {"high": None, "low": None},
                    "unacked_alerts": alert_counts.get(code, 0),
                })

            for cr in custom_rows:
                polygon = cr["polygon"]
                area = _polygon_area_km2(polygon)
                stats = _global_stats_for_polygon(cur, polygon, snapshot_ts)
                latest_data = None
                if stats and stats["total_ships"] > 0:
                    latest_data = {
                        "captured_at": snapshot_ts.isoformat(),
                        **stats,
                        "density": round(stats["total_ships"] / area, 2) if area > 0 else 0,
                    }
                regions_out.append({
                    "code": cr["code"],
                    "name": cr["name"],
                    "type": "custom",
                    "polygon": polygon,
                    "zoom": cr["zoom"],
                    "bbox": _polygon_bbox(polygon),
                    "centroid": list(_polygon_centroid(polygon)),
                    "area_km2": round(area, 1),
                    "latest": latest_data,
                    "thresholds": {
                        "high": cr["high_threshold"],
                        "low": cr["low_threshold"],
                    },
                    "unacked_alerts": alert_counts.get(cr["code"], 0),
                    "description": cr["description"],
                    "id": cr["id"],
                })

    _check_alerts(regions_out)
    return {"regions": regions_out}


def _check_alerts(regions_list):
    """Generate alerts for custom regions whose thresholds are crossed."""
    for r in regions_list:
        if r["type"] != "custom" or not r["latest"]:
            continue
        total = r["latest"]["total_ships"]
        code = r["code"]

        for ttype, threshold_key, alert_type in [
            ("high", "high", "high_traffic"),
            ("low", "low", "low_traffic"),
        ]:
            threshold = r["thresholds"].get(threshold_key)
            if threshold is None:
                continue
            triggered = (
                (ttype == "high" and total > threshold) or
                (ttype == "low" and total < threshold)
            )
            if not triggered:
                continue

            with get_conn() as conn:
                with conn.cursor() as cur:
                    # Skip if recent unacked alert exists for same type
                    cur.execute("""
                        SELECT id FROM alerts
                        WHERE region_code = %s AND alert_type = %s
                              AND acknowledged_at IS NULL
                              AND created_at > NOW() - INTERVAL '1 hour'
                        LIMIT 1
                    """, (code, alert_type))
                    if cur.fetchone():
                        continue
                    msg = f"{r['name']}: {total} ships detected (threshold: {threshold})"
                    cur.execute("""
                        INSERT INTO alerts (region_code, alert_type, message, value, threshold)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (code, alert_type, msg, total, threshold))


@app.get("/api/regions/{code}/analytics")
def get_region_analytics_global(
    code: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    granularity: str = Query("hourly", pattern="^(hourly|daily|weekly)$"),
):
    from collections import defaultdict

    trunc_map = {"hourly": "hour", "daily": "day", "weekly": "week"}
    trunc = trunc_map[granularity]
    now = datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
    start_dt = (datetime.fromisoformat(start.replace("Z", "+00:00")) if start
                else (end_dt - timedelta(days=7)))

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            region_name, polygon, _is_custom = _resolve_region(cur, code)
            if not polygon:
                raise HTTPException(404, f"Unknown region '{code}'")
            area = _polygon_area_km2(polygon)
            rows = _global_rows_for_polygon(
                cur,
                polygon,
                start_dt=start_dt,
                end_dt=end_dt,
                bucket_trunc=trunc,
            )

    snapshots = defaultdict(lambda: {
        "tankers": 0,
        "cargos": 0,
        "moving_tankers": 0,
        "moving_cargos": 0,
    })
    buckets = defaultdict(set)
    for r in rows:
        key = (r["bucket"], r["captured_at"])
        buckets[r["bucket"]].add(r["captured_at"])
        d = snapshots[key]
        is_tanker = r["ship_type"] == "tanker"
        is_moving = r["motion"] == "moving"
        if is_tanker and is_moving:
            d["moving_tankers"] += 1
        elif is_tanker:
            d["tankers"] += 1
        elif is_moving:
            d["moving_cargos"] += 1
        else:
            d["cargos"] += 1

    series = []
    for bucket in sorted(buckets.keys()):
        snap_keys = [(b, ts) for (b, ts) in snapshots if b == bucket]
        n = max(1, len(snap_keys))
        agg = {"tankers": 0, "cargos": 0, "moving_tankers": 0, "moving_cargos": 0}
        for key in snap_keys:
            for field in agg:
                agg[field] += snapshots[key][field]
        avg = {field: int(round(value / n)) for field, value in agg.items()}
        total = sum(avg.values())
        series.append({
            "timestamp": bucket.isoformat(),
            **avg,
            "total_ships": total,
            "density": round(total / area, 2) if area > 0 else 0,
        })

    totals = [s["total_ships"] for s in series]
    avg_total = int(sum(totals) / len(totals)) if totals else 0
    max_total = max(totals) if totals else 0
    min_total = min(totals) if totals else 0
    peak_hour = None
    if rows:
        hour_counts = defaultdict(int)
        for r in rows:
            hour_counts[r["captured_at"].hour] += 1
        peak_hour = max(hour_counts, key=hour_counts.get)

    return {
        "region": code,
        "region_name": region_name,
        "granularity": granularity,
        "series": series,
        "kpis": {
            "avg_total": avg_total,
            "max_total": max_total,
            "min_total": min_total,
            "avg_density": round(avg_total / area, 2) if area > 0 else 0,
            "captures_count": len({r["captured_at"] for r in rows}),
            "peak_hour": peak_hour,
        },
    }


@app.get("/api/regions_legacy/{code}/analytics", include_in_schema=False)
def get_region_analytics(
    code: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    granularity: str = Query("hourly", pattern="^(hourly|daily|weekly)$"),
):
    trunc_map = {"hourly": "hour", "daily": "day", "weekly": "week"}
    trunc = trunc_map[granularity]

    # Defaults: last 7 days
    now = datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
    start_dt = (datetime.fromisoformat(start.replace("Z", "+00:00")) if start
                else (end_dt - timedelta(days=7)))

    # Look up region polygon for density
    region_def = REGIONS.get(code)
    area = 0.0
    region_name = code
    polygon = None
    is_custom = False

    if region_def:
        area = _polygon_area_km2(region_def["polygon"])
        region_name = region_def["name"]
        polygon = region_def["polygon"]
    else:
        # Check custom regions
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name, polygon FROM custom_regions WHERE code = %s", (code,))
                cr = cur.fetchone()
                if cr:
                    area = _polygon_area_km2(cr["polygon"])
                    region_name = cr["name"]
                    polygon = cr["polygon"]
                    is_custom = True

    if is_custom and polygon:
        return _custom_region_analytics(code, region_name, polygon, area,
                                        start_dt, end_dt, trunc, granularity)

    # Predefined region: query captures table directly
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT date_trunc(%s, captured_at) AS bucket,
                       AVG(tankers)::int AS tankers,
                       AVG(cargos)::int AS cargos,
                       AVG(moving_tankers)::int AS moving_tankers,
                       AVG(moving_cargos)::int AS moving_cargos,
                       AVG(tankers + cargos + moving_tankers + moving_cargos)::int AS total_ships,
                       COUNT(*) AS captures_in_bucket
                FROM captures
                WHERE region = %s
                      AND captured_at >= %s
                      AND captured_at <= %s
                GROUP BY bucket
                ORDER BY bucket
            """, (trunc, code, start_dt, end_dt))
            series = []
            for row in cur.fetchall():
                total = row["total_ships"] or 0
                series.append({
                    "timestamp": row["bucket"].isoformat(),
                    "tankers": row["tankers"] or 0,
                    "cargos": row["cargos"] or 0,
                    "moving_tankers": row["moving_tankers"] or 0,
                    "moving_cargos": row["moving_cargos"] or 0,
                    "total_ships": total,
                    "density": round(total / area, 2) if area > 0 else 0,
                })

            # KPIs
            cur.execute("""
                SELECT AVG(tankers + cargos + moving_tankers + moving_cargos)::int AS avg_total,
                       MAX(tankers + cargos + moving_tankers + moving_cargos) AS max_total,
                       MIN(tankers + cargos + moving_tankers + moving_cargos) AS min_total,
                       COUNT(*) AS captures_count
                FROM captures
                WHERE region = %s
                      AND captured_at >= %s
                      AND captured_at <= %s
            """, (code, start_dt, end_dt))
            kpi_row = cur.fetchone()

            # Peak hour
            cur.execute("""
                SELECT EXTRACT(HOUR FROM captured_at)::int AS hr,
                       AVG(tankers + cargos + moving_tankers + moving_cargos) AS avg_ships
                FROM captures
                WHERE region = %s
                      AND captured_at >= %s
                      AND captured_at <= %s
                GROUP BY hr
                ORDER BY avg_ships DESC
                LIMIT 1
            """, (code, start_dt, end_dt))
            peak_row = cur.fetchone()

    avg_total = kpi_row["avg_total"] or 0
    return {
        "region": code,
        "region_name": region_name,
        "granularity": granularity,
        "series": series,
        "kpis": {
            "avg_total": avg_total,
            "max_total": kpi_row["max_total"] or 0,
            "min_total": kpi_row["min_total"] or 0,
            "avg_density": round(avg_total / area, 2) if area > 0 else 0,
            "captures_count": kpi_row["captures_count"],
            "peak_hour": peak_row["hr"] if peak_row else None,
        },
    }


def _custom_region_analytics(code, region_name, polygon, area,
                             start_dt, end_dt, trunc, granularity):
    """Compute analytics for a custom region by scanning vessel_positions."""
    from collections import defaultdict

    bbox = _polygon_bbox(polygon)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get all vessel positions within the bbox and time range
            cur.execute("""
                SELECT vp.lat, vp.lon, vp.ship_type, vp.motion,
                       date_trunc(%s, c.captured_at) AS bucket,
                       c.captured_at
                FROM vessel_positions vp
                JOIN captures c ON c.id = vp.capture_id
                WHERE c.captured_at >= %s AND c.captured_at <= %s
                      AND vp.lat BETWEEN %s AND %s
                      AND vp.lon BETWEEN %s AND %s
            """, (trunc, start_dt, end_dt, bbox[1], bbox[3], bbox[0], bbox[2]))
            rows = cur.fetchall()

    # Group by time bucket, filter by polygon
    buckets = defaultdict(lambda: {"tankers": 0, "cargos": 0,
                                   "moving_tankers": 0, "moving_cargos": 0})
    for r in rows:
        if not _point_in_polygon(r["lat"], r["lon"], polygon):
            continue
        b = r["bucket"]
        is_tanker = r["ship_type"] == "tanker"
        is_moving = r["motion"] == "moving"
        if is_tanker and is_moving:
            buckets[b]["moving_tankers"] += 1
        elif is_tanker:
            buckets[b]["tankers"] += 1
        elif is_moving:
            buckets[b]["moving_cargos"] += 1
        else:
            buckets[b]["cargos"] += 1

    series = []
    for bucket in sorted(buckets.keys()):
        d = buckets[bucket]
        total = d["tankers"] + d["cargos"] + d["moving_tankers"] + d["moving_cargos"]
        series.append({
            "timestamp": bucket.isoformat(),
            **d,
            "total_ships": total,
            "density": round(total / area, 2) if area > 0 else 0,
        })

    # KPIs
    totals = [s["total_ships"] for s in series]
    avg_total = int(sum(totals) / len(totals)) if totals else 0
    max_total = max(totals) if totals else 0
    min_total = min(totals) if totals else 0

    # Peak hour: aggregate by hour-of-day
    from collections import Counter
    hour_sums = defaultdict(list)
    for r in rows:
        if not _point_in_polygon(r["lat"], r["lon"], polygon):
            continue
        hour_sums[r["captured_at"].hour].append(1)
    peak_hour = None
    if hour_sums:
        peak_hour = max(hour_sums, key=lambda h: len(hour_sums[h]))

    return {
        "region": code,
        "region_name": region_name,
        "granularity": granularity,
        "series": series,
        "kpis": {
            "avg_total": avg_total,
            "max_total": max_total,
            "min_total": min_total,
            "avg_density": round(avg_total / area, 2) if area > 0 else 0,
            "captures_count": len(series),
            "peak_hour": peak_hour,
        },
    }


def _dedup_markers_spatial(vessels, eps_deg):
    """Collapse near-duplicate markers from overlapping regions.

    Snaps each (lat, lon) to a grid of ``eps_deg`` and keeps the first
    marker seen per (cell, ship_type). Motion is not part of the key:
    if the same ship gets classified moving in one region and stationary
    in another, we keep one — preferring 'moving' (more specific signal
    from the arrow glyph than the generic circle).
    """
    if eps_deg <= 0 or not vessels:
        return vessels
    by_key = {}
    order = []
    for v in vessels:
        key = (
            round(v["lat"] / eps_deg),
            round(v["lon"] / eps_deg),
            v.get("type", "unknown"),
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = v
            order.append(key)
        elif existing.get("motion") != "moving" and v.get("motion") == "moving":
            by_key[key] = v
    return [by_key[k] for k in order]


@app.get("/api/vessels")
def get_vessels_global(
    region: Optional[str] = None,
    type: Optional[str] = Query(None, pattern="^(tanker|cargo)$"),
    motion: Optional[str] = Query(None, pattern="^(moving|stationary)$"),
    timestamp: Optional[str] = None,
    dedup: bool = True,
    dedup_eps: Optional[float] = Query(None, ge=0, le=1),
):
    snapshot_ts = None
    vessels = []
    tiles_expected = 0
    tiles_returned = 0
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            snapshot_ts = _latest_global_snapshot(cur, timestamp=timestamp)
            cur.execute("SELECT COUNT(*) AS total FROM capture_tiles WHERE enabled")
            tiles_expected = cur.fetchone()["total"]
            if snapshot_ts:
                cur.execute("""
                    SELECT COUNT(*) AS total
                    FROM tile_captures
                    WHERE captured_at = %s
                """, (snapshot_ts,))
                tiles_returned = cur.fetchone()["total"]

            if region:
                _name, polygon, _is_custom = _resolve_region(cur, region)
                if not polygon:
                    raise HTTPException(404, f"Unknown region '{region}'")
                rows = _global_rows_for_polygon(
                    cur,
                    polygon,
                    snapshot_ts=snapshot_ts,
                    type_filter=type,
                    motion_filter=motion,
                ) if snapshot_ts else []
            elif snapshot_ts:
                sql = """
                    SELECT tile_id, captured_at, lat, lon, ship_type, motion
                    FROM global_vessel_positions
                    WHERE captured_at = %s
                """
                args = [snapshot_ts]
                if type:
                    sql += " AND ship_type = %s"
                    args.append(type)
                if motion:
                    sql += " AND motion = %s"
                    args.append(motion)
                sql += " ORDER BY tile_id, id"
                cur.execute(sql, args)
                rows = cur.fetchall()
            else:
                rows = []

    for row in rows:
        vessels.append({
            "lat": row["lat"],
            "lon": row["lon"],
            "type": row["ship_type"],
            "motion": row["motion"],
            "tile_id": row["tile_id"],
            "region": region,
        })

    raw_count = len(vessels)
    if dedup:
        eps = dedup_eps if dedup_eps is not None else VESSEL_DEDUP_EPS_DEG
        vessels = dedup_markers_spatial(vessels, eps)

    snapshot_iso = snapshot_ts.isoformat() if snapshot_ts else None
    return {
        "timestamp": snapshot_iso,
        "snapshot_timestamp": snapshot_iso,
        "coverage": {
            "tiles_returned": tiles_returned,
            "tiles_expected": tiles_expected,
            "regions_returned": tiles_returned,
            "regions_expected": tiles_expected,
        },
        "vessels": vessels,
        "count": len(vessels),
        "raw_count": raw_count,
    }


@app.get("/api/vessels_legacy", include_in_schema=False)
def get_vessels(
    region: Optional[str] = None,
    type: Optional[str] = Query(None, pattern="^(tanker|cargo)$"),
    motion: Optional[str] = Query(None, pattern="^(moving|stationary)$"),
    timestamp: Optional[str] = None,
    dedup: bool = True,
    dedup_eps: Optional[float] = Query(None, ge=0, le=1),
):
    snapshot_ts = None
    regions_expected = 0
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if region:
                # Single-region: no cross-region mixing possible. Use the
                # region's own latest, or its row closest to a requested
                # timestamp.
                if timestamp:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    cur.execute("""
                        SELECT id, region, captured_at, markers
                        FROM captures
                        WHERE region = %s
                        ORDER BY ABS(EXTRACT(EPOCH FROM captured_at - %s))
                        LIMIT 1
                    """, (region, ts))
                else:
                    cur.execute("""
                        SELECT id, region, captured_at, markers
                        FROM captures
                        WHERE region = %s
                        ORDER BY captured_at DESC
                        LIMIT 1
                    """, (region,))
                rows = cur.fetchall()
                if rows:
                    snapshot_ts = rows[0]["captured_at"]
                regions_expected = 1
            else:
                # Cross-region: strict snapshot. Resolve a single cycle
                # timestamp T, then fetch every row at captured_at = T.
                # Regions missing from cycle T are reported via `coverage`,
                # not silently filled with stale captures from earlier cycles.
                valid = list(_valid_region_codes())
                regions_expected = len(valid)
                if timestamp:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    # No DISTINCT: Postgres rejects `SELECT DISTINCT … ORDER BY <expr>`
                    # when the expression isn't in the select list. Picking a single
                    # row from the closest cycle yields the same captured_at as any
                    # other row in that cycle, so the column is read uniformly.
                    cur.execute("""
                        SELECT captured_at
                        FROM captures
                        WHERE region = ANY(%s)
                        ORDER BY ABS(EXTRACT(EPOCH FROM captured_at - %s))
                        LIMIT 1
                    """, (valid, ts))
                else:
                    cur.execute("""
                        SELECT MAX(captured_at) AS captured_at
                        FROM captures
                        WHERE region = ANY(%s)
                    """, (valid,))
                row = cur.fetchone()
                snapshot_ts = row["captured_at"] if row else None

                if snapshot_ts is None:
                    rows = []
                else:
                    cur.execute("""
                        SELECT id, region, captured_at, markers
                        FROM captures
                        WHERE captured_at = %s AND region = ANY(%s)
                    """, (snapshot_ts, valid))
                    rows = cur.fetchall()

    vessels = []
    for row in rows:
        markers = row["markers"] if isinstance(row["markers"], list) else json.loads(row["markers"])
        for m in markers:
            if type and m.get("type") != type:
                continue
            if motion and m.get("motion") != motion:
                continue
            vessels.append({
                "lat": m["lat"],
                "lon": m["lon"],
                "type": m.get("type", "unknown"),
                "motion": m.get("motion", "unknown"),
                "region": row["region"],
            })

    # Spatial dedup protects both cross-region overlap and within-region tile
    # overlap from bbox capture margins. Applying it here also cleans up older
    # capture rows that were stored before scraper-side marker dedup existed.
    raw_count = len(vessels)
    if dedup:
        eps = dedup_eps if dedup_eps is not None else VESSEL_DEDUP_EPS_DEG
        vessels = dedup_markers_spatial(vessels, eps)

    snapshot_iso = snapshot_ts.isoformat() if snapshot_ts else None
    return {
        # `timestamp` is the back-compat alias for `snapshot_timestamp`.
        "timestamp": snapshot_iso,
        "snapshot_timestamp": snapshot_iso,
        "coverage": {
            "regions_returned": len(rows),
            "regions_expected": regions_expected,
        },
        "vessels": vessels,
        "count": len(vessels),
        "raw_count": raw_count,
    }


@app.get("/api/vessels/history")
def get_vessels_history_global(
    region: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_frames: int = Query(100, ge=1, le=500),
):
    now = datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
    start_dt = (datetime.fromisoformat(start.replace("Z", "+00:00")) if start
                else (end_dt - timedelta(hours=24)))

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _name, polygon, _is_custom = _resolve_region(cur, region)
            if not polygon:
                raise HTTPException(404, f"Unknown region '{region}'")
            rows = _global_rows_for_polygon(
                cur,
                polygon,
                start_dt=start_dt,
                end_dt=end_dt,
            )

    by_ts = {}
    for row in rows:
        by_ts.setdefault(row["captured_at"], []).append({
            "lat": row["lat"],
            "lon": row["lon"],
            "type": row["ship_type"],
            "motion": row["motion"],
            "tile_id": row["tile_id"],
        })

    timestamps = sorted(by_ts)
    if len(timestamps) > max_frames:
        step = len(timestamps) / max_frames
        timestamps = [timestamps[int(i * step)] for i in range(max_frames)]

    frames = []
    for ts in timestamps:
        markers = dedup_markers_spatial(by_ts[ts], VESSEL_DEDUP_EPS_DEG)
        counts = count_markers_by_type(markers)
        frames.append({
            "timestamp": ts.isoformat(),
            "markers": markers,
            "tankers": counts["stationary_tankers"] + counts["moving_tankers"],
            "cargos": counts["stationary_cargos"] + counts["moving_cargos"],
            "moving_tankers": counts["moving_tankers"],
            "moving_cargos": counts["moving_cargos"],
        })

    return {"region": region, "frames": frames}


@app.get("/api/vessels_legacy/history", include_in_schema=False)
def get_vessels_history(
    region: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_frames: int = Query(100, ge=1, le=500),
):
    now = datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
    start_dt = (datetime.fromisoformat(start.replace("Z", "+00:00")) if start
                else (end_dt - timedelta(hours=24)))

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT captured_at, markers,
                       tankers, cargos, moving_tankers, moving_cargos
                FROM captures
                WHERE region = %s
                      AND captured_at >= %s
                      AND captured_at <= %s
                ORDER BY captured_at
            """, (region, start_dt, end_dt))
            rows = cur.fetchall()

    # Downsample if too many frames
    if len(rows) > max_frames:
        step = len(rows) / max_frames
        rows = [rows[int(i * step)] for i in range(max_frames)]

    frames = []
    for row in rows:
        markers = row["markers"] if isinstance(row["markers"], list) else json.loads(row["markers"])
        markers = dedup_markers_spatial(markers, VESSEL_DEDUP_EPS_DEG)
        counts = count_markers_by_type(markers)
        frames.append({
            "timestamp": row["captured_at"].isoformat(),
            "markers": markers,
            "tankers": counts["stationary_tankers"] + counts["moving_tankers"],
            "cargos": counts["stationary_cargos"] + counts["moving_cargos"],
            "moving_tankers": counts["moving_tankers"],
            "moving_cargos": counts["moving_cargos"],
        })

    return {"region": region, "frames": frames}


@app.get("/api/vessels/export")
def export_vessel_positions_global(
    region: str,
    start: str,
    end: str,
    type: Optional[str] = Query(None, pattern="^(tanker|cargo)$"),
    motion: Optional[str] = Query(None, pattern="^(moving|stationary)$"),
):
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "start/end must be ISO datetimes")
    if start_dt >= end_dt:
        raise HTTPException(400, "end must be after start")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _name, polygon, _is_custom = _resolve_region(cur, region)
            if not polygon:
                raise HTTPException(404, f"Unknown region '{region}'")
            rows = _global_rows_for_polygon(
                cur,
                polygon,
                start_dt=start_dt,
                end_dt=end_dt,
                type_filter=type,
                motion_filter=motion,
            )

    def stream():
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "region", "lat", "lon", "ship_type", "motion", "tile_id"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate()

        for row in rows:
            w.writerow([
                row["captured_at"].isoformat(),
                region,
                row["lat"],
                row["lon"],
                row["ship_type"],
                row["motion"],
                row["tile_id"],
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate()

    fname = f"{region}_positions_{start_dt.date()}_{end_dt.date()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/vessels_legacy/export", include_in_schema=False)
def export_vessel_positions(
    region: str,
    start: str,
    end: str,
    type: Optional[str] = Query(None, pattern="^(tanker|cargo)$"),
    motion: Optional[str] = Query(None, pattern="^(moving|stationary)$"),
):
    """Stream every vessel position in [start, end) for a region as CSV."""
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "start/end must be ISO datetimes")
    if start_dt >= end_dt:
        raise HTTPException(400, "end must be after start")

    # Resolve region: predefined or custom
    polygon = None
    if region not in REGIONS:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT polygon FROM custom_regions WHERE code = %s",
                            (region,))
                cr = cur.fetchone()
                if not cr:
                    raise HTTPException(404, f"Unknown region '{region}'")
                polygon = cr["polygon"]

    # Build SQL. Predefined: filter by captures.region. Custom: bbox prefilter
    # on vessel_positions, then point-in-polygon in Python.
    if polygon is None:
        sql = """
            SELECT c.captured_at, vp.lat, vp.lon, vp.ship_type, vp.motion
            FROM vessel_positions vp
            JOIN captures c ON c.id = vp.capture_id
            WHERE c.region = %s
              AND c.captured_at >= %s
              AND c.captured_at <  %s
        """
        args = [region, start_dt, end_dt]
    else:
        bbox = _polygon_bbox(polygon)  # [min_lon, min_lat, max_lon, max_lat]
        sql = """
            SELECT c.captured_at, vp.lat, vp.lon, vp.ship_type, vp.motion
            FROM vessel_positions vp
            JOIN captures c ON c.id = vp.capture_id
            WHERE c.captured_at >= %s
              AND c.captured_at <  %s
              AND vp.lat BETWEEN %s AND %s
              AND vp.lon BETWEEN %s AND %s
        """
        args = [start_dt, end_dt, bbox[1], bbox[3], bbox[0], bbox[2]]
    if type:
        sql += " AND vp.ship_type = %s"
        args.append(type)
    if motion:
        sql += " AND vp.motion = %s"
        args.append(motion)
    sql += " ORDER BY c.captured_at, vp.id"

    def stream():
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "region", "lat", "lon", "ship_type", "motion"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate()

        with get_conn() as conn:
            with conn.cursor(name="export_cur",
                             cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.itersize = 5000
                cur.execute(sql, args)
                for row in cur:
                    if polygon is not None and not _point_in_polygon(
                        row["lat"], row["lon"], polygon
                    ):
                        continue
                    w.writerow([
                        row["captured_at"].isoformat(),
                        region,
                        row["lat"], row["lon"],
                        row["ship_type"], row["motion"],
                    ])
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate()

    fname = f"{region}_positions_{start_dt.date()}_{end_dt.date()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


_tiles_geojson_cache = None


def _build_tiles_geojson():
    """Return the global capture tile manifest as GeoJSON."""
    features = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT tile_id, zoom, row, col, enabled, schedule_minutes,
                       priority, source, seed_regions, center_lat, center_lon,
                       tile_bounds, capture_bounds, owner_bounds_px,
                       capture_bounds_px
                FROM capture_tiles
                ORDER BY zoom, row, col
            """)
            rows = cur.fetchall()

    if rows:
        tiles = []
        for row in rows:
            tile = dict(row)
            for key in ("seed_regions", "tile_bounds", "capture_bounds",
                        "owner_bounds_px", "capture_bounds_px"):
                if isinstance(tile.get(key), str):
                    tile[key] = json.loads(tile[key])
            tiles.append(tile)
    else:
        tiles = build_global_tile_manifest(
            VIEWPORT_WIDTH,
            VIEWPORT_HEIGHT,
            global_bbox=GLOBAL_GRID_BBOX,
            default_zoom=GLOBAL_GRID_DEFAULT_ZOOM,
            schedule_minutes=SCRAPE_INTERVAL_MINUTES,
        )

    for tile in tiles:
        features.append(tile_to_geojson_feature(tile))
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/tiles")
def get_tiles():
    """Tile-grid coverage as GeoJSON for the predefined regions."""
    global _tiles_geojson_cache
    if _tiles_geojson_cache is None:
        _tiles_geojson_cache = _build_tiles_geojson()
    return _tiles_geojson_cache


@app.get("/api/timeline")
def get_timeline():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT MIN(captured_at) AS earliest,
                       MAX(captured_at) AS latest,
                       COUNT(*) AS total_captures
                FROM tile_captures
            """)
            row = cur.fetchone()

            cur.execute("""
                SELECT DISTINCT captured_at
                FROM tile_captures
                ORDER BY captured_at DESC
                LIMIT 200
            """)
            snapshots = [r["captured_at"].isoformat() for r in cur.fetchall()]

    return {
        "earliest": row["earliest"].isoformat() if row["earliest"] else None,
        "latest": row["latest"].isoformat() if row["latest"] else None,
        "total_captures": row["total_captures"],
        "snapshots": snapshots,
    }


# --- Custom Regions CRUD ---------------------------------------------------

@app.post("/api/regions/custom", status_code=201)
def create_custom_region(body: CustomRegionCreate):
    if body.code in REGIONS:
        raise HTTPException(400, f"Code '{body.code}' conflicts with predefined region")
    if len(body.polygon) < 3:
        raise HTTPException(400, "Polygon must have at least 3 points")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    INSERT INTO custom_regions (code, name, description, polygon, zoom,
                                                high_threshold, low_threshold)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (
                    body.code, body.name, body.description,
                    json.dumps(body.polygon), body.zoom,
                    body.high_threshold, body.low_threshold,
                ))
                row = cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                raise HTTPException(409, f"Region code '{body.code}' already exists")

    row["created_at"] = row["created_at"].isoformat()
    row["updated_at"] = row["updated_at"].isoformat()
    return row


@app.put("/api/regions/custom/{region_id}")
def update_custom_region(region_id: int, body: CustomRegionUpdate):
    sets = []
    vals = []
    for field in ["name", "description", "zoom", "high_threshold", "low_threshold"]:
        v = getattr(body, field)
        if v is not None:
            sets.append(f"{field} = %s")
            vals.append(v)
    if body.polygon is not None:
        sets.append("polygon = %s")
        vals.append(json.dumps(body.polygon))
    if not sets:
        raise HTTPException(400, "No fields to update")
    sets.append("updated_at = NOW()")
    vals.append(region_id)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE custom_regions SET {', '.join(sets)} WHERE id = %s RETURNING *",
                vals,
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Custom region not found")

    row["created_at"] = row["created_at"].isoformat()
    row["updated_at"] = row["updated_at"].isoformat()
    return row


@app.delete("/api/regions/custom/{region_id}")
def delete_custom_region(region_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM custom_regions WHERE id = %s RETURNING code", (region_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Custom region not found")
            # Also delete associated alerts
            cur.execute("DELETE FROM alerts WHERE region_code = %s", (row[0],))
    return {"deleted": True}


# --- Alerts -----------------------------------------------------------------

@app.get("/api/alerts/{region_code}")
def get_alerts(
    region_code: str,
    limit: int = Query(50, ge=1, le=200),
    unacked_only: bool = False,
):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = "WHERE region_code = %s"
            params = [region_code]
            if unacked_only:
                where += " AND acknowledged_at IS NULL"
            cur.execute(f"""
                SELECT * FROM alerts
                {where}
                ORDER BY created_at DESC
                LIMIT %s
            """, params + [limit])
            rows = cur.fetchall()

    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        r["acknowledged_at"] = r["acknowledged_at"].isoformat() if r["acknowledged_at"] else None
    return {"alerts": rows}


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE alerts SET acknowledged_at = NOW()
                WHERE id = %s AND acknowledged_at IS NULL
                RETURNING *
            """, (alert_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Alert not found or already acknowledged")

    row["created_at"] = row["created_at"].isoformat()
    row["acknowledged_at"] = row["acknowledged_at"].isoformat()
    return row


# --- Static files -----------------------------------------------------------

dashboard_dir = Path(__file__).parent / "dashboard"


@app.get("/")
def serve_dashboard():
    index = dashboard_dir / "index.html"
    if not index.exists():
        raise HTTPException(404, "Dashboard not found. Create dashboard/index.html")
    return FileResponse(index, media_type="text/html")


if dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir)), name="dashboard")
