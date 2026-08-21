#!/usr/bin/env python3
"""Entrypoint: run the CSFloat sales collector.

The tracked-item list lives in the SQLite database (managed via the web
dashboard). On a fresh database the list is seeded once from items.yaml. The
collector re-reads the active items from the DB periodically, so items you
add / remove / pause / resume in the web UI take effect within ~30s without a
restart.

Polls each item on a randomized interval (jitter within the configured
[min, max] minutes), spreading requests over time. Runs until Ctrl+C.

Usage:
    python run_collector.py            # run forever
    python run_collector.py --once     # poll every active item once and exit
"""
from __future__ import annotations

import argparse
import heapq
import logging
import random
import time

from src.backup import read_generation
from src.backup_service import BackupService
from src.collector import Collector
from src.config import load_config
from src.csfloat_client import CSFloatClient
from src.db import Database
from src.logging_setup import setup_logging

log = logging.getLogger("csfloat.main")

# How often the running collector re-reads the tracked-item list from the DB.
RESYNC_SECONDS = 30.0


def run_once(collector: Collector) -> None:
    for item in collector.active_items().values():
        collector.poll_item(item)


def run_forever(collector: Collector) -> None:
    """Min-heap scheduler: (next_run_monotonic, seq, name). Each item reschedules
    itself with fresh jitter after every poll. The active set is refreshed from
    the DB every RESYNC_SECONDS so web edits apply live. Also drives the backup
    service (daily Telegram export + inbound restore)."""
    backup = BackupService(collector.config, collector)
    db_generation = read_generation(collector.config.db_path)

    heap: list[tuple[float, int, str]] = []
    seq = 0
    scheduled: set[str] = set()
    active = collector.active_items()
    stagger = max(collector.config.polling.min_seconds_between_requests, 2.0)

    def schedule(name: str, base: float, spread: float) -> None:
        nonlocal seq
        heapq.heappush(heap, (base + random.uniform(0, spread), seq, name))
        scheduled.add(name)
        seq += 1

    # Prompt first poll for every current item, staggered a few seconds apart.
    now = time.monotonic()
    names = list(active.keys())
    random.shuffle(names)
    for i, name in enumerate(names):
        schedule(name, now + i * stagger, stagger)

    log.info("Collector started for %d item(s). Ctrl+C to stop.", len(names))
    last_resync = time.monotonic()

    while True:
        # Backup service: daily Telegram export + inbound restore polling.
        backup.tick()

        # If the web UI restored the DB, its generation marker changed — reopen.
        gen = read_generation(collector.config.db_path)
        if gen != db_generation:
            db_generation = gen
            collector.reopen_db()

        # Periodically re-read the active item list from the DB.
        if time.monotonic() - last_resync >= RESYNC_SECONDS:
            last_resync = time.monotonic()
            active = collector.active_items()
            new_names = [n for n in active if n not in scheduled]
            for i, name in enumerate(new_names):
                schedule(name, time.monotonic() + i * stagger, stagger)
                log.info("New item picked up from DB: '%s'", name)

        if not heap:
            time.sleep(min(RESYNC_SECONDS, 5.0))
            continue

        run_at, _, name = heap[0]
        delay = run_at - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 5.0))
            continue
        heapq.heappop(heap)
        scheduled.discard(name)

        item = active.get(name)
        if item is None:  # removed or paused in the web UI — drop it
            log.info("Item '%s' no longer active; unscheduled.", name)
            continue
        collector.poll_item(item)

        next_delay = collector.interval_for(item)
        schedule(name, time.monotonic() + next_delay, 0.0)
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

    db = Database(config.db_path)
    client = CSFloatClient(config.http, config.polling)
    collector = Collector(config, db, client)

    if not client.has_credentials():
        log.warning(
            "No CSFLOAT_COOKIE or CSFLOAT_AUTHORIZATION set in .env — requests "
            "will likely be rejected (401/403). See README for how to copy them."
        )

    # Fresh DB → import the initial list from items.yaml (one time only).
    collector.seed_from_yaml_if_empty()

    if not collector.active_items():
        log.error("No active items in the database. Add items via the web "
                  "dashboard, or put them in items.yaml before first run.")
        return 1

    try:
        if args.once:
            run_once(collector)
        else:
            run_forever(collector)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
