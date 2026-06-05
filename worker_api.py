"""Control-plane HTTP API for distributed OceanScrape capture workers.

This is a small FastAPI app, intentionally **separate** from the dashboard API
(``api.py``). It is the only thing distributed workers talk to: they claim
capture jobs, heartbeat their lease, and upload raw JSONL back here. The
control plane owns the queue (``worker_queue.Queue``), the leases, the artifact
files, and — optionally — the eventual Postgres ingest. Workers never touch the
database or the warehouse directly.

Bind it on a private interface for a Hetzner-style deployment, e.g.::

    WORKER_API_HOST=10.0.0.3 WORKER_API_PORT=8081 \
    WORKER_API_TOKEN=... WORKER_QUEUE_DSN=$DATABASE_URL \
    python run.py serve

Auth: every ``/worker`` and ``/queue`` endpoint requires
``Authorization: Bearer <token>``. Tokens come from ``WORKER_API_TOKEN`` (single)
or ``WORKER_TOKENS`` (comma-separated). Tokens are never logged. If no token is
configured the API refuses worker requests (503) unless
``WORKER_API_ALLOW_NO_AUTH=1`` is set for local development.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from worker_queue import (
    DEFAULT_LEASE_SECONDS,
    LeaseLost,
    Queue,
    QueueError,
    default_dsn,
)

load_dotenv()

logger = logging.getLogger("worker_api")

DEFAULT_ARTIFACTS_DIR = os.getenv("WORKER_ARTIFACTS_DIR", "data/raw/queue")


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def load_tokens() -> set[str]:
    """Collect worker tokens from env. Never logged, never returned to clients."""
    tokens: set[str] = set()
    multi = os.getenv("WORKER_TOKENS", "")
    for tok in multi.split(","):
        tok = tok.strip()
        if tok:
            tokens.add(tok)
    single = os.getenv("WORKER_API_TOKEN", "").strip()
    if single:
        tokens.add(single)
    return tokens


def _token_matches(presented: str, allowed: set[str]) -> bool:
    # Constant-time compare against every configured token to avoid leaking
    # which prefix matched via timing.
    ok = False
    for tok in allowed:
        if hmac.compare_digest(presented, tok):
            ok = True
    return ok


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RegisterReq(BaseModel):
    worker_id: str
    meta: Optional[dict] = None


class HeartbeatReq(BaseModel):
    worker_id: str


class ClaimReq(BaseModel):
    worker_id: str
    lease_seconds: Optional[int] = None


class LeaseHeartbeatReq(BaseModel):
    worker_id: str
    lease_seconds: Optional[int] = None


class CompleteReq(BaseModel):
    worker_id: str
    captures: List[dict] = []
    stats: Optional[dict] = None
    artifact_id: Optional[str] = None


class FailReq(BaseModel):
    worker_id: str
    error_summary: str = ""
    retryable: bool = True


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(queue: Optional[Queue] = None, *, tokens: Optional[set] = None,
               artifacts_dir: Optional[str] = None,
               allow_no_auth: Optional[bool] = None,
               auto_ingest: Optional[bool] = None) -> FastAPI:
    app = FastAPI(title="OceanScrape Worker Control Plane")

    app.state.queue = queue or Queue(default_dsn())
    app.state.tokens = load_tokens() if tokens is None else set(tokens)
    app.state.artifacts_dir = Path(
        artifacts_dir if artifacts_dir is not None else DEFAULT_ARTIFACTS_DIR
    )
    app.state.allow_no_auth = (
        os.getenv("WORKER_API_ALLOW_NO_AUTH", "0") == "1"
        if allow_no_auth is None else allow_no_auth
    )
    app.state.auto_ingest = (
        os.getenv("WORKER_API_AUTO_INGEST", "0") == "1"
        if auto_ingest is None else auto_ingest
    )

    if not app.state.tokens and not app.state.allow_no_auth:
        logger.warning(
            "Worker API has no WORKER_API_TOKEN/WORKER_TOKENS configured; "
            "worker endpoints will reject all requests. Set a token, or "
            "WORKER_API_ALLOW_NO_AUTH=1 for local development only."
        )

    def require_token(request: Request,
                      authorization: Optional[str] = Header(None)):
        allowed = request.app.state.tokens
        if not allowed:
            if request.app.state.allow_no_auth:
                return
            raise HTTPException(503, "Worker API has no tokens configured")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        presented = authorization.split(" ", 1)[1].strip()
        if not _token_matches(presented, allowed):
            raise HTTPException(401, "Invalid worker token")

    auth = [Depends(require_token)]

    # -- health (no auth) ---------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "queue_dialect": app.state.queue.dialect}

    # -- worker registration ------------------------------------------------

    @app.post("/worker/register", dependencies=auth)
    def worker_register(body: RegisterReq):
        # Never persist or echo tokens; meta is worker-supplied capability info.
        app.state.queue.register_worker(body.worker_id, body.meta)
        return {"ok": True, "worker_id": body.worker_id}

    @app.post("/worker/heartbeat", dependencies=auth)
    def worker_heartbeat(body: HeartbeatReq):
        app.state.queue.touch_worker(body.worker_id)
        return {"ok": True}

    # -- queue --------------------------------------------------------------

    @app.post("/queue/claim", dependencies=auth)
    def queue_claim(body: ClaimReq):
        q = app.state.queue
        q.touch_worker(body.worker_id)
        lease = body.lease_seconds or DEFAULT_LEASE_SECONDS
        job = q.claim(body.worker_id, lease_seconds=lease)
        return {"batch": q.public(job)}

    @app.post("/queue/{batch_id}/heartbeat", dependencies=auth)
    def queue_heartbeat(batch_id: str, body: LeaseHeartbeatReq):
        q = app.state.queue
        try:
            job = q.heartbeat_batch(
                batch_id, body.worker_id, lease_seconds=body.lease_seconds,
            )
        except LeaseLost as exc:
            raise HTTPException(409, str(exc))
        except QueueError as exc:
            raise HTTPException(404, str(exc))
        return {"batch": q.public(job)}

    @app.post("/queue/{batch_id}/complete", dependencies=auth)
    def queue_complete(batch_id: str, body: CompleteReq):
        q = app.state.queue
        job = q.get_job(batch_id)
        if job is None:
            raise HTTPException(404, f"unknown batch {batch_id!r}")

        artifact_path = _write_artifact(
            app.state.artifacts_dir, batch_id, job.get("job_id"), body.captures,
        )
        result_meta = body.stats or _summarize_captures(body.captures)
        result_meta = dict(result_meta)
        result_meta.setdefault("capture_count", len(body.captures))

        try:
            updated = q.complete_batch(
                batch_id, body.worker_id,
                artifact_path=str(artifact_path) if artifact_path else None,
                artifact_id=body.artifact_id,
                result_meta=result_meta,
            )
        except LeaseLost as exc:
            # The lease expired and was reclaimed by another worker. Reject so a
            # stale upload cannot mark a job done that someone else now owns.
            raise HTTPException(409, str(exc))
        except QueueError as exc:
            raise HTTPException(404, str(exc))

        ingest_result = None
        if app.state.auto_ingest and artifact_path is not None:
            ingest_result = _maybe_ingest(artifact_path)

        return {
            "batch": q.public(updated),
            "artifact_path": str(artifact_path) if artifact_path else None,
            "ingest": ingest_result,
        }

    @app.post("/queue/{batch_id}/fail", dependencies=auth)
    def queue_fail(batch_id: str, body: FailReq):
        q = app.state.queue
        try:
            job = q.fail_batch(
                batch_id, body.worker_id,
                error_summary=body.error_summary, retryable=body.retryable,
            )
        except LeaseLost as exc:
            raise HTTPException(409, str(exc))
        except QueueError as exc:
            raise HTTPException(404, str(exc))
        return {"batch": q.public(job)}

    # -- ops ----------------------------------------------------------------

    @app.get("/queue/stats", dependencies=auth)
    def queue_stats():
        return app.state.queue.stats()

    @app.get("/queue/jobs", dependencies=auth)
    def queue_jobs(status: Optional[str] = None, limit: int = 100):
        q = app.state.queue
        jobs = q.list_jobs(status=status, limit=limit)
        return {"jobs": [q.public(j) for j in jobs]}

    return app


# ---------------------------------------------------------------------------
# Artifact + ingest helpers
# ---------------------------------------------------------------------------

def _write_artifact(artifacts_dir: Path, batch_id, job_id, captures) -> Optional[Path]:
    """Persist a worker's uploaded raw captures as a JSONL artifact file.

    Mirrors the scraper's own ``captures.jsonl`` format so the existing
    ``update_database.ingest_file`` can load it unchanged.
    """
    if not captures:
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{batch_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for entry in captures:
            f.write(json.dumps(entry) + "\n")
    return path


def _summarize_captures(captures) -> dict:
    """Derive coarse counts from uploaded captures for queue ``result_meta``."""
    ok = sum(1 for c in captures if c.get("status") == "success")
    failed = sum(1 for c in captures if c.get("status") not in (None, "success"))
    return {
        "tiles_total": len(captures),
        "tiles_ok": ok,
        "tiles_failed": failed,
        "tankers": sum(int(c.get("tankers", 0)) for c in captures),
        "cargos": sum(int(c.get("cargos", 0)) for c in captures),
        "moving_tankers": sum(int(c.get("moving_tankers", 0)) for c in captures),
        "moving_cargos": sum(int(c.get("moving_cargos", 0)) for c in captures),
    }


def _maybe_ingest(artifact_path: Path) -> dict:
    """Best-effort inline ingest of an artifact into Postgres.

    Lazy-imports the DB layer so the API has no hard psycopg2 dependency when
    auto-ingest is disabled. Never raises into the request — the raw artifact is
    already saved and can be ingested later via ``update_database.py``.
    """
    try:
        from update_database import ingest_file
        result = ingest_file(str(artifact_path))
        return {"ok": True, **(result or {})}
    except Exception as exc:  # pragma: no cover - depends on live DB
        logger.error("Auto-ingest failed for %s: %s", artifact_path, exc)
        return {"ok": False, "error": str(exc)}


# Module-level app for ``uvicorn worker_api:app`` / ``run.py serve``.
app = create_app()
