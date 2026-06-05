"""Persistent capture-job queue for the OceanScrape control plane.

The control plane owns this queue; distributed capture workers never touch it
directly — they go through the HTTP control-plane API (``worker_api.py``). The
queue tracks **capture jobs** (a small batch of tile ids to capture) and their
**leases** (which worker currently owns a job, and until when).

Two storage backends share one code path, selected by the DSN scheme:

* ``sqlite:///path/to/file``  — zero-infra default. Perfect for a single
  control-plane node and for tests (``sqlite:///:memory:``).
* ``postgresql://user:pass@host/db`` — reuses the project's existing Postgres
  (``DATABASE_URL``) when you want the queue co-located with the warehouse.

Two queue concepts worth introducing, since they're load-bearing here:

* **Lease / visibility timeout** (the pattern behind SQS, Cloud Tasks, etc.):
  claiming a job doesn't delete it; it marks the row ``leased`` with a
  ``lease_until`` deadline. While leased the job is invisible to other workers.
  If the worker dies, the lease simply expires and the job becomes claimable
  again — no dead jobs, no manual cleanup. Workers extend the deadline with
  periodic *heartbeats* during a long capture.

* **Atomic claim**: two workers must never get the same job. On Postgres we use
  ``SELECT ... FOR UPDATE SKIP LOCKED`` — the row is locked for the duration of
  the claim transaction and concurrent claimers skip locked rows entirely
  (no blocking, no double-hand-out). SQLite has no row locks, so we serialize
  the claim with a single ``BEGIN IMMEDIATE`` write transaction guarded by an
  in-process lock — sufficient because the control plane is one process.

The queue stores only operational metadata. Raw capture data (the JSONL the
worker uploads) is written to artifact files by the API layer; this module just
records the ``artifact_path``. No secrets (worker tokens, proxy creds) are ever
stored or logged here.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

# Job lifecycle states.
PENDING = "pending"        # never handed out, ready to claim
LEASED = "leased"          # claimed by a worker, lease_until in the future
DONE = "done"              # completed, artifact recorded
FAILED = "failed"          # failed permanently (out of attempts / fatal)
RETRYABLE = "retryable"    # failed but eligible to be re-claimed
CLAIMABLE_STATES = (PENDING, RETRYABLE)

DEFAULT_LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "600"))
DEFAULT_MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))

# Columns in the canonical order used for every SELECT, so rows map to dicts by
# position regardless of backend (avoids RETURNING / column-order coupling).
_COLS = (
    "job_id", "batch_id", "ordinal", "zoom", "tile_ids", "status",
    "claimed_by", "lease_until", "attempts", "max_attempts",
    "created_at", "updated_at", "completed_at", "error_summary",
    "artifact_path", "artifact_id", "result_meta", "source", "enqueue_id",
)
_COL_LIST = ", ".join(_COLS)

# Per-dialect physical types. Postgres ``REAL`` is float4 (~7 sig digits) which
# is too coarse for epoch seconds (~1.7e9 loses sub-minute precision), so epoch
# columns must be DOUBLE PRECISION there; SQLite ``REAL`` is already 8-byte.
_TYPES = {
    "postgres": {"text": "TEXT", "int": "INTEGER", "float": "DOUBLE PRECISION"},
    "sqlite": {"text": "TEXT", "int": "INTEGER", "float": "REAL"},
}


def default_dsn() -> str:
    """Resolve the queue DSN from env, defaulting to a local SQLite file.

    Production control planes can set ``WORKER_QUEUE_DSN=$DATABASE_URL`` to keep
    the queue in Postgres alongside the warehouse.
    """
    return os.getenv("WORKER_QUEUE_DSN") or "sqlite:///data/worker_queue.sqlite3"


def _now() -> float:
    return time.time()


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class QueueError(RuntimeError):
    """Raised for invalid queue transitions (e.g. completing a lost lease)."""


class LeaseLost(QueueError):
    """The job is no longer leased by the worker trying to act on it."""


class Queue:
    """A capture-job queue backed by SQLite or Postgres.

    All time-based methods accept an optional ``now`` override (epoch seconds)
    so lease-expiry behaviour can be tested deterministically without sleeping.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or default_dsn()
        self.dialect = self._detect_dialect(self.dsn)
        self._lock = threading.RLock()
        self._sqlite_conn = None  # persistent connection for SQLite backends
        self.ensure_schema()

    # -- backend plumbing ---------------------------------------------------

    @staticmethod
    def _detect_dialect(dsn: str) -> str:
        if dsn.startswith("sqlite"):
            return "sqlite"
        if dsn.startswith(("postgres://", "postgresql://")):
            return "postgres"
        raise ValueError(f"Unsupported queue DSN scheme: {dsn!r}")

    @staticmethod
    def _sqlite_path(dsn: str) -> str:
        # sqlite:///relative/file -> relative/file ; sqlite:////abs -> /abs
        if dsn.startswith("sqlite:///"):
            path = dsn[len("sqlite:///"):]
        elif dsn.startswith("sqlite://"):
            path = dsn[len("sqlite://"):]
        else:
            path = dsn
        if path in ("", ":memory:"):
            return ":memory:"
        return path

    def _connect_sqlite(self):
        import sqlite3
        if self._sqlite_conn is None:
            path = self._sqlite_path(self.dsn)
            if path != ":memory:":
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
            # isolation_level=None -> autocommit; we issue explicit
            # BEGIN IMMEDIATE around the claim so writers serialize.
            conn = sqlite3.connect(
                path, check_same_thread=False, isolation_level=None,
            )
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._sqlite_conn = conn
        return self._sqlite_conn

    def _connect_postgres(self):
        import psycopg2  # lazy: only needed for the Postgres backend
        return psycopg2.connect(self.dsn)

    def _q(self, sql: str) -> str:
        """Translate the ``?`` placeholders we author to the backend style."""
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql

    # -- schema -------------------------------------------------------------

    def ensure_schema(self):
        t = _TYPES[self.dialect]
        jobs = f"""
        CREATE TABLE IF NOT EXISTS capture_jobs (
            job_id        {t['text']} PRIMARY KEY,
            batch_id      {t['text']} NOT NULL UNIQUE,
            ordinal       {t['int']}  NOT NULL DEFAULT 0,
            zoom          {t['int']},
            tile_ids      {t['text']} NOT NULL,
            status        {t['text']} NOT NULL DEFAULT 'pending',
            claimed_by    {t['text']},
            lease_until   {t['float']},
            attempts      {t['int']}  NOT NULL DEFAULT 0,
            max_attempts  {t['int']}  NOT NULL DEFAULT 3,
            created_at    {t['float']} NOT NULL,
            updated_at    {t['float']} NOT NULL,
            completed_at  {t['float']},
            error_summary {t['text']},
            artifact_path {t['text']},
            artifact_id   {t['text']},
            result_meta   {t['text']},
            source        {t['text']},
            enqueue_id    {t['text']}
        )
        """
        workers = f"""
        CREATE TABLE IF NOT EXISTS workers (
            worker_id   {t['text']} PRIMARY KEY,
            first_seen  {t['float']} NOT NULL,
            last_seen   {t['float']} NOT NULL,
            meta        {t['text']}
        )
        """
        idx = [
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON capture_jobs (status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_claimable "
            "ON capture_jobs (status, created_at, ordinal)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_enqueue ON capture_jobs (enqueue_id)",
        ]
        if self.dialect == "sqlite":
            with self._lock:
                conn = self._connect_sqlite()
                conn.execute(jobs)
                conn.execute(workers)
                for s in idx:
                    conn.execute(s)
        else:
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    cur.execute(jobs)
                    cur.execute(workers)
                    for s in idx:
                        cur.execute(s)
                conn.commit()
            finally:
                conn.close()

    # -- row helpers --------------------------------------------------------

    def _row_to_dict(self, row):
        if row is None:
            return None
        d = dict(zip(_COLS, row))
        d["tile_ids"] = json.loads(d["tile_ids"]) if d.get("tile_ids") else []
        if d.get("result_meta"):
            try:
                d["result_meta"] = json.loads(d["result_meta"])
            except (TypeError, ValueError):
                d["result_meta"] = None
        else:
            d["result_meta"] = None
        return d

    @staticmethod
    def public(job: dict | None) -> dict | None:
        """Return an API-friendly view: epochs -> ISO-8601 strings."""
        if job is None:
            return None
        out = dict(job)
        for k in ("lease_until", "created_at", "updated_at", "completed_at"):
            out[k] = _iso(job.get(k))
        return out

    # -- enqueue ------------------------------------------------------------

    def enqueue_batches(self, batches, *, max_attempts=None, source="enqueue",
                        enqueue_id=None, now=None):
        """Insert one capture job per batch.

        ``batches`` is an iterable of dicts ``{"tile_ids": [...], "zoom": int}``.
        Each becomes one independently-claimable job. Returns the created job
        dicts. All jobs from one call share an ``enqueue_id`` (the wave) and are
        FIFO-ordered by ``(created_at, ordinal)``.
        """
        now = _now() if now is None else now
        max_attempts = DEFAULT_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
        enqueue_id = enqueue_id or _new_id("enq")
        rows = []
        for ordinal, batch in enumerate(batches):
            tile_ids = list(batch.get("tile_ids", []))
            if not tile_ids:
                continue
            rows.append((
                _new_id("job"),               # job_id
                _new_id("batch"),             # batch_id
                ordinal,                      # ordinal
                batch.get("zoom"),            # zoom
                json.dumps(tile_ids),         # tile_ids
                PENDING,                      # status
                None,                         # claimed_by
                None,                         # lease_until
                0,                            # attempts
                max_attempts,                 # max_attempts
                now,                          # created_at
                now,                          # updated_at
                None,                         # completed_at
                None,                         # error_summary
                None,                         # artifact_path
                None,                         # artifact_id
                None,                         # result_meta
                source,                       # source
                enqueue_id,                   # enqueue_id
            ))
        if not rows:
            return []
        placeholders = "(" + ", ".join(["?"] * len(_COLS)) + ")"
        sql = self._q(f"INSERT INTO capture_jobs ({_COL_LIST}) VALUES {placeholders}")
        if self.dialect == "sqlite":
            with self._lock:
                conn = self._connect_sqlite()
                conn.executemany(sql, rows)
        else:
            conn = self._connect_postgres()
            try:
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
                conn.commit()
            finally:
                conn.close()
        return [self.get_job(r[1]) for r in rows]

    # -- claim --------------------------------------------------------------

    def claim(self, worker_id, *, lease_seconds=None, now=None):
        """Atomically claim the oldest claimable job; returns it or ``None``.

        A job is claimable if it is ``pending``/``retryable`` **or** ``leased``
        with an expired ``lease_until`` (the dead-worker recovery path).
        """
        lease_seconds = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
        now = _now() if now is None else now
        lease_until = now + lease_seconds
        if self.dialect == "sqlite":
            return self._claim_sqlite(worker_id, now, lease_until)
        return self._claim_postgres(worker_id, now, lease_until)

    def _claimable_where(self):
        return (
            "(status IN ('pending', 'retryable') "
            "OR (status = 'leased' AND lease_until IS NOT NULL AND lease_until < ?))"
        )

    def _claim_sqlite(self, worker_id, now, lease_until):
        with self._lock:
            conn = self._connect_sqlite()
            # BEGIN IMMEDIATE takes the write lock up front so a concurrent
            # claim can't read the same candidate before we mark it leased.
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    f"SELECT {_COL_LIST} FROM capture_jobs "
                    f"WHERE {self._claimable_where()} "
                    "ORDER BY created_at, ordinal LIMIT 1",
                    (now,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                job = self._row_to_dict(row)
                conn.execute(
                    "UPDATE capture_jobs SET status='leased', claimed_by=?, "
                    "lease_until=?, attempts=attempts+1, updated_at=? "
                    "WHERE batch_id=?",
                    (worker_id, lease_until, now, job["batch_id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_job(job["batch_id"])

    def _claim_postgres(self, worker_id, now, lease_until):
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    self._q(
                        f"SELECT {_COL_LIST} FROM capture_jobs "
                        f"WHERE {self._claimable_where()} "
                        "ORDER BY created_at, ordinal LIMIT 1 "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    (now,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None
                job = self._row_to_dict(row)
                cur.execute(
                    self._q(
                        "UPDATE capture_jobs SET status='leased', claimed_by=?, "
                        "lease_until=?, attempts=attempts+1, updated_at=? "
                        "WHERE batch_id=?"
                    ),
                    (worker_id, lease_until, now, job["batch_id"]),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_job(job["batch_id"])

    # -- lease lifecycle ----------------------------------------------------

    def heartbeat_batch(self, batch_id, worker_id, *, lease_seconds=None, now=None):
        """Extend the lease on a job the worker still owns."""
        lease_seconds = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
        now = _now() if now is None else now
        job = self.get_job(batch_id)
        if job is None:
            raise QueueError(f"unknown batch {batch_id!r}")
        if job["status"] != LEASED or job["claimed_by"] != worker_id:
            raise LeaseLost(f"batch {batch_id!r} not leased by {worker_id!r}")
        self._update(batch_id, {
            "lease_until": now + lease_seconds,
            "updated_at": now,
        })
        return self.get_job(batch_id)

    def complete_batch(self, batch_id, worker_id, *, artifact_path=None,
                       artifact_id=None, result_meta=None, now=None):
        """Mark a leased job done and record its uploaded artifact metadata."""
        now = _now() if now is None else now
        job = self.get_job(batch_id)
        if job is None:
            raise QueueError(f"unknown batch {batch_id!r}")
        if job["status"] != LEASED or job["claimed_by"] != worker_id:
            raise LeaseLost(f"batch {batch_id!r} not leased by {worker_id!r}")
        self._update(batch_id, {
            "status": DONE,
            "completed_at": now,
            "updated_at": now,
            "lease_until": None,
            "artifact_path": artifact_path,
            "artifact_id": artifact_id,
            "result_meta": json.dumps(result_meta) if result_meta is not None else None,
            "error_summary": None,
        })
        return self.get_job(batch_id)

    def fail_batch(self, batch_id, worker_id, *, error_summary="",
                   retryable=True, now=None):
        """Mark a leased job failed.

        If ``retryable`` and attempts remain (< ``max_attempts``) the job goes
        back to ``retryable`` and can be claimed again; otherwise ``failed``.
        """
        now = _now() if now is None else now
        job = self.get_job(batch_id)
        if job is None:
            raise QueueError(f"unknown batch {batch_id!r}")
        if job["status"] != LEASED or job["claimed_by"] != worker_id:
            raise LeaseLost(f"batch {batch_id!r} not leased by {worker_id!r}")
        can_retry = retryable and job["attempts"] < job["max_attempts"]
        self._update(batch_id, {
            "status": RETRYABLE if can_retry else FAILED,
            "updated_at": now,
            "lease_until": None,
            "claimed_by": None if can_retry else job["claimed_by"],
            "completed_at": None if can_retry else now,
            "error_summary": (error_summary or "")[:1000],
        })
        return self.get_job(batch_id)

    def reclaim_expired(self, *, now=None):
        """Flip expired leases back to ``retryable`` (housekeeping).

        Not strictly required — :meth:`claim` already treats an expired lease as
        claimable — but it keeps queue stats honest and lets ops see recovery.
        Returns the number of jobs reclaimed.
        """
        now = _now() if now is None else now
        sql = self._q(
            "UPDATE capture_jobs SET status='retryable', claimed_by=NULL, "
            "lease_until=NULL, updated_at=? "
            "WHERE status='leased' AND lease_until IS NOT NULL AND lease_until < ?"
        )
        if self.dialect == "sqlite":
            with self._lock:
                conn = self._connect_sqlite()
                cur = conn.execute(sql, (now, now))
                return cur.rowcount
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (now, now))
                n = cur.rowcount
            conn.commit()
            return n
        finally:
            conn.close()

    # -- generic update -----------------------------------------------------

    def _update(self, batch_id, fields: dict):
        cols = list(fields.keys())
        assigns = ", ".join(f"{c}=?" for c in cols)
        params = [fields[c] for c in cols] + [batch_id]
        sql = self._q(f"UPDATE capture_jobs SET {assigns} WHERE batch_id=?")
        if self.dialect == "sqlite":
            with self._lock:
                conn = self._connect_sqlite()
                conn.execute(sql, params)
            return
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # -- reads --------------------------------------------------------------

    def get_job(self, batch_id):
        sql = self._q(
            f"SELECT {_COL_LIST} FROM capture_jobs WHERE batch_id=?"
        )
        row = self._fetchone(sql, (batch_id,))
        return self._row_to_dict(row)

    def get_job_by_id(self, job_id):
        sql = self._q(f"SELECT {_COL_LIST} FROM capture_jobs WHERE job_id=?")
        row = self._fetchone(sql, (job_id,))
        return self._row_to_dict(row)

    def list_jobs(self, *, status=None, limit=100):
        if status:
            sql = self._q(
                f"SELECT {_COL_LIST} FROM capture_jobs WHERE status=? "
                "ORDER BY created_at, ordinal LIMIT ?"
            )
            params = (status, int(limit))
        else:
            sql = self._q(
                f"SELECT {_COL_LIST} FROM capture_jobs "
                "ORDER BY created_at, ordinal LIMIT ?"
            )
            params = (int(limit),)
        return [self._row_to_dict(r) for r in self._fetchall(sql, params)]

    def stats(self, *, now=None):
        rows = self._fetchall(
            self._q("SELECT status, COUNT(*) FROM capture_jobs GROUP BY status"),
            (),
        )
        by_status = {r[0]: r[1] for r in rows}
        return {
            "by_status": by_status,
            "total": sum(by_status.values()),
            "pending": by_status.get(PENDING, 0),
            "leased": by_status.get(LEASED, 0),
            "done": by_status.get(DONE, 0),
            "failed": by_status.get(FAILED, 0),
            "retryable": by_status.get(RETRYABLE, 0),
        }

    # -- workers ------------------------------------------------------------

    def register_worker(self, worker_id, meta=None, *, now=None):
        now = _now() if now is None else now
        meta_json = json.dumps(meta) if meta else None
        if self.dialect == "sqlite":
            sql = (
                "INSERT INTO workers (worker_id, first_seen, last_seen, meta) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "meta=COALESCE(excluded.meta, workers.meta)"
            )
        else:
            sql = self._q(
                "INSERT INTO workers (worker_id, first_seen, last_seen, meta) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (worker_id) DO UPDATE SET last_seen=EXCLUDED.last_seen, "
                "meta=COALESCE(EXCLUDED.meta, workers.meta)"
            )
        params = (worker_id, now, now, meta_json)
        if self.dialect == "sqlite":
            with self._lock:
                self._connect_sqlite().execute(sql, params)
            return
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def touch_worker(self, worker_id, *, now=None):
        now = _now() if now is None else now
        # Register-on-touch keeps heartbeats working even if /register was missed.
        self.register_worker(worker_id, None, now=now)

    # -- low-level fetch ----------------------------------------------------

    def _fetchone(self, sql, params):
        if self.dialect == "sqlite":
            with self._lock:
                cur = self._connect_sqlite().execute(sql, params)
                return cur.fetchone()
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        finally:
            conn.close()

    def _fetchall(self, sql, params):
        if self.dialect == "sqlite":
            with self._lock:
                cur = self._connect_sqlite().execute(sql, params)
                return cur.fetchall()
        conn = self._connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    def close(self):
        if self._sqlite_conn is not None:
            self._sqlite_conn.close()
            self._sqlite_conn = None
