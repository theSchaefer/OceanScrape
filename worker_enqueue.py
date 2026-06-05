"""Enqueue capture batches into the control-plane queue.

Turns the scraper's existing tile-selection parameters (regions / zoom / tier /
tile-ids) into queue jobs that distributed workers can claim. Reuses
``scraper_global``'s deterministic tile manifest and batching so the batches a
worker receives are identical to what a single-server run would have captured.

Run on the control plane (it needs the queue, and ``--due-only`` needs the DB)::

    python run.py enqueue --regions global --batch-size 12
    python run.py enqueue --tier 1 --zoom 9 --batch-size 8
    python run.py enqueue --due-only            # only tiles due per schedule

``scraper_global`` is imported lazily after arg-parsing so ``enqueue --help``
works even on hosts without the capture/DB stack.
"""

from __future__ import annotations

import argparse
import logging

from worker_queue import Queue, default_dsn

logger = logging.getLogger("enqueue")

# Sentinels meaning "no region filter" (capture the whole global manifest).
_ALL_TOKENS = {"", "global", "all", "*"}


def _csv(value):
    if value is None:
        return None
    parts = [v.strip() for v in value.split(",") if v.strip()]
    return parts or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py enqueue",
        description="Enqueue capture batches into the worker queue.",
    )
    p.add_argument("--regions", default=None,
                   help="Comma region codes, or 'global'/'all' for everything")
    p.add_argument("--zoom", default=None, help="Comma zoom levels, e.g. 9,12")
    p.add_argument("--tier", default=None, help="Comma tiers, e.g. 1,2 or original")
    p.add_argument("--tile-ids", default=None, help="Comma explicit tile ids")
    p.add_argument("--batch-size", type=int, default=None,
                   help="Tiles per batch (default: GLOBAL_TILE_BATCH_SIZE)")
    p.add_argument("--max-attempts", type=int, default=None,
                   help="Max claim attempts per batch before it's marked failed")
    p.add_argument("--due-only", action="store_true",
                   help="Only enqueue tiles due per the capture schedule (needs DB)")
    p.add_argument("--queue-dsn", default=None,
                   help="Override queue DSN (default env WORKER_QUEUE_DSN)")
    p.add_argument("--source", default="enqueue-cli",
                   help="Label stored on jobs for provenance")
    p.add_argument("--once", action="store_true",
                   help="Accepted for symmetry; enqueue always runs a single wave")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be enqueued without writing to the queue")
    return p


def run_enqueue(args) -> int:
    region_filter = _csv(args.regions)
    if region_filter and any(r.lower() in _ALL_TOKENS for r in region_filter):
        region_filter = None
    zoom_filter = [int(z) for z in (_csv(args.zoom) or [])] or None
    tier_filter = _csv(args.tier)
    tile_ids = _csv(args.tile_ids)

    # Heavy imports happen here, after --help/arg validation.
    import scraper_global as sg

    tiles = sg._select_global_tiles(
        region_filter=region_filter,
        zoom_filter=zoom_filter,
        tier_filter=tier_filter,
        tile_ids=tile_ids,
        respect_schedule=args.due_only,
    )
    if not tiles:
        logger.warning("No tiles selected; nothing to enqueue.")
        return 0

    if args.batch_size:
        sg.GLOBAL_TILE_BATCH_SIZE = int(args.batch_size)
    batches = sg._chunk_global_tiles(tiles)

    payloads = [
        {"tile_ids": [t["tile_id"] for t in batch], "zoom": int(batch[0]["zoom"])}
        for batch in batches
    ]

    by_zoom = {}
    for b in payloads:
        by_zoom[b["zoom"]] = by_zoom.get(b["zoom"], 0) + 1

    if args.dry_run:
        print(f"[dry-run] would enqueue {len(payloads)} batches "
              f"({len(tiles)} tiles) by_zoom_batches={by_zoom}")
        for b in payloads[:10]:
            print(f"  z{b['zoom']} {len(b['tile_ids'])} tiles: "
                  f"{', '.join(b['tile_ids'][:4])}{'...' if len(b['tile_ids']) > 4 else ''}")
        if len(payloads) > 10:
            print(f"  ... and {len(payloads) - 10} more batches")
        return 0

    dsn = args.queue_dsn or default_dsn()
    q = Queue(dsn)
    created = q.enqueue_batches(
        payloads, max_attempts=args.max_attempts, source=args.source,
    )
    enqueue_id = created[0]["enqueue_id"] if created else None
    print(f"Enqueued {len(created)} batches ({len(tiles)} tiles) into "
          f"{q.dialect} queue | enqueue_id={enqueue_id} by_zoom_batches={by_zoom}")
    logger.info("Queue stats after enqueue: %s", q.stats())
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    return run_enqueue(args)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
