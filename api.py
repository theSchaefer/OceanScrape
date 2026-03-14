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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from regions import REGIONS

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "")
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))

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
            cur.execute(_EXTRA_SCHEMA)


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
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    return {"mapbox_token": MAPBOX_TOKEN}


@app.get("/api/status")
def get_status():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT MAX(captured_at) AS last_scrape FROM captures")
            row = cur.fetchone()
            last_scrape = row["last_scrape"] if row else None

            cur.execute("SELECT COUNT(*) AS total FROM captures")
            total_captures = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) AS total FROM vessel_positions")
            total_vessels = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(DISTINCT region) AS cnt FROM captures")
            regions_active = cur.fetchone()["cnt"]

            last_run_stats = None
            if last_scrape:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE status = 'success') AS regions_ok,
                           COUNT(*) FILTER (WHERE status != 'success') AS regions_failed,
                           SUM(tankers + cargos + moving_tankers + moving_cargos) AS total_ships
                    FROM captures
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
        "total_vessels": total_vessels,
        "regions_active": regions_active,
        "last_run_stats": last_run_stats,
    }


@app.get("/api/regions")
def get_regions():
    regions_out = []

    # Predefined regions
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Latest capture per region
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

    for cr in custom_rows:
        polygon = cr["polygon"]  # already JSONB list
        area = _polygon_area_km2(polygon)
        latest = latest_map.get(cr["code"])
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
    end_dt = datetime.fromisoformat(end) if end else now
    start_dt = datetime.fromisoformat(start) if start else (end_dt - timedelta(days=7))

    # Look up region polygon for density
    region_def = REGIONS.get(code)
    area = 0.0
    region_name = code
    if region_def:
        area = _polygon_area_km2(region_def["polygon"])
        region_name = region_def["name"]
    else:
        # Check custom regions
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name, polygon FROM custom_regions WHERE code = %s", (code,))
                cr = cur.fetchone()
                if cr:
                    area = _polygon_area_km2(cr["polygon"])
                    region_name = cr["name"]

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT date_trunc(%s, captured_at) AS bucket,
                       AVG(tankers)::int AS tankers,
                       AVG(cargos)::int AS cargos,
                       AVG(moving_tankers)::int AS moving_tankers,
                       AVG(moving_cargos)::int AS moving_cargos,
                       AVG(tankers + cargos + moving_tankers + moving_cargos)::int AS total_ships,
                       MAX(tankers + cargos + moving_tankers + moving_cargos) AS max_ships,
                       MIN(tankers + cargos + moving_tankers + moving_cargos) AS min_ships,
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


@app.get("/api/vessels")
def get_vessels(
    region: Optional[str] = None,
    type: Optional[str] = Query(None, pattern="^(tanker|cargo)$"),
    motion: Optional[str] = Query(None, pattern="^(moving|stationary)$"),
    timestamp: Optional[str] = None,
):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if timestamp:
                ts = datetime.fromisoformat(timestamp)
                if region:
                    cur.execute("""
                        SELECT id, region, captured_at, markers
                        FROM captures
                        WHERE region = %s
                        ORDER BY ABS(EXTRACT(EPOCH FROM captured_at - %s))
                        LIMIT 1
                    """, (region, ts))
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (region) id, region, captured_at, markers
                        FROM captures
                        WHERE captured_at BETWEEN %s - INTERVAL '2 hours'
                              AND %s + INTERVAL '2 hours'
                        ORDER BY region, ABS(EXTRACT(EPOCH FROM captured_at - %s))
                    """, (ts, ts, ts))
            else:
                if region:
                    cur.execute("""
                        SELECT id, region, captured_at, markers
                        FROM captures
                        WHERE region = %s
                        ORDER BY captured_at DESC
                        LIMIT 1
                    """, (region,))
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (region) id, region, captured_at, markers
                        FROM captures
                        ORDER BY region, captured_at DESC
                    """)

            rows = cur.fetchall()

    vessels = []
    effective_ts = None
    for row in rows:
        if effective_ts is None:
            effective_ts = row["captured_at"]
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

    return {
        "timestamp": effective_ts.isoformat() if effective_ts else None,
        "vessels": vessels,
        "count": len(vessels),
    }


@app.get("/api/vessels/history")
def get_vessels_history(
    region: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_frames: int = Query(100, ge=1, le=500),
):
    now = datetime.now(timezone.utc)
    end_dt = datetime.fromisoformat(end) if end else now
    start_dt = datetime.fromisoformat(start) if start else (end_dt - timedelta(hours=24))

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
        frames.append({
            "timestamp": row["captured_at"].isoformat(),
            "markers": markers,
            "tankers": row["tankers"],
            "cargos": row["cargos"],
            "moving_tankers": row["moving_tankers"],
            "moving_cargos": row["moving_cargos"],
        })

    return {"region": region, "frames": frames}


@app.get("/api/timeline")
def get_timeline():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT MIN(captured_at) AS earliest,
                       MAX(captured_at) AS latest,
                       COUNT(*) AS total_captures
                FROM captures
            """)
            row = cur.fetchone()

            cur.execute("""
                SELECT DISTINCT captured_at
                FROM captures
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
