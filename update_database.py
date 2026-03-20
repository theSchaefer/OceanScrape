#!/bin/python3
"""PostgreSQL database layer for MarineScraper captures.

Reads capture entries from the JSON log produced by scraper_global.py and
batch-inserts them into a PostgreSQL ``captures`` table.  Nested marker and
detection data are stored as JSONB columns.

Exported API (consumed by run.py):
    process_log(log_path=None)  — ingest ./data/captures_log.json → PostgreSQL
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

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

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

DEFAULT_LOG_PATH = Path("./data") / "captures_log.json"


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


def _parse_datetime(date_str):
    """Convert the scraper's 'YYYY-MM-DD-HH-MM-SS' format to a UTC datetime."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d-%H-%M-%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        logger.warning("Could not parse date_time '%s', using current UTC time", date_str)
        return datetime.now(timezone.utc)


def _entry_to_row(entry):
    """Convert a capture dict to a tuple matching _INSERT_SQL column order."""
    region = entry.get("region", "")
    if not region and entry.get("is_north"):
        region = "N"
    if not region:
        region = "?"

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
        int(entry.get("tankers", 0)),
        int(entry.get("cargos", 0)),
        int(entry.get("moving_tankers", 0)),
        int(entry.get("moving_cargos", 0)),
        psycopg2.extras.Json(entry.get("markers", [])),
        psycopg2.extras.Json(entry.get("detections", [])),
    )


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
    conn = _get_connection()
    try:
        _ensure_schema(conn)
        row = _entry_to_row(data)
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL + " RETURNING id", row)
            result = cur.fetchone()
            row_id = result[0] if result else None
            if row_id:
                logger.info("Inserted capture id=%d for region=%s",
                            row_id, data.get("region", "?"))
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


def process_log(log_path=None):
    """Read captures_log.json, batch-insert all entries, then archive the log.

    After a successful insert the log file is renamed to
    ``captures_log_YYYYMMDD_HHMMSS.json`` in the same directory and a
    fresh empty ``[]`` is written in its place so the scraper can
    continue appending.
    """
    log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH

    if not log_path.exists():
        logger.info("No captures log found at %s — nothing to process", log_path)
        return

    with open(log_path, "r") as f:
        try:
            entries = json.load(f)
        except json.JSONDecodeError:
            logger.error("Malformed JSON in %s — skipping", log_path)
            return

    if not entries:
        logger.info("Captures log is empty — nothing to process")
        return

    conn = _get_connection()
    try:
        _ensure_schema(conn)
        inserted = 0
        skipped = 0
        with conn.cursor() as cur:
            for entry in entries:
                region = entry.get("region", "?")
                row = _entry_to_row(entry)
                logger.debug("Inserting capture: region=%s status=%s tankers=%d cargos=%d",
                             region, entry.get("status"), entry.get("tankers", 0),
                             entry.get("cargos", 0))
                cur.execute(_INSERT_SQL + " RETURNING id", row)
                result = cur.fetchone()
                if result:
                    inserted += 1
                    logger.info("Inserted capture id=%d region=%s",
                                result[0], region)
                else:
                    skipped += 1
                    logger.debug("Skipped duplicate: region=%s ts=%s",
                                 region, entry.get("date_time"))
        conn.commit()
        logger.info(
            "Processed %d entries: %d inserted, %d duplicates skipped",
            len(entries), inserted, skipped,
        )
    except Exception:
        conn.rollback()
        logger.exception("Failed to process captures log — log file preserved for retry")
        raise
    finally:
        conn.close()

    # Archive the processed log
    archive_name = "captures_log_{}.json".format(
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    archive_path = log_path.parent / archive_name
    shutil.move(str(log_path), str(archive_path))
    logger.info("Archived captures log to %s", archive_path)

    # Reset the log file for the next scrape run
    with open(log_path, "w") as f:
        json.dump([], f)
