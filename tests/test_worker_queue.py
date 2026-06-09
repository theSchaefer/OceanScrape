"""Tests for the capture-job queue (worker_queue.Queue).

Covers the load-bearing invariants of the distributed model:
  * atomic claiming — two workers never get the same batch
  * expired leases become claimable again (dead-worker recovery)
  * complete sets `done` and records artifact metadata
  * fail honours the retry budget (retryable until attempts exhausted)

Backed by SQLite temp files so they run with zero external infrastructure.

Run with::

    .venv/Scripts/python.exe -m pytest tests/test_worker_queue.py
    .venv/Scripts/python.exe tests/test_worker_queue.py   # standalone
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker_queue import Queue, LeaseLost, DONE, FAILED, RETRYABLE, LEASED


def _queue():
    tmp = tempfile.mkdtemp()
    return Queue(f"sqlite:///{os.path.join(tmp, 'q.sqlite3')}")


def test_enqueue_creates_pending_jobs():
    q = _queue()
    created = q.enqueue_batches(
        [{"tile_ids": ["a", "b"], "zoom": 9}, {"tile_ids": ["c"], "zoom": 12}]
    )
    assert len(created) == 2
    assert created[0]["status"] == "pending"
    assert created[0]["tile_ids"] == ["a", "b"]
    assert q.stats()["pending"] == 2
    # empty batches are skipped
    assert q.enqueue_batches([{"tile_ids": [], "zoom": 9}]) == []


def test_atomic_claiming_no_double_handout():
    q = _queue()
    n = 30
    q.enqueue_batches([{"tile_ids": [f"t{i}"], "zoom": 9} for i in range(n)])

    claimed = []
    lock = threading.Lock()

    def worker(name):
        while True:
            job = q.claim(name, lease_seconds=60)
            if job is None:
                return
            with lock:
                claimed.append(job["batch_id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n, f"claimed {len(claimed)} of {n}"
    assert len(set(claimed)) == n, "a batch was handed out twice!"


def test_expired_lease_is_reclaimable():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])

    a = q.claim("workerA", lease_seconds=60, now=1000.0)
    assert a["claimed_by"] == "workerA" and a["status"] == LEASED
    # While the lease is live, no one else can claim it.
    assert q.claim("workerB", now=1001.0) is None

    # Simulate the lease deadline passing; the job becomes claimable again.
    b = q.claim("workerB", lease_seconds=60, now=2000.0)
    assert b is not None and b["claimed_by"] == "workerB"
    assert b["attempts"] == 2  # both claims counted


def test_reclaim_expired_marks_retryable():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])
    q.claim("A", lease_seconds=10, now=1000.0)
    reclaimed = q.reclaim_expired(now=2000.0)
    assert reclaimed == 1
    job = q.list_jobs()[0]
    assert job["status"] == RETRYABLE and job["claimed_by"] is None


def test_complete_sets_done_and_artifact():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])
    job = q.claim("A", now=1.0)
    done = q.complete_batch(
        job["batch_id"], "A",
        artifact_path="/data/raw/queue/x.jsonl",
        artifact_id="art-1",
        result_meta={"tiles_ok": 1, "tankers": 3},
    )
    assert done["status"] == DONE
    assert done["artifact_path"] == "/data/raw/queue/x.jsonl"
    assert done["artifact_id"] == "art-1"
    assert done["result_meta"] == {"tiles_ok": 1, "tankers": 3}
    assert done["completed_at"] is not None
    # ISO view for the API
    assert q.public(done)["completed_at"].startswith("19") or \
        q.public(done)["completed_at"].startswith("20")


def test_fail_retryable_then_failed_when_exhausted():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}], max_attempts=2)

    j1 = q.claim("A", now=1.0)
    f1 = q.fail_batch(j1["batch_id"], "A", error_summary="boom", retryable=True, now=2.0)
    assert f1["status"] == RETRYABLE and f1["attempts"] == 1

    j2 = q.claim("A", now=3.0)  # attempts now == max_attempts
    f2 = q.fail_batch(j2["batch_id"], "A", error_summary="boom2", retryable=True, now=4.0)
    assert f2["status"] == FAILED and f2["attempts"] == 2


def test_non_retryable_fail_is_terminal():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}], max_attempts=5)
    j = q.claim("A", now=1.0)
    f = q.fail_batch(j["batch_id"], "A", error_summary="fatal", retryable=False, now=2.0)
    assert f["status"] == FAILED


def test_wave_reads_and_active_counts_are_not_limited():
    q = _queue()
    wave_a = q.enqueue_batches(
        [{"tile_ids": [f"a{i}"], "zoom": 9} for i in range(120)],
        enqueue_id="wave-a",
    )
    q.enqueue_batches(
        [{"tile_ids": ["b"], "zoom": 12}],
        enqueue_id="wave-b",
    )

    assert len(q.list_enqueue_jobs("wave-a")) == 120
    assert q.enqueue_stats("wave-a")["pending"] == 120
    assert q.count_active_jobs() == 121
    assert q.count_active_jobs(exclude_enqueue_id="wave-a") == 1

    claimed = q.claim("worker", now=1.0)
    assert claimed["batch_id"] == wave_a[0]["batch_id"]
    q.complete_batch(claimed["batch_id"], "worker", now=2.0)
    assert q.enqueue_stats("wave-a")["done"] == 1
    assert q.enqueue_stats("wave-a")["pending"] == 119


def test_lease_guards_reject_non_owner():
    q = _queue()
    q.enqueue_batches([{"tile_ids": ["t1"], "zoom": 9}])
    job = q.claim("A", now=1.0)
    for action in (
        lambda: q.heartbeat_batch(job["batch_id"], "B"),
        lambda: q.complete_batch(job["batch_id"], "B"),
        lambda: q.fail_batch(job["batch_id"], "B"),
    ):
        try:
            action()
            assert False, "expected LeaseLost"
        except LeaseLost:
            pass


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
    print(f"\n{'OK' if not failures else 'FAILED'} "
          f"({len([n for n in globals() if n.startswith('test_')])} tests, "
          f"{failures} failures)")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
