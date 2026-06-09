"""Tests for the continuous wave orchestrator."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from continuous_queue import ContinuousQueueRunner, summarize_wave
from worker_queue import Queue


def _queue():
    tmp = tempfile.mkdtemp()
    return Queue(f"sqlite:///{os.path.join(tmp, 'q.sqlite3')}")


def _payload_factory():
    return (
        [
            {"tile_ids": ["a", "b"], "zoom": 9},
            {"tile_ids": ["c"], "zoom": 12},
        ],
        3,
        {9: 1, 12: 1},
    )


def _complete_jobs(queue, stop, seen_waves):
    while not stop.is_set():
        job = queue.claim("test-worker", lease_seconds=30)
        if job is None:
            time.sleep(0.005)
            continue
        seen_waves.append(job["enqueue_id"])
        queue.complete_batch(
            job["batch_id"],
            "test-worker",
            result_meta={
                "tiles_ok": len(job["tile_ids"]),
                "tiles_failed": 0,
                "ingest": {"ok": True, "inserted": len(job["tile_ids"])},
            },
        )


def test_runner_starts_next_wave_only_after_previous_is_terminal():
    q = _queue()
    root = Path(tempfile.mkdtemp())
    runner = ContinuousQueueRunner(
        q, _payload_factory,
        state_path=root / "state.json",
        poll_seconds=0.01,
    )
    worker_stop = threading.Event()
    seen_waves = []
    worker = threading.Thread(
        target=_complete_jobs, args=(q, worker_stop, seen_waves), daemon=True,
    )
    worker.start()
    try:
        assert runner.run(max_completed_waves=2) == 0
    finally:
        worker_stop.set()
        worker.join(timeout=1)

    state = json.loads((root / "state.json").read_text())
    assert state["waves_started"] == 2
    assert state["waves_completed"] == 2
    assert state["last_summary"]["tiles_ok"] == 3
    # FIFO claims mean all jobs from wave 1 were seen before wave 2 existed.
    transitions = [
        wave for index, wave in enumerate(seen_waves)
        if index == 0 or wave != seen_waves[index - 1]
    ]
    assert len(transitions) == 2


def test_runner_recovers_state_written_before_enqueue():
    q = _queue()
    root = Path(tempfile.mkdtemp())
    state_path = root / "state.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "waves_started": 1,
        "waves_completed": 0,
        "current_enqueue_id": "enq_cont_recover",
        "current_wave_number": 1,
    }))
    runner = ContinuousQueueRunner(
        q, _payload_factory, state_path=state_path, poll_seconds=0.01,
    )
    worker_stop = threading.Event()
    seen_waves = []
    worker = threading.Thread(
        target=_complete_jobs, args=(q, worker_stop, seen_waves), daemon=True,
    )
    worker.start()
    try:
        assert runner.run(max_completed_waves=1) == 0
    finally:
        worker_stop.set()
        worker.join(timeout=1)

    assert set(seen_waves) == {"enq_cont_recover"}
    assert len(q.list_enqueue_jobs("enq_cont_recover")) == 2


def test_unhealthy_wave_stops_before_starting_another():
    q = _queue()
    root = Path(tempfile.mkdtemp())
    runner = ContinuousQueueRunner(
        q, _payload_factory,
        state_path=root / "state.json",
        poll_seconds=0.01,
    )

    def fail_wave():
        while True:
            job = q.claim("test-worker", lease_seconds=30)
            if job is None:
                time.sleep(0.005)
                continue
            q.complete_batch(
                job["batch_id"],
                "test-worker",
                result_meta={
                    "tiles_ok": len(job["tile_ids"]),
                    "tiles_failed": 0,
                    "ingest": {"ok": False, "error": "db unavailable"},
                },
            )
            # Complete the other batch normally.
            other = q.claim("test-worker", lease_seconds=30)
            q.complete_batch(
                other["batch_id"], "test-worker",
                result_meta={"tiles_ok": 1, "ingest": {"ok": True}},
            )
            return

    thread = threading.Thread(target=fail_wave, daemon=True)
    thread.start()
    assert runner.run() == 1
    thread.join(timeout=1)
    state = json.loads((root / "state.json").read_text())
    assert state["waves_started"] == 1
    assert state["waves_completed"] == 1
    assert len(state["last_summary"]["ingest_failures"]) == 1


def test_summarize_wave_requires_all_jobs_terminal():
    summary = summarize_wave([
        {
            "batch_id": "a",
            "status": "done",
            "result_meta": {"tiles_ok": 2, "ingest": {"ok": True}},
        },
        {
            "batch_id": "b",
            "status": "pending",
            "result_meta": None,
        },
    ])
    assert summary["terminal"] is False
    assert summary["tiles_ok"] == 2


def test_summarize_wave_flags_missing_auto_ingest_result():
    summary = summarize_wave([{
        "batch_id": "a",
        "status": "done",
        "result_meta": {"tiles_ok": 1},
    }])
    assert summary["terminal"] is True
    assert summary["ingest_failures"] == [{
        "batch_id": "a",
        "error": "missing auto-ingest result",
    }]
    relaxed = summarize_wave(
        [{
            "batch_id": "a",
            "status": "done",
            "result_meta": {"tiles_ok": 1},
        }],
        require_auto_ingest=False,
    )
    assert relaxed["ingest_failures"] == []


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
    raise SystemExit(1 if _run_all() else 0)
