"""Unit tests for global-tile snake ordering and pan/navigation decisions.

These cover the regression that made sparse z12 tiles cost thousands of
seconds of pure navigation:

  * the manifest / selection must stay in snake (boustrophedon) order so
    sequential navigation makes small local hops, and
  * far viewport jumps must fall back to a URL load instead of hundreds of
    8K mouse-drag steps.

Run with::

    .venv/Scripts/python.exe -m pytest tests/test_tile_navigation.py
    .venv/Scripts/python.exe tests/test_tile_navigation.py   # standalone
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from global_tile_grid import build_global_tile_manifest, tile_scan_key
import scraper_global as s


def _tile(zoom, row, col):
    return {
        "zoom": zoom,
        "row": row,
        "col": col,
        "tile_id": f"g_z{zoom}_r{row}_c{col}",
    }


def _has_no_snake_violation(tiles):
    """Within each (zoom,row) run, even rows must scan west->east and odd rows
    east->west (i.e. col is monotone in the row-parity direction)."""
    for a, b in zip(tiles, tiles[1:]):
        if a["zoom"] == b["zoom"] and a["row"] == b["row"]:
            if a["row"] % 2 == 0 and b["col"] < a["col"]:
                return False
            if a["row"] % 2 == 1 and b["col"] > a["col"]:
                return False
    return True


# --- tile_scan_key ------------------------------------------------------------

def test_tile_scan_key_even_row_scans_east():
    row = [_tile(12, 4, c) for c in (3, 1, 2, 0)]  # even row
    ordered = sorted(row, key=tile_scan_key)
    assert [t["col"] for t in ordered] == [0, 1, 2, 3]


def test_tile_scan_key_odd_row_scans_west():
    row = [_tile(12, 5, c) for c in (0, 2, 1, 3)]  # odd row
    ordered = sorted(row, key=tile_scan_key)
    assert [t["col"] for t in ordered] == [3, 2, 1, 0]


def test_tile_scan_key_groups_by_zoom_then_row():
    tiles = [_tile(12, 1, 9), _tile(9, 7, 1), _tile(12, 0, 5), _tile(9, 7, 0)]
    ordered = sorted(tiles, key=tile_scan_key)
    # zoom ascending first, then row ascending
    assert [(t["zoom"], t["row"]) for t in ordered] == [
        (9, 7), (9, 7), (12, 0), (12, 1)
    ]


def test_tile_scan_key_is_boustrophedon():
    # A full 3x4 block across two rows: even row L->R, odd row R->L.
    block = [_tile(10, r, c) for r in (0, 1) for c in (0, 1, 2, 3)]
    # shuffle deterministically
    block = list(reversed(block))
    ordered = sorted(block, key=tile_scan_key)
    cols = [t["col"] for t in ordered]
    assert cols == [0, 1, 2, 3, 3, 2, 1, 0]


# --- manifest -----------------------------------------------------------------

def test_manifest_is_snake_sorted():
    tiles = build_global_tile_manifest(s.VIEWPORT_WIDTH, s.VIEWPORT_HEIGHT)
    assert tiles == sorted(tiles, key=tile_scan_key)
    assert _has_no_snake_violation(tiles)


# --- _select_global_tiles -----------------------------------------------------

def test_select_global_tiles_preserves_snake_order():
    sel = s._select_global_tiles(respect_schedule=False)
    assert sel, "manifest selection should not be empty"
    assert sel == sorted(sel, key=tile_scan_key)
    assert _has_no_snake_violation(sel)


# --- _chunk_global_tiles ------------------------------------------------------

def test_chunk_keeps_zoom_order_and_within_zoom_order():
    # Snake-ordered input spanning two zooms.
    z9 = sorted([_tile(9, r, c) for r in (0, 1) for c in range(5)],
                key=tile_scan_key)
    z12 = sorted([_tile(12, r, c) for r in (0, 1) for c in range(5)],
                 key=tile_scan_key)
    tiles = z9 + z12

    batches = s._chunk_global_tiles(tiles)

    # Every batch is single-zoom.
    assert all(len({t["zoom"] for t in b}) == 1 for b in batches)
    # Batch zoom order is non-decreasing (z9 batches before z12 batches).
    batch_zooms = [b[0]["zoom"] for b in batches]
    assert batch_zooms == sorted(batch_zooms)
    # Flattening the batches reproduces the input order exactly (no reshuffle).
    flat = [t for b in batches for t in b]
    assert flat == tiles


def test_chunk_respects_batch_size():
    tiles = sorted([_tile(9, r, c) for r in range(6) for c in range(6)],
                   key=tile_scan_key)
    batches = s._chunk_global_tiles(tiles)
    assert all(len(b) <= s.GLOBAL_TILE_BATCH_SIZE for b in batches)


# --- _plan_navigation ---------------------------------------------------------

def test_plan_navigation_zero_move():
    plan = s._plan_navigation(10.0, 20.0, 10.0, 20.0, 12)
    assert plan["steps_needed"] == 0
    assert plan["mode"] == "mouse-drag"


def test_plan_navigation_near_is_mouse_drag():
    # A few hundred metres at z12 -> a handful of drag steps.
    saved_threshold = s.URL_NAV_MAX_DRAG_STEPS
    try:
        # Production may intentionally set 0 for URL-hopping-only. This test
        # verifies the planner's normal near-drag behavior independently.
        s.URL_NAV_MAX_DRAG_STEPS = 24
        plan = s._plan_navigation(10.0, 20.0, 10.0, 20.05, 12)
        assert plan["steps_needed"] >= 1
        assert plan["steps_needed"] <= s.URL_NAV_MAX_DRAG_STEPS
        assert plan["mode"] == "mouse-drag"
    finally:
        s.URL_NAV_MAX_DRAG_STEPS = saved_threshold


def test_plan_navigation_far_is_url_load():
    # Most of the way around the globe -> thousands of steps would be needed.
    plan = s._plan_navigation(0.0, -170.0, 0.0, 170.0, 12)
    assert plan["steps_needed"] > s.URL_NAV_MAX_DRAG_STEPS
    assert plan["mode"] == "url-load"


def test_plan_navigation_threshold_boundary():
    # Pin the constants so we can land exactly on / just past the threshold.
    saved_max_drag = s.MAX_DRAG_PX
    saved_threshold = s.URL_NAV_MAX_DRAG_STEPS
    try:
        s.MAX_DRAG_PX = 800
        s.URL_NAV_MAX_DRAG_STEPS = 10

        # drag_x in px = dlon * (256 * 2**zoom) / 360. Solve dlon for a target
        # step count: steps = ceil(|drag_x| / MAX_DRAG_PX).
        def dlon_for_px(px, zoom):
            return px * 360.0 / (256 * (2 ** zoom))

        # 7600 px -> ceil(7600/800) = 10 steps -> at threshold -> mouse-drag.
        # (Mid-band values avoid float rounding at exact step multiples.)
        at = s._plan_navigation(0.0, 0.0, 0.0, dlon_for_px(7600, 12), 12)
        assert at["steps_needed"] == 10
        assert at["mode"] == "mouse-drag"

        # 8400 px -> ceil(8400/800) = 11 steps -> just over -> url-load.
        over = s._plan_navigation(0.0, 0.0, 0.0, dlon_for_px(8400, 12), 12)
        assert over["steps_needed"] == 11
        assert over["mode"] == "url-load"
    finally:
        s.MAX_DRAG_PX = saved_max_drag
        s.URL_NAV_MAX_DRAG_STEPS = saved_threshold


# --- standalone runner --------------------------------------------------------

if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
    sys.exit(1 if failures else 0)
