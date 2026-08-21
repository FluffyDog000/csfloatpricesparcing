#!/usr/bin/env python3
"""Entrypoint: run the CSFloat sales collector.

Polls each tracked item on a randomized interval (jitter within the configured
[min, max] minutes), spreading requests over time so they do not fire as one
batch. Runs until interrupted (Ctrl+C).

Usage:
    python run_collector.py            # run forever
    python run_collector.py --once     # poll every item exactly once and exit
                                        # (handy for testing / cron)
"""
from __future__ import annotations

import argparse
import heapq
import logging
import random
import time

from src.collector import Collector
from src.config import load_config, load_items
from src.csfloat_client import CSFloatClient
from src.db import Database
from src.logging_setup import setup_logging

log = logging.getLogger("csfloat.main")


def run_once(collector: Collector, active: dict) -> None:
    for item in active.values():
        collector.poll_item(item)


def run_forever(collector: Collector, active: dict) -> None:
    """Min-heap scheduler: (next_run_monotonic, seq, name). Each item reschedules
    itself with fresh jitter after every poll."""
    now = time.monotonic()
    heap: list[tuple[float, int, str]] = []
    seq = 0

    # Spread the first poll of each item across the first interval so they do
    # not all fire at t=0.
    names = list(active.keys())
    random.shuffle(names)
    if names:
        window = collector.interval_for(list(active.values())[0])  # rough spread
        spread = max(window, 60.0)
        step = spread / len(names)
        for i, name in enumerate(names):
            heapq.heappush(heap, (now + i * step + random.uniform(0, step), seq, name))
            seq += 1

    log.info("Collector started for %d item(s). Ctrl+C to stop.", len(names))
    while True:
        run_at, _, name = heap[0]
        delay = run_at - time.monotonic()
        if delay > 0:
            # Sleep in small slices so Ctrl+C is responsive.
            time.sleep(min(delay, 5.0))
            continue
        heapq.heappop(heap)

        item = active.get(name)
        if item is None:  # removed from items.yaml at reload time
            continue
        collector.poll_item(item)

        next_delay = collector.interval_for(item)
        heapq.heappush(heap, (time.monotonic() + next_delay, seq, name))
        seq += 1
        log.info("Next poll for '%s' in %.1f min", name, next_delay / 60.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="CSFloat sales collector")
    parser.add_argument(
        "--once", action="store_true",
        help="Poll every active item once and exit (for testing / cron).",
    )
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.log_path)

    items = load_items()
    db = Database(config.db_path)
    client = CSFloatClient(config.http, config.polling)
    collector = Collector(config, db, client)

    if not client.has_credentials():
        log.warning(
            "No CSFLOAT_COOKIE or CSFLOAT_AUTHORIZATION set in .env — requests "
            "will likely be rejected (401/403). See README for how to copy them."
        )

    active = collector.sync_items(items)
    if not active:
        log.error("No active items in items.yaml — nothing to poll.")
        return 1

    try:
        if args.once:
            run_once(collector, active)
        else:
            run_forever(collector, active)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
