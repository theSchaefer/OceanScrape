"""Timestamp parsing and per-tile timestamp format tests."""

import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper_global
import update_database


def test_per_tile_timestamp_has_microsecond_precision():
    value = scraper_global._utc_capture_timestamp()
    parsed = update_database._parse_datetime(value)
    assert parsed.tzinfo == timezone.utc
    assert len(value) == len("2026-06-09-20-10-24-123456")


def test_legacy_timestamp_still_parses():
    parsed = update_database._parse_datetime("2026-06-09-20-10-24")
    assert parsed.isoformat() == "2026-06-09T20:10:24+00:00"


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
