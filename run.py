#!/bin/python3
"""Pipeline orchestrator: scraper → OpenCV ship detection → SQLite database.

Usage:
  python run.py                  # Run scraper then update DB from captures log
  python run.py image1 image2    # Legacy mode: run seer.py on given images
"""

import sys
import subprocess

from update_database import process_log


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


def main():
    if len(sys.argv) > 1:
        # Legacy mode: explicit image files passed as args
        legacy_mode(sys.argv[1:])
    else:
        # Full pipeline: run scraper then process captures log into DB
        print("Running global scraper...")
        result = subprocess.run(
            [sys.executable, "scraper_global.py"],
            text=True,
        )
        if result.returncode != 0:
            print("Scraper failed")
            sys.exit(result.returncode)

        print("\nUpdating database from captures log...")
        process_log()


if __name__ == "__main__":
    main()
