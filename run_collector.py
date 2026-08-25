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
from src.alerts import AlertService
from src.backup_service import BackupService
from src.collector import Collector
from src.config import load_config
from src.csfloat_client import CSFloatClient
from src.db import Database
from src.logging_setup import setup_logging

log = logging.getLogger("csfloat.main")

# How often the running collector re-reads the tracked-item list from the DB.
RESYNC_SECONDS = 30.0
# Upper bound on the gap between first polls at startup, so a short item
# list still starts collecting immediately.
STARTUP_MAX_STEP = 20.0
# How often to look for "poll now" requests queued from the dashboard.
MANUAL_POLL_CHECK_SECONDS = 5.0


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
    collector.restore_cooldown_from_db()
    collector.seed_proxies_from_env()
    collector.sync_proxies()
    collector.restore_account_block()
    alerts = AlertService(collector.config, collector.db)

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

    def startup_step(count: int) -> float:
        """Seconds between the FIRST poll of consecutive items. A large list is
        spread across the shortest poll interval so startup doesn't open with a
        burst (which is what trips CSFloat's rate limit); a small list is capped
        at STARTUP_MAX_STEP so it still starts collecting right away."""
        window = collector.config.polling.interval_min_minutes * 60.0
        per_item = window / max(count, 1)
        return max(stagger, min(per_item, STARTUP_MAX_STEP))

    # First poll for every item, spread evenly (first one fires immediately).
    now = time.monotonic()
    names = list(active.keys())
    random.shuffle(names)
    step = startup_step(len(names))
    for i, name in enumerate(names):
        schedule(name, now + i * step, 0.0)

    log.info("Collector started for %d item(s); first pass spread over %.1f min.",
             len(names), len(names) * step / 60.0)
    last_resync = time.monotonic()
    last_cooldown_log = 0.0
    last_quota_log = 0.0
    last_manual_check = 0.0

    while True:
        # Backup service: daily Telegram export + inbound restore polling.
        backup.tick()
        # Health alerts (auth failure / stall / sustained 429) to Telegram.
        alerts.db = collector.db          # follow DB reopen after a restore
        alerts.tick(time.monotonic())

        # If the web UI restored the DB, its generation marker changed — reopen.
        gen = read_generation(collector.config.db_path)
        if gen != db_generation:
            db_generation = gen
            collector.reopen_db()

        # Periodically re-read the active item list from the DB.
        if time.monotonic() - last_resync >= RESYNC_SECONDS:
            last_resync = time.monotonic()
            active = collector.active_items()
            collector.refresh_budget_factor()
            # Unwind the 429 backoff on the clock. Doing this only after a
            # successful poll deadlocks: a large multiplier is exactly what
            # makes successful polls rare.
            collector.maybe_speed_up()
            collector.sync_proxies()          # proxies edited in the dashboard
            # Keep the dashboard honest about the live pool even during a long
            # quiet stretch between polls.
            collector.store_rate_state()
            new_names = [n for n in active if n not in scheduled]
            new_step = startup_step(max(len(active), 1))
            for i, name in enumerate(new_names):
                schedule(name, time.monotonic() + i * new_step, new_step)
                log.info("New item picked up from DB: '%s'", name)

        if not heap:
            time.sleep(min(RESYNC_SECONDS, 5.0))
            continue

        # Quota exhausted (x-ratelimit-remaining ~ 0): wait for the reset.
        quota_wait = collector.quota_pause_seconds()
        if quota_wait > 0:
            if time.monotonic() - last_quota_log >= 300.0:
                last_quota_log = time.monotonic()
                _, remaining, _ = collector.quota()
                log.warning("API quota spent (remaining=%s); waiting %.1f min for reset",
                            remaining, quota_wait / 60.0)
            time.sleep(min(quota_wait, 5.0))
            continue

        # Global 429 cooldown: hold every item until CSFloat lets us back in.
        cooling = collector.client.cooldown_remaining()
        if cooling > 0:
            if time.monotonic() - last_cooldown_log >= 60.0:
                last_cooldown_log = time.monotonic()
                log.warning("Rate-limited by CSFloat; polling paused for %.1f min",
                            cooling / 60.0)
            time.sleep(min(cooling, 5.0))
            continue

        # "Poll now" from the dashboard. Handled after the quota and cooldown
        # gates above, so a manual request never punches through a rate limit —
        # it just waits, and the flag stays set until it can run.
        if time.monotonic() - last_manual_check >= MANUAL_POLL_CHECK_SECONDS:
            last_manual_check = time.monotonic()
            for row in collector.db.pending_poll_requests():
                item = active.get(row["market_hash_name"])
                if item is None:
                    collector.db.clear_poll_request(int(row["id"]))
                    continue
                log.info("Manual poll requested for '%s'", item.name)
                collector.db.clear_poll_request(int(row["id"]))
                collector.poll_item(item)

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
