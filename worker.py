"""Distributed capture worker for OceanScrape.

A worker is a thin, stateless client of the control plane (``worker_api.py``).
It owns no database, reads none of the control plane's ``.env``, and writes no
warehouse rows. Its whole life is::

    register → claim a batch → capture its tiles with the existing scraper →
    upload the raw JSONL back to the control plane → repeat

Because capture reuses ``scraper_global`` unchanged, a worker host needs the
capture stack (Patchright/Chrome, OpenCV) and its **own** proxy credentials —
but nothing about the control plane's Postgres or data paths.

Run::

    python run.py worker --server http://10.0.0.3:8081 --token-env WORKER_TOKEN \
        --max-browsers 1

Each worker process captures one batch at a time (one batch == one browser).
To use more browsers on a beefy host, run several worker processes.

The worker token is read from an env var (``--token-env``, default
``WORKER_TOKEN``) and is never printed or logged. Proxy credentials live inside
``scraper_global`` and are likewise never emitted.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger("worker")

# scraper_global is imported lazily (see _load_scraper) so `worker --help` and
# unit tests don't require the heavy capture stack (Patchright, psycopg2, ...).
_sg = None
_geo_resolved = False


def _load_scraper():
    global _sg
    if _sg is None:
        import scraper_global as sg
        _sg = sg
    return _sg


def _default_worker_id() -> str:
    host = socket.gethostname()
    return f"{host}-{os.getpid()}-{random.randint(0, 9999):04d}"


def _short_error(exc: Exception, limit: int = 500) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


class ControlPlaneClient:
    """Minimal HTTP client for the worker control-plane API.

    Holds the bearer token only in the session's Authorization header; it is
    never logged. All calls are scoped to a single ``server`` base URL.
    """

    def __init__(self, server: str, token: str, *, timeout: float = 30.0):
        self.base = server.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(
            f"{self.base}{path}", json=payload, timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def register(self, worker_id: str, meta: dict) -> dict:
        return self._post("/worker/register", {"worker_id": worker_id, "meta": meta})

    def claim(self, worker_id: str, lease_seconds: int) -> dict | None:
        out = self._post(
            "/queue/claim",
            {"worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        return out.get("batch")

    def heartbeat(self, batch_id: str, worker_id: str, lease_seconds: int) -> dict:
        return self._post(
            f"/queue/{batch_id}/heartbeat",
            {"worker_id": worker_id, "lease_seconds": lease_seconds},
        )

    def complete(self, batch_id: str, worker_id: str, captures: list,
                 stats: dict) -> dict:
        return self._post(
            f"/queue/{batch_id}/complete",
            {"worker_id": worker_id, "captures": captures, "stats": stats},
        )

    def fail(self, batch_id: str, worker_id: str, error_summary: str,
             retryable: bool) -> dict:
        return self._post(
            f"/queue/{batch_id}/fail",
            {
                "worker_id": worker_id,
                "error_summary": error_summary,
                "retryable": retryable,
            },
        )


class _LeaseHeartbeat:
    """Background thread that keeps a claimed batch's lease alive during capture.

    Capture can run for minutes; without heartbeats the lease would expire and
    the control plane would hand the batch to another worker. Runs as a daemon
    so it never blocks shutdown.
    """

    def __init__(self, client: ControlPlaneClient, batch_id: str,
                 worker_id: str, lease_seconds: int):
        self.client = client
        self.batch_id = batch_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        interval = max(5, self.lease_seconds // 3)
        self._thread = threading.Thread(
            target=self._run, args=(interval,), daemon=True,
        )
        self._thread.start()
        return self

    def _run(self, interval: int):
        while not self._stop.wait(interval):
            try:
                self.client.heartbeat(self.batch_id, self.worker_id,
                                      self.lease_seconds)
            except Exception as exc:
                # A lost lease (409) or transient network blip — log and keep
                # trying; the eventual complete() will surface a real conflict.
                logger.debug("heartbeat for %s failed: %s", self.batch_id, exc)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _read_jsonl(path: Path) -> list[dict]:
    import json
    entries = []
    if not path.exists():
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    return entries


def _summarize(captures: list) -> dict:
    ok = sum(1 for c in captures if c.get("status") == "success")
    return {
        "tiles_total": len(captures),
        "tiles_ok": ok,
        "tiles_failed": len(captures) - ok,
        "tankers": sum(int(c.get("tankers", 0)) for c in captures),
        "cargos": sum(int(c.get("cargos", 0)) for c in captures),
        "moving_tankers": sum(int(c.get("moving_tankers", 0)) for c in captures),
        "moving_cargos": sum(int(c.get("moving_cargos", 0)) for c in captures),
    }


def _ensure_geo(no_geo: bool):
    """Resolve proxy geolocations once (best-effort), like scraper main()."""
    global _geo_resolved
    if _geo_resolved or no_geo:
        return
    sg = _load_scraper()
    try:
        sg.geo_profiles = sg.resolve_all_proxies(sg.proxies)
        logger.info("Resolved %d/%d proxy geo profiles",
                    len(sg.geo_profiles), len(sg.proxies))
    except Exception as exc:
        logger.warning("Proxy geo resolution failed (%s); using fallbacks", exc)
    _geo_resolved = True


def process_batch(client: ControlPlaneClient, worker_id: str, batch: dict,
                  scratch_dir: Path, lease_seconds: int, no_geo: bool) -> str:
    """Capture one claimed batch and report the result. Returns an outcome str."""
    sg = _load_scraper()
    batch_id = batch["batch_id"]
    tile_ids = batch.get("tile_ids", [])

    # Resolve tile ids against the worker's own deterministic manifest. No DB:
    # respect_schedule=False skips the only DB call in this path.
    tiles = sg._select_global_tiles(tile_ids=tile_ids, respect_schedule=False)
    if not tiles:
        client.fail(batch_id, worker_id,
                    error_summary="no resolvable tiles for batch (manifest mismatch?)",
                    retryable=False)
        return "no-tiles"

    _ensure_geo(no_geo)

    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch_path = scratch_dir / f"{batch_id}.jsonl"
    if scratch_path.exists():
        scratch_path.unlink()

    # Redirect the scraper's per-run log to our scratch file. The worker
    # processes one batch at a time, so this module global is not contended.
    prev_log_path = sg._capture_log_path
    sg._capture_log_path = scratch_path
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")

    try:
        with _LeaseHeartbeat(client, batch_id, worker_id, lease_seconds):
            try:
                sg.capture_tile_batch_worker(tiles, timestamp_str)
            except Exception as exc:
                captures = _read_jsonl(scratch_path)
                if captures:
                    # Partial capture still worth keeping.
                    client.complete(batch_id, worker_id, captures, _summarize(captures))
                    return "partial"
                retryable = sg._is_crash_error(exc) or True
                client.fail(batch_id, worker_id,
                            error_summary=_short_error(exc), retryable=retryable)
                logger.warning("batch %s capture failed: %s", batch_id,
                               _short_error(exc))
                return "failed"

        captures = _read_jsonl(scratch_path)
        if not captures:
            client.fail(batch_id, worker_id,
                        error_summary="capture produced no tiles", retryable=True)
            return "empty"
        result = client.complete(batch_id, worker_id, captures, _summarize(captures))
        meta = (result.get("batch") or {}).get("result_meta") or {}
        logger.info("batch %s done: %d captures (tankers=%s cargos=%s)",
                    batch_id, len(captures), meta.get("tankers"), meta.get("cargos"))
        return "done"
    finally:
        sg._capture_log_path = prev_log_path
        try:
            if scratch_path.exists():
                scratch_path.unlink()
        except OSError:
            pass


def run_worker(args) -> int:
    token = args.token or os.getenv(args.token_env, "")
    if not token:
        logger.error("No worker token: set --token or env %s", args.token_env)
        return 2

    worker_id = args.worker_id or _default_worker_id()
    client = ControlPlaneClient(args.server, token, timeout=args.http_timeout)
    scratch_dir = Path(args.scratch_dir)
    meta = {"max_browsers": args.max_browsers, "host": socket.gethostname()}

    # Apply max-browsers to the scraper config for completeness (a single batch
    # is still captured by one browser; scale out with more worker processes).
    if args.max_browsers:
        try:
            _load_scraper().MAX_BROWSERS = int(args.max_browsers)
        except Exception:
            pass

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Signal %s received — finishing current batch then stopping",
                    signum)
        stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported platform

    # Register; tolerate the control plane being slow to come up.
    try:
        client.register(worker_id, meta)
        logger.info("Registered worker %s with %s", worker_id, args.server)
    except Exception as exc:
        logger.warning("register failed (%s); continuing to claim loop", exc)

    idle_backoff = args.poll_seconds
    while not stop.is_set():
        try:
            batch = client.claim(worker_id, args.lease_seconds)
        except Exception as exc:
            logger.warning("claim failed (%s); retrying in %.0fs", exc, idle_backoff)
            stop.wait(idle_backoff)
            idle_backoff = min(idle_backoff * 2, 60)
            continue
        idle_backoff = args.poll_seconds

        if not batch:
            if args.once:
                logger.info("Queue empty and --once set; exiting")
                break
            stop.wait(args.poll_seconds)
            continue

        logger.info("Claimed batch %s (%d tiles, zoom=%s)",
                    batch["batch_id"], len(batch.get("tile_ids", [])),
                    batch.get("zoom"))
        try:
            process_batch(client, worker_id, batch, scratch_dir,
                          args.lease_seconds, args.no_geo)
        except Exception as exc:
            # Last-resort guard: never let one batch kill the loop.
            logger.error("Unhandled error processing %s: %s",
                         batch.get("batch_id"), _short_error(exc))
            try:
                client.fail(batch["batch_id"], worker_id,
                            error_summary=_short_error(exc), retryable=True)
            except Exception:
                pass

        if args.once:
            break

    logger.info("Worker %s stopped", worker_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py worker",
        description="OceanScrape distributed capture worker.",
    )
    p.add_argument("--server", default=os.getenv("SERVER_URL", "http://127.0.0.1:8081"),
                   help="Control plane base URL (default env SERVER_URL or "
                        "http://127.0.0.1:8081)")
    p.add_argument("--token-env", default="WORKER_TOKEN",
                   help="Env var holding the worker bearer token (default WORKER_TOKEN)")
    p.add_argument("--token", default=None,
                   help="Worker token literal (prefer --token-env to avoid shell history)")
    p.add_argument("--worker-id", default=os.getenv("WORKER_ID"),
                   help="Stable worker id (default: host-pid-rand)")
    p.add_argument("--max-browsers", type=int,
                   default=int(os.getenv("MAX_BROWSERS", "1")),
                   help="Browsers per worker process (one batch == one browser)")
    p.add_argument("--lease-seconds", type=int,
                   default=int(os.getenv("WORKER_LEASE_SECONDS", "600")))
    p.add_argument("--poll-seconds", type=float, default=5.0,
                   help="Idle poll interval when the queue is empty")
    p.add_argument("--http-timeout", type=float, default=30.0)
    p.add_argument("--scratch-dir",
                   default=os.getenv("WORKER_SCRATCH_DIR", "data/worker_scratch"),
                   help="Local dir for transient per-batch JSONL (never the warehouse)")
    p.add_argument("--no-geo", action="store_true",
                   help="Skip proxy geolocation resolution at startup")
    p.add_argument("--once", action="store_true",
                   help="Process available batches then exit when the queue drains")
    return p


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] worker: %(message)s",
    )
    args = build_parser().parse_args(argv)
    return run_worker(args)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
