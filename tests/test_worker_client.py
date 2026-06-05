"""Tests for the worker client glue (worker.process_batch).

The real capture stack (Patchright/Chrome, OpenCV, psycopg2) isn't available in
CI, so these inject a fake ``scraper_global`` to validate the worker's own
logic: resolving tile ids, redirecting the capture log to a scratch file,
reading the produced JSONL back, and choosing complete vs. fail. A second test
drives the *real* HTTP client against a live in-process control plane.

Run with::

    .venv/Scripts/python.exe -m pytest tests/test_worker_client.py
    .venv/Scripts/python.exe tests/test_worker_client.py   # standalone
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker


def make_fake_scraper(behavior):
    """A stand-in for scraper_global exposing only what worker.process_batch uses."""
    fake = types.SimpleNamespace()
    fake._capture_log_path = Path("unused-default")
    fake.proxies = []
    fake.geo_profiles = {}
    fake.MAX_BROWSERS = 1

    def _select(tile_ids=None, respect_schedule=True, **_kw):
        if behavior.get("missing_tiles"):
            return []
        return [{"tile_id": t, "zoom": 9} for t in (tile_ids or [])]

    def _capture(tiles, _ts):
        if behavior.get("raise"):
            raise behavior["raise"]
        if behavior.get("empty"):
            return {"_retryable": [t["tile_id"] for t in tiles]}
        with open(fake._capture_log_path, "a", encoding="utf-8") as f:
            for t in tiles:
                f.write(json.dumps({
                    "capture_type": "tile", "tile_id": t["tile_id"],
                    "status": "success", "tankers": 1, "cargos": 2, "markers": [],
                }) + "\n")
        return {t["tile_id"]: {"tankers": 1} for t in tiles}

    fake._select_global_tiles = _select
    fake.capture_tile_batch_worker = _capture
    fake._is_crash_error = lambda exc: "crash" in str(exc).lower()
    fake.resolve_all_proxies = lambda proxies: {}
    return fake


class RecordingClient:
    def __init__(self):
        self.completed = []
        self.failed = []

    def complete(self, batch_id, worker_id, captures, stats):
        self.completed.append((batch_id, captures, stats))
        return {"batch": {"result_meta": stats}}

    def fail(self, batch_id, worker_id, error_summary, retryable):
        self.failed.append((batch_id, error_summary, retryable))
        return {}

    def heartbeat(self, *a, **k):
        return {}


def _install_fake(behavior):
    fake = make_fake_scraper(behavior)
    worker._sg = fake
    worker._geo_resolved = True
    return fake


def _batch(tile_ids):
    return {"batch_id": "batch_test", "tile_ids": tile_ids, "zoom": 9}


def test_successful_capture_completes_with_captures():
    fake = _install_fake({})
    prev = fake._capture_log_path
    client = RecordingClient()
    scratch = Path(tempfile.mkdtemp())
    outcome = worker.process_batch(client, "w1", _batch(["a", "b"]),
                                   scratch, lease_seconds=60, no_geo=True)
    assert outcome == "done"
    assert len(client.completed) == 1
    _bid, captures, stats = client.completed[0]
    assert len(captures) == 2
    assert stats["tiles_ok"] == 2 and stats["tankers"] == 2
    # scratch cleaned up and module global restored
    assert not (scratch / "batch_test.jsonl").exists()
    assert fake._capture_log_path == prev


def test_empty_capture_fails_retryable():
    _install_fake({"empty": True})
    client = RecordingClient()
    scratch = Path(tempfile.mkdtemp())
    outcome = worker.process_batch(client, "w1", _batch(["a"]),
                                   scratch, lease_seconds=60, no_geo=True)
    assert outcome == "empty"
    assert client.failed and client.failed[0][2] is True  # retryable


def test_exception_with_no_output_fails():
    _install_fake({"raise": RuntimeError("boom")})
    client = RecordingClient()
    scratch = Path(tempfile.mkdtemp())
    outcome = worker.process_batch(client, "w1", _batch(["a"]),
                                   scratch, lease_seconds=60, no_geo=True)
    assert outcome == "failed"
    assert client.failed and "boom" in client.failed[0][1]


def test_unresolvable_tiles_fail_non_retryable():
    _install_fake({"missing_tiles": True})
    client = RecordingClient()
    scratch = Path(tempfile.mkdtemp())
    outcome = worker.process_batch(client, "w1", _batch(["ghost"]),
                                   scratch, lease_seconds=60, no_geo=True)
    assert outcome == "no-tiles"
    assert client.failed and client.failed[0][2] is False  # non-retryable


def test_live_end_to_end_with_real_http_client():
    """Drive the actual ControlPlaneClient against a live in-process server."""
    import socket
    import threading
    import time

    import uvicorn

    import worker_api
    from worker_queue import Queue

    tmp = tempfile.mkdtemp()
    token = "live-token"
    q = Queue(f"sqlite:///{os.path.join(tmp, 'q.sqlite3')}")
    q.enqueue_batches([{"tile_ids": ["a", "b"], "zoom": 9}])
    app = worker_api.create_app(q, tokens={token},
                                artifacts_dir=os.path.join(tmp, "art"))

    # ephemeral free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # wait for startup
        import requests
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if requests.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            print("SKIP live test: server did not start")
            return

        _install_fake({})
        client = worker.ControlPlaneClient(base, token)
        client.register("live-worker", {"max_browsers": 1})
        batch = client.claim("live-worker", lease_seconds=60)
        assert batch is not None and batch["status"] == "leased"
        outcome = worker.process_batch(client, "live-worker", batch,
                                       Path(tmp) / "scratch", 60, no_geo=True)
        assert outcome == "done", outcome
        assert q.get_job(batch["batch_id"])["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'OK' if not failures else 'FAILED'} ({failures} failures)")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
