"""Tests for the worker control-plane API (worker_api).

Covers token auth (missing/bad token rejected) and the claim → complete happy
path including artifact storage. Uses FastAPI's TestClient against an app built
on a throwaway SQLite queue with a known token, so nothing external is touched.

Run with::

    .venv/Scripts/python.exe -m pytest tests/test_worker_api.py
    .venv/Scripts/python.exe tests/test_worker_api.py   # standalone
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import worker_api
from worker_queue import Queue

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client():
    tmp = tempfile.mkdtemp()
    q = Queue(f"sqlite:///{os.path.join(tmp, 'q.sqlite3')}")
    app = worker_api.create_app(
        q,
        tokens={TOKEN},
        artifacts_dir=os.path.join(tmp, "artifacts"),
        allow_no_auth=False,
        auto_ingest=False,
    )
    return TestClient(app), q


def test_health_needs_no_auth():
    client, _ = _client()
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_auth_rejects_missing_token():
    client, _ = _client()
    r = client.post("/queue/claim", json={"worker_id": "w1"})
    assert r.status_code == 401


def test_auth_rejects_bad_token():
    client, _ = _client()
    r = client.post("/queue/claim", json={"worker_id": "w1"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_auth_accepts_good_token():
    client, _ = _client()
    r = client.post("/queue/claim", json={"worker_id": "w1"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["batch"] is None  # nothing enqueued yet


def test_claim_complete_flow_and_artifact():
    client, q = _client()
    q.enqueue_batches([{"tile_ids": ["g_z9_r0_c0", "g_z9_r0_c1"], "zoom": 9}])

    claimed = client.post("/queue/claim", json={"worker_id": "w1", "lease_seconds": 120},
                          headers=AUTH).json()["batch"]
    assert claimed["status"] == "leased"
    batch_id = claimed["batch_id"]

    # heartbeat keeps the lease; wrong worker is rejected
    assert client.post(f"/queue/{batch_id}/heartbeat",
                       json={"worker_id": "w1"}, headers=AUTH).status_code == 200
    assert client.post(f"/queue/{batch_id}/heartbeat",
                       json={"worker_id": "intruder"}, headers=AUTH).status_code == 409

    captures = [
        {"capture_type": "tile", "tile_id": "g_z9_r0_c0", "status": "success",
         "tankers": 2, "cargos": 1, "markers": []},
        {"capture_type": "tile", "tile_id": "g_z9_r0_c1", "status": "success",
         "tankers": 0, "cargos": 3, "markers": []},
    ]
    r = client.post(f"/queue/{batch_id}/complete",
                    json={"worker_id": "w1", "captures": captures}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["batch"]["status"] == "done"
    assert body["artifact_path"] and os.path.exists(body["artifact_path"])
    with open(body["artifact_path"], encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 2
    assert body["batch"]["result_meta"]["tankers"] == 2

    # job is gone from the claimable set
    assert client.post("/queue/claim", json={"worker_id": "w1"},
                       headers=AUTH).json()["batch"] is None


def test_complete_rejects_lost_lease():
    client, q = _client()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])
    claimed = client.post("/queue/claim", json={"worker_id": "w1"},
                          headers=AUTH).json()["batch"]
    # A different worker tries to complete the batch it doesn't own.
    r = client.post(f"/queue/{claimed['batch_id']}/complete",
                    json={"worker_id": "someone-else", "captures": []}, headers=AUTH)
    assert r.status_code == 409


def test_fail_marks_retryable():
    client, q = _client()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])
    claimed = client.post("/queue/claim", json={"worker_id": "w1"},
                          headers=AUTH).json()["batch"]
    r = client.post(f"/queue/{claimed['batch_id']}/fail",
                    json={"worker_id": "w1", "error_summary": "boom", "retryable": True},
                    headers=AUTH)
    assert r.status_code == 200 and r.json()["batch"]["status"] == "retryable"


def test_no_tokens_configured_returns_503():
    tmp = tempfile.mkdtemp()
    q = Queue(f"sqlite:///{os.path.join(tmp, 'q.sqlite3')}")
    app = worker_api.create_app(q, tokens=set(), allow_no_auth=False)
    client = TestClient(app)
    r = client.post("/queue/claim", json={"worker_id": "w1"},
                    headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


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
