#!/bin/python3
"""PostgreSQL database layer for MarineScraper captures.

Reads capture entries from the JSON log produced by scraper_global.py and
batch-inserts them into a PostgreSQL ``captures`` table.  Nested marker and
detection data are stored as JSONB columns.

Exported API (consumed by run.py):
    process_log(log_path=None)  — ingest ./data/captures_log.jsonl → PostgreSQL
    insert_capture(data)        — insert a single capture record
"""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from global_tile_grid import build_global_tile_manifest, parse_global_bbox
from marker_dedup import count_markers_by_type, dedup_markers_spatial

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
MARKER_DEDUP_EPS_DEG = float(os.getenv(
    "MARKER_DEDUP_EPS_DEG",
    os.getenv("VESSEL_DEDUP_EPS_DEG", "0.003"),
))
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "7680"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "4320"))
GLOBAL_GRID_DEFAULT_ZOOM = int(os.getenv("GLOBAL_GRID_DEFAULT_ZOOM", "9"))

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS captures (
    id              BIGSERIAL PRIMARY KEY,

    -- Region identification
    region          VARCHAR(8)   NOT NULL,
    region_name     VARCHAR(128),

    -- Temporal
    captured_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    ingested_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Capture metadata
    filepath        TEXT         NOT NULL DEFAULT '',
    is_north        BOOLEAN      NOT NULL DEFAULT FALSE,
    zoom            SMALLINT,
    status          VARCHAR(16)  NOT NULL DEFAULT 'success',
    file_size_kb    REAL         NOT NULL DEFAULT 0.0,

    -- Tile stats
    tiles_total     INTEGER      NOT NULL DEFAULT 0,
    tiles_ok        INTEGER      NOT NULL DEFAULT 0,
    tiles_failed    INTEGER      NOT NULL DEFAULT 0,

    -- Ship counts
    tankers         INTEGER      NOT NULL DEFAULT 0,
    cargos          INTEGER      NOT NULL DEFAULT 0,
    moving_tankers  INTEGER      NOT NULL DEFAULT 0,
    moving_cargos   INTEGER      NOT NULL DEFAULT 0,

    -- Detailed detection data
    markers         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    detections      JSONB        NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT uq_region_timestamp UNIQUE (region, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_captures_captured_at
    ON captures (captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_captures_region
    ON captures (region);

CREATE INDEX IF NOT EXISTS idx_captures_region_time
    ON captures (region, captured_at DESC);

CREATE TABLE IF NOT EXISTS vessel_positions (
    id          BIGSERIAL PRIMARY KEY,
    capture_id  BIGINT REFERENCES captures(id) ON DELETE CASCADE,

    -- Position (from pixel-based detection, converted via Web Mercator)
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,

    -- Classification
    ship_type   VARCHAR(16)  NOT NULL,   -- 'tanker' or 'cargo'
    motion      VARCHAR(16)  NOT NULL,   -- 'stationary' or 'moving'

    CONSTRAINT uq_marker_capture UNIQUE (capture_id, lat, lon)
);

CREATE INDEX IF NOT EXISTS idx_vp_capture ON vessel_positions (capture_id);

CREATE TABLE IF NOT EXISTS capture_tiles (
    tile_id             TEXT PRIMARY KEY,
    zoom                SMALLINT NOT NULL,
    row                 INTEGER NOT NULL,
    col                 INTEGER NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_minutes    INTEGER NOT NULL DEFAULT 60,
    priority            SMALLINT NOT NULL DEFAULT 0,
    source              TEXT NOT NULL DEFAULT 'global_default',
    seed_regions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    center_lat          DOUBLE PRECISION NOT NULL,
    center_lon          DOUBLE PRECISION NOT NULL,
    tile_bounds         JSONB NOT NULL,
    capture_bounds      JSONB NOT NULL,
    owner_bounds_px     JSONB NOT NULL,
    capture_bounds_px   JSONB NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_capture_tiles_zoom
    ON capture_tiles (zoom, row, col);

CREATE INDEX IF NOT EXISTS idx_capture_tiles_enabled
    ON capture_tiles (enabled);

CREATE TABLE IF NOT EXISTS tile_captures (
    id                  BIGSERIAL PRIMARY KEY,
    tile_id             TEXT NOT NULL REFERENCES capture_tiles(tile_id) ON DELETE RESTRICT,
    captured_at         TIMESTAMPTZ NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filepath            TEXT NOT NULL DEFAULT '',
    zoom                SMALLINT NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'success',
    file_size_kb        REAL NOT NULL DEFAULT 0.0,
    tankers             INTEGER NOT NULL DEFAULT 0,
    cargos              INTEGER NOT NULL DEFAULT 0,
    moving_tankers      INTEGER NOT NULL DEFAULT 0,
    moving_cargos       INTEGER NOT NULL DEFAULT 0,
    markers             JSONB NOT NULL DEFAULT '[]'::jsonb,
    detections          JSONB NOT NULL DEFAULT '[]'::jsonb,
    nav_mode            TEXT,
    projection_mode     TEXT,
    qa_flags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    qa_confidence       REAL,
    tile_bounds         JSONB NOT NULL DEFAULT '{}'::jsonb,
    capture_bounds      JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_tile_capture_timestamp UNIQUE (tile_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_tile_captures_time
    ON tile_captures (captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_tile_captures_tile_time
    ON tile_captures (tile_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS global_vessel_positions (
    id                  BIGSERIAL PRIMARY KEY,
    tile_capture_id     BIGINT NOT NULL REFERENCES tile_captures(id) ON DELETE CASCADE,
    tile_id             TEXT NOT NULL,
    captured_at         TIMESTAMPTZ NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    ship_type           VARCHAR(16) NOT NULL,
    motion              VARCHAR(16) NOT NULL,
    CONSTRAINT uq_global_marker_capture UNIQUE (
        tile_capture_id, lat, lon, ship_type
    )
);

CREATE INDEX IF NOT EXISTS idx_gvp_time
    ON global_vessel_positions (captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_gvp_tile_time
    ON global_vessel_positions (tile_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_gvp_lat_lon
    ON global_vessel_positions (lat, lon);
"""

_INSERT_SQL = """
INSERT INTO captures (
    region, region_name, captured_at, filepath, is_north,
    zoom, status, file_size_kb,
    tiles_total, tiles_ok, tiles_failed,
    tankers, cargos, moving_tankers, moving_cargos,
    markers, detections
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s
)
ON CONFLICT (region, captured_at) DO NOTHING
"""

_INSERT_MARKER_SQL = """
INSERT INTO vessel_positions (capture_id, lat, lon, ship_type, motion)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (capture_id, lat, lon) DO NOTHING
"""

_UPSERT_CAPTURE_TILE_SQL = """
INSERT INTO capture_tiles (
    tile_id, zoom, row, col, enabled, schedule_minutes, priority,
    source, seed_regions, center_lat, center_lon, tile_bounds,
    capture_bounds, owner_bounds_px, capture_bounds_px, updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, NOW()
)
ON CONFLICT (tile_id) DO UPDATE SET
    zoom = EXCLUDED.zoom,
    row = EXCLUDED.row,
    col = EXCLUDED.col,
    enabled = EXCLUDED.enabled,
    schedule_minutes = EXCLUDED.schedule_minutes,
    priority = EXCLUDED.priority,
    source = EXCLUDED.source,
    seed_regions = EXCLUDED.seed_regions,
    center_lat = EXCLUDED.center_lat,
    center_lon = EXCLUDED.center_lon,
    tile_bounds = EXCLUDED.tile_bounds,
    capture_bounds = EXCLUDED.capture_bounds,
    owner_bounds_px = EXCLUDED.owner_bounds_px,
    capture_bounds_px = EXCLUDED.capture_bounds_px,
    updated_at = NOW()
"""

_INSERT_TILE_CAPTURE_SQL = """
INSERT INTO tile_captures (
    tile_id, captured_at, filepath, zoom, status, file_size_kb,
    tankers, cargos, moving_tankers, moving_cargos, markers,
    detections, nav_mode, projection_mode, qa_flags, qa_confidence,
    tile_bounds, capture_bounds
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s
)
ON CONFLICT (tile_id, captured_at) DO NOTHING
"""

_INSERT_GLOBAL_MARKER_SQL = """
INSERT INTO global_vessel_positions (
    tile_capture_id, tile_id, captured_at, lat, lon, ship_type, motion
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tile_capture_id, lat, lon, ship_type) DO NOTHING
"""

DEFAULT_LOG_PATH = Path("./data") / "captures_log.jsonl"
_LEGACY_LOG_PATH = Path("./data") / "captures_log.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_connection():
    """Open a new PostgreSQL connection using DATABASE_URL."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is required. "
            "Example: postgresql://user:pass@localhost:5432/marinescraper"
        )
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def _ensure_schema(conn):
    """Create the captures table and indexes if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()


def _tile_to_row(tile):
    return (
        tile["tile_id"],
        int(tile["zoom"]),
        int(tile["row"]),
        int(tile["col"]),
        bool(tile.get("enabled", True)),
        int(tile.get("schedule_minutes", 60)),
        int(tile.get("priority", 0)),
        tile.get("source", "global_default"),
        psycopg2.extras.Json(tile.get("seed_regions", [])),
        float(tile["center_lat"]),
        float(tile["center_lon"]),
        psycopg2.extras.Json(tile["tile_bounds"]),
        psycopg2.extras.Json(tile["capture_bounds"]),
        psycopg2.extras.Json(tile["owner_bounds_px"]),
        psycopg2.extras.Json(tile["capture_bounds_px"]),
    )


def _sync_capture_tile_manifest(cur, tiles=None):
    """Upsert the deterministic global capture manifest."""
    manifest = tiles or build_global_tile_manifest(
        VIEWPORT_WIDTH,
        VIEWPORT_HEIGHT,
        global_bbox=parse_global_bbox(),
        default_zoom=GLOBAL_GRID_DEFAULT_ZOOM,
        schedule_minutes=int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60")),
    )
    for tile in manifest:
        cur.execute(_UPSERT_CAPTURE_TILE_SQL, _tile_to_row(tile))
    return len(manifest)


def _parse_datetime(date_str):
    """Convert the scraper's 'YYYY-MM-DD-HH-MM-SS' format to a UTC datetime."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d-%H-%M-%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        logger.warning("Could not parse date_time '%s', using current UTC time", date_str)
        return datetime.now(timezone.utc)


def _dedup_entry_markers(entry):
    """Return capture markers with overlapping-tile duplicates removed."""
    return dedup_markers_spatial(entry.get("markers", []), MARKER_DEDUP_EPS_DEG)


def _entry_counts(entry, markers):
    """Prefer marker-derived counts so stored totals match the marker payload."""
    if markers:
        counts = count_markers_by_type(markers)
        return {
            "tankers": counts["stationary_tankers"] + counts["moving_tankers"],
            "cargos": counts["stationary_cargos"] + counts["moving_cargos"],
            "moving_tankers": counts["moving_tankers"],
            "moving_cargos": counts["moving_cargos"],
        }
    return {
        "tankers": int(entry.get("tankers", 0)),
        "cargos": int(entry.get("cargos", 0)),
        "moving_tankers": int(entry.get("moving_tankers", 0)),
        "moving_cargos": int(entry.get("moving_cargos", 0)),
    }


def _entry_to_row(entry):
    """Convert a capture dict to a tuple matching _INSERT_SQL column order."""
    region = entry.get("region", "")
    if not region and entry.get("is_north"):
        region = "N"
    if not region:
        region = "?"

    markers = _dedup_entry_markers(entry)
    counts = _entry_counts(entry, markers)

    return (
        region,
        entry.get("region_name", ""),
        _parse_datetime(entry.get("date_time", "")),
        entry.get("filepath", ""),
        bool(entry.get("is_north", False)),
        entry.get("zoom"),
        entry.get("status", "success"),
        float(entry.get("file_size_kb", 0)),
        int(entry.get("tiles_total", 0)),
        int(entry.get("tiles_ok", 0)),
        int(entry.get("tiles_failed", 0)),
        counts["tankers"],
        counts["cargos"],
        counts["moving_tankers"],
        counts["moving_cargos"],
        psycopg2.extras.Json(markers),
        psycopg2.extras.Json(entry.get("detections", [])),
    )


def _entry_status(entry):
    status = entry.get("status")
    if status:
        return status
    ok = int(entry.get("tiles_ok", 0))
    failed = int(entry.get("tiles_failed", 0))
    total = int(entry.get("tiles_total", 0))
    if total == 0 and ok == 0:
        return "error"
    if failed == 0:
        return "success"
    if ok > 0:
        return "partial"
    return "error"


def _tile_from_entry(entry):
    tile = entry.get("tile") or {}
    tile_id = entry.get("tile_id") or tile.get("tile_id")
    if not tile_id:
        raise ValueError("tile capture entry is missing tile_id")
    return {
        "tile_id": tile_id,
        "zoom": int(entry.get("zoom") or tile.get("zoom") or entry.get("zoom_used")),
        "row": int(entry.get("row", tile.get("row", 0))),
        "col": int(entry.get("col", tile.get("col", 0))),
        "enabled": bool(tile.get("enabled", True)),
        "schedule_minutes": int(tile.get("schedule_minutes", entry.get("schedule_minutes", 60))),
        "priority": int(tile.get("priority", entry.get("priority", 0))),
        "source": tile.get("source", entry.get("source", "global_default")),
        "seed_regions": tile.get("seed_regions", entry.get("seed_regions", [])),
        "center_lat": float(entry.get("center_lat", tile.get("center_lat"))),
        "center_lon": float(entry.get("center_lon", tile.get("center_lon"))),
        "tile_bounds": entry.get("tile_bounds") or tile.get("tile_bounds") or {},
        "capture_bounds": entry.get("capture_bounds") or tile.get("capture_bounds") or {},
        "owner_bounds_px": entry.get("owner_bounds_px") or tile.get("owner_bounds_px") or {},
        "capture_bounds_px": (
            entry.get("capture_bounds_px")
            or tile.get("capture_bounds_px")
            or {}
        ),
    }


def _tile_entry_to_row(entry, markers):
    tile = _tile_from_entry(entry)
    counts = _entry_counts(entry, markers)
    captured_at = _parse_datetime(entry.get("date_time", ""))
    return (
        tile["tile_id"],
        captured_at,
        entry.get("filepath", ""),
        int(tile["zoom"]),
        _entry_status(entry),
        float(entry.get("file_size_kb", 0)),
        counts["tankers"],
        counts["cargos"],
        counts["moving_tankers"],
        counts["moving_cargos"],
        psycopg2.extras.Json(markers),
        psycopg2.extras.Json(entry.get("detections", [])),
        entry.get("nav_mode"),
        entry.get("projection_mode"),
        psycopg2.extras.Json(entry.get("qa_flags", [])),
        entry.get("qa_confidence"),
        psycopg2.extras.Json(tile["tile_bounds"]),
        psycopg2.extras.Json(tile["capture_bounds"]),
    )


def _upsert_entry_tile(cur, entry):
    tile = _tile_from_entry(entry)
    cur.execute(_UPSERT_CAPTURE_TILE_SQL, _tile_to_row(tile))
    return tile


def _insert_markers(cur, capture_id, markers):
    """Batch-insert detected marker positions for a given capture."""
    if not markers:
        return 0
    inserted = 0
    for m in dedup_markers_spatial(markers, MARKER_DEDUP_EPS_DEG):
        lat = m.get("lat")
        lon = m.get("lon")
        if lat is None or lon is None:
            continue
        cur.execute(_INSERT_MARKER_SQL, (
            capture_id,
            float(lat),
            float(lon),
            m.get("type", "unknown"),
            m.get("motion", "unknown"),
        ))
        inserted += cur.rowcount
    return inserted


def _insert_global_markers(cur, tile_capture_id, tile_id, captured_at, markers):
    """Batch-insert globally accepted marker positions for a tile capture."""
    if not markers:
        return 0
    inserted = 0
    for m in dedup_markers_spatial(markers, MARKER_DEDUP_EPS_DEG):
        lat = m.get("lat")
        lon = m.get("lon")
        if lat is None or lon is None:
            continue
        cur.execute(_INSERT_GLOBAL_MARKER_SQL, (
            tile_capture_id,
            tile_id,
            captured_at,
            float(lat),
            float(lon),
            m.get("type", "unknown"),
            m.get("motion", "unknown"),
        ))
        inserted += cur.rowcount
    return inserted


def insert_tile_capture(data):
    """Insert a single global tile capture into PostgreSQL."""
    conn = _get_connection()
    try:
        _ensure_schema(conn)
        markers = _dedup_entry_markers(data)
        row = _tile_entry_to_row(data, markers)
        captured_at = row[1]
        tile_id = row[0]
        with conn.cursor() as cur:
            _upsert_entry_tile(cur, data)
            cur.execute(_INSERT_TILE_CAPTURE_SQL + " RETURNING id", row)
            result = cur.fetchone()
            row_id = result[0] if result else None
            if row_id:
                m_count = _insert_global_markers(
                    cur, row_id, tile_id, captured_at, markers
                )
                logger.info("Inserted tile_capture id=%d tile=%s (%d markers)",
                            row_id, tile_id, m_count)
            else:
                logger.info("Skipped duplicate tile_capture tile=%s", tile_id)
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        logger.exception("Failed to insert tile capture")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def insert_capture(data):
    """Insert a single capture record into PostgreSQL.

    Accepts either a full entry dict (from _log_json) or a partial dict
    (from the legacy seer.py pipeline in run.py).  Missing keys default
    to safe zero/empty values.

    Returns the inserted row id, or None if skipped due to conflict.
    """
    if data.get("tile_id") or data.get("capture_type") == "tile":
        return insert_tile_capture(data)

    conn = _get_connection()
    try:
        _ensure_schema(conn)
        row = _entry_to_row(data)
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL + " RETURNING id", row)
            result = cur.fetchone()
            row_id = result[0] if result else None
            if row_id:
                m_count = _insert_markers(cur, row_id, data.get("markers", []))
                logger.info("Inserted capture id=%d for region=%s (%d markers)",
                            row_id, data.get("region", "?"), m_count)
            else:
                logger.info("Skipped duplicate capture for region=%s", data.get("region", "?"))
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        logger.exception("Failed to insert capture")
        raise
    finally:
        conn.close()


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file (one JSON object per line)."""
    entries = []
    with open(path, "r") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line %d in %s", lineno, path)
    return entries


def get_due_tile_ids(tile_ids=None):
    """Return enabled tile ids due for capture according to capture_tiles."""
    conn = _get_connection()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            _sync_capture_tile_manifest(cur)
            args = []
            where = "WHERE ct.enabled"
            if tile_ids is not None:
                ids = list(tile_ids)
                if not ids:
                    return set()
                where += " AND ct.tile_id = ANY(%s)"
                args.append(ids)
            cur.execute(f"""
                SELECT ct.tile_id
                FROM capture_tiles ct
                LEFT JOIN (
                    SELECT tile_id, MAX(captured_at) AS last_captured_at
                    FROM tile_captures
                    GROUP BY tile_id
                ) latest ON latest.tile_id = ct.tile_id
                {where}
                  AND (
                    latest.last_captured_at IS NULL
                    OR latest.last_captured_at <=
                       NOW() - (ct.schedule_minutes || ' minutes')::interval
                  )
                ORDER BY ct.zoom, ct.row, ct.col
            """, args)
            due = {row[0] for row in cur.fetchall()}
        conn.commit()
        return due
    except Exception:
        conn.rollback()
        logger.exception("Failed to query due capture tiles")
        raise
    finally:
        conn.close()


def process_log(log_path=None):
    """Read captures_log.jsonl, batch-insert all entries, then archive the log.

    After a successful insert the log file is renamed to
    ``captures_log_YYYYMMDD_HHMMSS.jsonl`` in the same directory and a
    fresh empty file is written in its place so the scraper can
    continue appending.

    Falls back to the legacy ``captures_log.json`` format if the JSONL
    file does not exist.
    """
    log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
    is_legacy = False

    if not log_path.exists() and _LEGACY_LOG_PATH.exists():
        log_path = _LEGACY_LOG_PATH
        is_legacy = True
        logger.info("Using legacy JSON log at %s", log_path)

    if not log_path.exists():
        logger.info("No captures log found at %s — nothing to process", log_path)
        return

    if is_legacy:
        with open(log_path, "r") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                logger.error("Malformed JSON in %s — skipping", log_path)
                return
    else:
        entries = _read_jsonl(log_path)

    if not entries:
        logger.info("Captures log is empty — nothing to process")
        return

    conn = _get_connection()
    try:
        _ensure_schema(conn)
        inserted = 0
        skipped = 0
        total_markers = 0
        with conn.cursor() as cur:
            manifest_count = _sync_capture_tile_manifest(cur)
            logger.info("Synced %d global capture tiles", manifest_count)
            for entry in entries:
                if entry.get("tile_id") or entry.get("capture_type") == "tile":
                    markers = _dedup_entry_markers(entry)
                    row = _tile_entry_to_row(entry, markers)
                    tile_id = row[0]
                    captured_at = row[1]
                    _upsert_entry_tile(cur, entry)
                    logger.debug(
                        "Inserting tile capture: tile=%s status=%s markers=%d",
                        tile_id, _entry_status(entry), len(markers),
                    )
                    cur.execute(_INSERT_TILE_CAPTURE_SQL + " RETURNING id", row)
                    result = cur.fetchone()
                    if result:
                        capture_id = result[0]
                        inserted += 1
                        m_count = _insert_global_markers(
                            cur, capture_id, tile_id, captured_at, markers
                        )
                        total_markers += m_count
                        logger.info(
                            "Inserted tile_capture id=%d tile=%s (%d markers)",
                            capture_id, tile_id, m_count,
                        )
                    else:
                        skipped += 1
                        logger.debug(
                            "Skipped duplicate tile capture: tile=%s ts=%s",
                            tile_id, entry.get("date_time"),
                        )
                    continue

                region = entry.get("region", "?")
                row = _entry_to_row(entry)
                logger.debug("Inserting capture: region=%s status=%s tankers=%d cargos=%d",
                             region, entry.get("status"), entry.get("tankers", 0),
                             entry.get("cargos", 0))
                cur.execute(_INSERT_SQL + " RETURNING id", row)
                result = cur.fetchone()
                if result:
                    capture_id = result[0]
                    inserted += 1
                    m_count = _insert_markers(cur, capture_id, entry.get("markers", []))
                    total_markers += m_count
                    logger.info("Inserted capture id=%d region=%s (%d markers)",
                                capture_id, region, m_count)
                else:
                    skipped += 1
                    logger.debug("Skipped duplicate: region=%s ts=%s",
                                 region, entry.get("date_time"))
        conn.commit()
        logger.info(
            "Processed %d entries: %d inserted, %d skipped, %d markers",
            len(entries), inserted, skipped, total_markers,
        )
    except Exception:
        conn.rollback()
        logger.exception("Failed to process captures log — log file preserved for retry")
        raise
    finally:
        conn.close()

    # Archive the processed log
    ext = ".json" if is_legacy else ".jsonl"
    archive_name = "captures_log_{}{}".format(
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"), ext
    )
    archive_path = log_path.parent / archive_name
    shutil.move(str(log_path), str(archive_path))
    logger.info("Archived captures log to %s", archive_path)

    # Reset the log file for the next scrape run
    with open(log_path, "w") as f:
        pass  # empty file — scraper appends JSONL lines
