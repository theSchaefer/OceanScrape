"""Global tile capture/ingest must preserve owned detector outputs."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper_global
import update_database
from marker_dedup import dedup_markers_across_tiles


DUPLICATE_MARKERS = [
    {"lat": 51.0000, "lon": 4.0000, "type": "cargo", "motion": "stationary"},
    {"lat": 51.0001, "lon": 4.0001, "type": "cargo", "motion": "moving"},
]


def test_capture_ownership_does_not_spatially_dedup():
    original = scraper_global.GLOBAL_TILE_INDEX

    class FakeIndex:
        def filter_markers_for_tile(self, tile_id, markers):
            assert tile_id == "tile-a"
            return list(markers), 0

    scraper_global.GLOBAL_TILE_INDEX = FakeIndex()
    try:
        accepted, rejected = scraper_global._owned_markers_for_tile(
            "tile-a", DUPLICATE_MARKERS
        )
    finally:
        scraper_global.GLOBAL_TILE_INDEX = original

    assert accepted == DUPLICATE_MARKERS
    assert len(accepted) == 2
    assert rejected == 0


def test_tile_row_counts_all_markers_without_dedup():
    entry = {
        "capture_type": "tile",
        "tile_id": "g_z9_r0_c0",
        "tile": {
            "tile_id": "g_z9_r0_c0",
            "zoom": 9,
            "row": 0,
            "col": 0,
            "center_lat": 0,
            "center_lon": 0,
            "tile_bounds": {},
            "capture_bounds": {},
            "owner_bounds_px": {},
            "capture_bounds_px": {},
        },
        "date_time": "2026-06-09-20-10-24-123456",
        "status": "success",
        "markers": DUPLICATE_MARKERS,
    }
    row = update_database._tile_entry_to_row(entry, list(entry["markers"]))
    assert row[10] == 0  # tankers
    assert row[11] == 2  # cargos
    assert row[13] == 1  # moving cargos
    assert row[14].adapted == DUPLICATE_MARKERS


def test_global_marker_insert_keeps_exact_duplicates():
    class Cursor:
        def __init__(self):
            self.params = []
            self.rowcount = 1

        def execute(self, _sql, params):
            self.params.append(params)

    cursor = Cursor()
    inserted = update_database._insert_global_markers(
        cursor,
        123,
        "tile-a",
        update_database._parse_datetime("2026-06-09-20-10-24-123456"),
        [DUPLICATE_MARKERS[0], dict(DUPLICATE_MARKERS[0])],
    )
    assert inserted == 2
    assert len(cursor.params) == 2
    assert cursor.params[0][3:] == cursor.params[1][3:]


def test_wave_dedup_only_collapses_markers_from_different_tiles():
    markers = [
        {
            "lat": 51.0, "lon": 4.0, "type": "cargo",
            "motion": "stationary", "tile_id": "z12-a", "zoom": 12,
        },
        {
            "lat": 51.0001, "lon": 4.0001, "type": "cargo",
            "motion": "stationary", "tile_id": "z12-a", "zoom": 12,
        },
        {
            "lat": 51.0001, "lon": 4.0001, "type": "cargo",
            "motion": "moving", "tile_id": "z9-b", "zoom": 9,
        },
    ]
    deduped = dedup_markers_across_tiles(markers, 0.003)
    assert len(deduped) == 2
    assert {marker["tile_id"] for marker in deduped} == {"z12-a"}
    assert deduped[0]["motion"] == "moving"


def test_tile_row_carries_queue_provenance():
    entry = {
        "capture_type": "tile",
        "tile_id": "g_z9_r0_c0",
        "zoom": 9,
        "center_lat": 0,
        "center_lon": 0,
        "tile_bounds": {},
        "capture_bounds": {},
        "owner_bounds_px": {},
        "capture_bounds_px": {},
        "date_time": "2026-06-09-20-10-24-123456",
        "wave_id": "wave-1",
        "enqueue_id": "enqueue-1",
        "batch_id": "batch-1",
        "worker_id": "worker-1",
        "markers": [],
    }
    row = update_database._tile_entry_to_row(entry, [])
    assert row[:6] == (
        "g_z9_r0_c0",
        "wave-1",
        "enqueue-1",
        "batch-1",
        "worker-1",
        update_database._parse_datetime(entry["date_time"]),
    )


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
