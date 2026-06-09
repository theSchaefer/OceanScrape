#!/bin/python3
"""Pipeline orchestrator and multi-mode entrypoint for OceanScrape.

Single-server / legacy usage (unchanged):
  python run.py                   # Full pipeline: scrape → raw JSONL → ingest DB
  python run.py image1 image2     # Legacy mode: run seer.py on given images

Distributed control-plane / worker usage:
  python run.py serve             # Run the worker control-plane API (worker_api)
  python run.py enqueue --regions global --batch-size 12   # Queue capture batches
  python run.py worker --server http://10.0.0.3:8081 --token-env WORKER_TOKEN

The single-server modes do not require the control plane; the distributed modes
do not change the existing raw/ingest separation — workers produce raw JSONL and
the control plane stores (and optionally ingests) it.
"""

import sys
import subprocess
from pathlib import Path

_LATEST_RUN_POINTER = Path("data/raw/runs/LATEST")
_SUBCOMMANDS = {"worker", "enqueue", "continuous", "serve"}


def legacy_mode(image_files):
    """Original behavior: run seer.py on explicit image files, pipe to DB."""
    seer = subprocess.run(
        [sys.executable, "seer.py"] + image_files,
        capture_output=True,
        text=True,
    )
    if seer.returncode != 0:
        print(seer.stdout, end="")
        sys.exit(seer.returncode)

    for line in seer.stdout.splitlines():
        # seer output: filename:is_north:date_time:tankers:cargo
        parts = line.split(":")
        if len(parts) != 5:
            continue
        file_name, is_north, date_time, tankers, cargo = parts
        from update_database import insert_capture
        insert_capture({
            "filepath": file_name,
            "filename": file_name,
            "is_north": is_north == "True",
            "date_time": date_time,
            "status": "success",
            "zoom": None,
            "file_size_kb": 0,
        })


def _pipeline():
    """Full pipeline: run the scraper, then ingest the just-written raw run."""
    # Imported here (not at module top) so the distributed subcommands and their
    # --help work on hosts without the Postgres stack installed.
    from update_database import ingest_file, process_log

    print("Running global scraper...")
    result = subprocess.run([sys.executable, "scraper_global.py"], text=True)
    if result.returncode != 0:
        print("Scraper failed")
        sys.exit(result.returncode)

    # The scraper writes a per-run raw file and records its path in the LATEST
    # pointer. Ingest that specific run rather than a shared log.
    if _LATEST_RUN_POINTER.exists():
        run_file = _LATEST_RUN_POINTER.read_text(encoding="utf-8").strip()
        print(f"\nIngesting raw run into database: {run_file}")
        ingest_file(run_file)
    else:
        print("\nNo run pointer found; falling back to legacy captures log...")
        process_log()


def _serve_main(argv):
    """Run the worker control-plane API via uvicorn."""
    import argparse
    import os

    p = argparse.ArgumentParser(
        prog="run.py serve",
        description="Run the OceanScrape worker control-plane API.",
    )
    p.add_argument("--host", default=os.getenv("WORKER_API_HOST", "127.0.0.1"),
                   help="Bind address (default env WORKER_API_HOST or 127.0.0.1; "
                        "use a private IP like 10.0.0.3 for a Hetzner network)")
    p.add_argument("--port", type=int,
                   default=int(os.getenv("WORKER_API_PORT", "8081")))
    p.add_argument("--reload", action="store_true", help="Auto-reload (dev only)")
    args = p.parse_args(argv)

    import uvicorn
    print(f"Starting worker control plane on {args.host}:{args.port}")
    uvicorn.run("worker_api:app", host=args.host, port=args.port,
                reload=args.reload)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in _SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        if cmd == "worker":
            from worker import main as worker_main
            return worker_main(rest)
        if cmd == "enqueue":
            from worker_enqueue import main as enqueue_main
            return enqueue_main(rest)
        if cmd == "continuous":
            from continuous_queue import main as continuous_main
            return continuous_main(rest)
        if cmd == "serve":
            return _serve_main(rest)

    if argv:
        # Legacy mode: explicit image files passed as args
        legacy_mode(argv)
    else:
        # Full pipeline: run scraper then process captures log into DB
        _pipeline()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
