"""Continuously enqueue non-overlapping global capture waves.

The worker processes and control-plane API remain separate long-running
services. This control-plane orchestrator keeps exactly one owned wave active:
when every batch in that wave is terminal, it immediately enqueues the next
wave. State is written before enqueue so a process restart can safely resume
without duplicating a wave.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import tempfile
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from worker_enqueue import build_batch_payloads  # noqa: E402
from worker_queue import Queue, TERMINAL_STATES, default_dsn  # noqa: E402

logger = logging.getLogger("continuous")


def _atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _read_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        state = {}
    state.setdefault("version", 1)
    state.setdefault("waves_started", 0)
    state.setdefault("waves_completed", 0)
    state.setdefault("current_enqueue_id", None)
    return state


def summarize_wave(jobs: list[dict], *, require_auto_ingest=True) -> dict:
    by_status = {}
    tiles_ok = tiles_failed = 0
    ingest_failures = []
    for job in jobs:
        status = job["status"]
        by_status[status] = by_status.get(status, 0) + 1
        meta = job.get("result_meta") or {}
        tiles_ok += int(meta.get("tiles_ok", 0) or 0)
        tiles_failed += int(meta.get("tiles_failed", 0) or 0)
        if status == "done" and require_auto_ingest:
            ingest = meta.get("ingest")
            if not isinstance(ingest, dict):
                ingest_failures.append({
                    "batch_id": job["batch_id"],
                    "error": "missing auto-ingest result",
                })
            elif ingest.get("ok") is not True:
                ingest_failures.append({
                    "batch_id": job["batch_id"],
                    "error": ingest.get("error", "unknown ingest error"),
                })
    return {
        "jobs": len(jobs),
        "by_status": by_status,
        "tiles_ok": tiles_ok,
        "tiles_failed": tiles_failed,
        "ingest_failures": ingest_failures,
        "terminal": bool(jobs) and all(
            job["status"] in TERMINAL_STATES for job in jobs
        ),
    }


class ContinuousQueueRunner:
    def __init__(self, queue: Queue, payload_factory, *, state_path: Path,
                 poll_seconds=2.0, max_attempts=None,
                 continue_on_wave_failure=False, require_auto_ingest=True):
        self.queue = queue
        self.payload_factory = payload_factory
        self.state_path = Path(state_path)
        self.poll_seconds = float(poll_seconds)
        self.max_attempts = max_attempts
        self.continue_on_wave_failure = continue_on_wave_failure
        self.require_auto_ingest = require_auto_ingest
        self.stop = threading.Event()

    def request_stop(self):
        self.stop.set()

    def _save(self, state):
        state["updated_at"] = time.time()
        _atomic_write_json(self.state_path, state)

    def _start_wave(self, state):
        payloads, tile_count, by_zoom = self.payload_factory()
        if not payloads:
            logger.warning("No tiles selected; retrying after %.1fs",
                           self.poll_seconds)
            return False

        enqueue_id = f"enq_cont_{uuid.uuid4().hex}"
        wave_number = int(state["waves_started"]) + 1
        state.update({
            "current_enqueue_id": enqueue_id,
            "current_wave_number": wave_number,
            "current_started_at": time.time(),
            "current_tile_count": int(tile_count),
            "current_by_zoom": by_zoom,
            "waves_started": wave_number,
        })
        # Persist the chosen id first. If the process dies before enqueue, the
        # empty wave is reconstructed with the same id after restart.
        self._save(state)
        created = self.queue.enqueue_batches(
            payloads,
            max_attempts=self.max_attempts,
            source=f"continuous-wave-{wave_number}",
            enqueue_id=enqueue_id,
        )
        logger.info(
            "Started wave %d: enqueue_id=%s batches=%d tiles=%d by_zoom=%s",
            wave_number, enqueue_id, len(created), tile_count, by_zoom,
        )
        return True

    def _recover_empty_wave(self, state):
        payloads, tile_count, by_zoom = self.payload_factory()
        if not payloads:
            return False
        enqueue_id = state["current_enqueue_id"]
        created = self.queue.enqueue_batches(
            payloads,
            max_attempts=self.max_attempts,
            source=f"continuous-wave-{state['current_wave_number']}",
            enqueue_id=enqueue_id,
        )
        state["current_tile_count"] = int(tile_count)
        state["current_by_zoom"] = by_zoom
        self._save(state)
        logger.warning("Recovered empty wave %s with %d batches",
                       enqueue_id, len(created))
        return True

    def run(self, *, max_completed_waves=None):
        state = _read_state(self.state_path)
        while not self.stop.is_set():
            enqueue_id = state.get("current_enqueue_id")
            if enqueue_id:
                jobs = self.queue.list_enqueue_jobs(enqueue_id)
                if not jobs:
                    self._recover_empty_wave(state)
                    self.stop.wait(self.poll_seconds)
                    continue

                summary = summarize_wave(
                    jobs, require_auto_ingest=self.require_auto_ingest
                )
                if not summary["terminal"]:
                    self.stop.wait(self.poll_seconds)
                    continue

                state["waves_completed"] = int(state["waves_completed"]) + 1
                state["last_enqueue_id"] = enqueue_id
                state["last_completed_at"] = time.time()
                state["last_summary"] = summary
                state["current_enqueue_id"] = None
                self._save(state)
                logger.info("Completed wave %s: %s", enqueue_id, summary)

                has_batch_failure = bool(summary["by_status"].get("failed"))
                has_ingest_failure = bool(summary["ingest_failures"])
                if ((has_batch_failure or has_ingest_failure)
                        and not self.continue_on_wave_failure):
                    logger.error(
                        "Stopping after unhealthy wave: failed_batches=%d "
                        "ingest_failures=%d",
                        summary["by_status"].get("failed", 0),
                        len(summary["ingest_failures"]),
                    )
                    return 1
                if (max_completed_waves is not None
                        and state["waves_completed"] >= max_completed_waves):
                    return 0
                continue

            foreign_active = self.queue.count_active_jobs()
            if foreign_active:
                logger.info(
                    "Waiting for %d pre-existing active queue job(s)",
                    foreign_active,
                )
                self.stop.wait(self.poll_seconds)
                continue

            if not self._start_wave(state):
                self.stop.wait(self.poll_seconds)

        logger.info("Stop requested; no new wave will be enqueued")
        return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py continuous",
        description=(
            "Keep one capture wave active and enqueue the next immediately "
            "after completion."
        ),
    )
    parser.add_argument("--regions", default="global")
    parser.add_argument("--zoom", default=None)
    parser.add_argument("--tier", default=None)
    parser.add_argument("--tile-ids", default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--queue-dsn", default=None)
    parser.add_argument("--poll-seconds", type=float,
                        default=float(os.getenv(
                            "CONTINUOUS_POLL_SECONDS", "2"
                        )))
    parser.add_argument(
        "--state-file",
        default=os.getenv(
            "CONTINUOUS_STATE_FILE",
            "data/continuous/state.json",
        ),
    )
    parser.add_argument(
        "--lock-file",
        default=os.getenv(
            "CONTINUOUS_LOCK_FILE",
            "data/continuous/orchestrator.lock",
        ),
    )
    parser.add_argument(
        "--continue-on-wave-failure",
        action="store_true",
        help="Start another wave even if a batch or auto-ingest failed",
    )
    parser.add_argument(
        "--allow-no-auto-ingest",
        action="store_true",
        help="Allow startup when WORKER_API_AUTO_INGEST is not 1",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] continuous: %(message)s",
    )
    if (os.getenv("WORKER_API_AUTO_INGEST", "0") != "1"
            and not args.allow_no_auto_ingest):
        logger.error(
            "WORKER_API_AUTO_INGEST must be 1 for continuous operation "
            "(or pass --allow-no-auto-ingest)"
        )
        return 2

    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another continuous orchestrator holds %s", lock_path)
        lock_handle.close()
        return 3

    def payload_factory():
        payloads, tiles, by_zoom = build_batch_payloads(
            regions=args.regions,
            zoom=args.zoom,
            tier=args.tier,
            tile_ids=args.tile_ids,
            batch_size=args.batch_size,
            due_only=False,
        )
        return payloads, len(tiles), by_zoom

    runner = ContinuousQueueRunner(
        Queue(args.queue_dsn or default_dsn()),
        payload_factory,
        state_path=Path(args.state_file),
        poll_seconds=args.poll_seconds,
        max_attempts=args.max_attempts,
        continue_on_wave_failure=args.continue_on_wave_failure,
        require_auto_ingest=not args.allow_no_auto_ingest,
    )

    def handle_signal(signum, _frame):
        logger.info("Signal %s received", signum)
        runner.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)
    try:
        return runner.run()
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
