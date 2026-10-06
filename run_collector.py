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
import json
import logging
import random
import time

from src.backup import read_generation
from src.alerts import AlertService
from src.backup_service import BackupService
from src.collector import Collector
from src.settings import defend_minutes, defending
from src.config import load_config
from src.csfloat_client import CSFloatClient
from src.db import Database
from src.parallel import sweep_items
from src.logging_setup import setup_logging

log = logging.getLogger("csfloat.main")

# How often the running collector re-reads the tracked-item list from the DB.
RESYNC_SECONDS = 30.0
PLAN_SECONDS = 600.0
# How often the account's trades are read for the earnings tab.
TRADES_SECONDS = 1800.0
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
    # The whole list's plan: what it asks of the day, and how far the rest
    # tier gives way. Six thousand items' history is not read every 30 s.
    last_plan = -PLAN_SECONDS

    # Monotonic, so a clock change cannot make the defence fire in a loop.

    last_defence = [time.monotonic()]
    # First read a minute after start, not in the middle of starting up.
    last_trades = [time.monotonic() - TRADES_SECONDS + 60.0]

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
            if time.monotonic() - last_plan >= PLAN_SECONDS:
                last_plan = time.monotonic()
                try:
                    plan = collector.refresh_plan()
                    log.info("Poll plan: %.0f poll(s)/day asked, %.0f possible, "
                             "rest tier x%.2f", sum(plan["demand"].values()),
                             plan["capacity"], plan["rest_stretch"])
                except Exception as exc:  # noqa: BLE001 - a plan is not a poll
                    log.warning("Poll plan failed: %s", exc)
            collector.refresh_budget_factor()
            # Unwind the 429 backoff on the clock. Doing this only after a
            # successful poll deadlocks: a large multiplier is exactly what
            # makes successful polls rare.
            collector.maybe_speed_up()
            collector.apply_quarantine_clear()   # operator lifted it in the UI
            collector.sync_proxies()          # proxies edited in the dashboard
            collector.refresh_cny_rate()      # display-only, twice a day at most
            # Keep the dashboard honest about the live pool even during a long
            # quiet stretch between polls.
            collector.store_rate_state()
            new_names = [n for n in active if n not in scheduled]
            new_step = startup_step(max(len(active), 1))
            for i, name in enumerate(new_names):
                schedule(name, time.monotonic() + i * new_step, new_step)
                log.info("New item picked up from DB: '%s'", name)

        # Requests from the dashboard and the account's own work: an approved
        # plan, the sync, the defence, the sweeps. Ahead of the sales polls'
        # gates below, not behind them. Those gates are about the addresses
        # the anonymous sales history is read from; the plan, the sync and
        # the defence go out on the main key's own addresses and the sweeps
        # on the analysis keys', each with limits of their own. Behind the
        # gates, an approved plan sat "waiting for the collector" for as long
        # as the history addresses were spent.
        if time.monotonic() - last_manual_check >= MANUAL_POLL_CHECK_SECONDS:
            last_manual_check = time.monotonic()
            # "Poll now" is a sales poll, and waits for the history addresses
            # like any other: the flag stays set until it can run.
            history_free = (collector.quota_pause_seconds() <= 0
                            and collector.client.cooldown_remaining() <= 0)
            for row in (collector.db.pending_poll_requests()
                        if history_free else []):
                item = active.get(row["market_hash_name"])
                if item is None:
                    collector.db.clear_poll_request(int(row["id"]))
                    continue
                log.info("Manual poll requested for '%s'", item.name)
                collector.db.clear_poll_request(int(row["id"]))
                collector.poll_item(item)

            # An approved plan runs before anything else asks for quota: it
            # was approved against a book that is already minutes old.
            collector.apply_pending_actions()

            collector.flush_traffic()
            try:
                collector.check_holding_alerts()
            except Exception as exc:  # noqa: BLE001
                log.warning("Holding check failed: %s", exc)
            # Old logs and snapshots out, hourly; the file compressed on request.
            try:
                collector.maintain_db()
            except Exception as exc:  # noqa: BLE001 - housekeeping is not the bot
                log.warning("Database housekeeping failed: %s", exc)

            # "Are the orders we think we hold actually there." Asked on
            # demand, and by the defence before every pass.
            if collector.db.get_setting("orders_sync_requested") == "1":
                collector.db.set_setting("orders_sync_requested", "0")
                try:
                    # Asked for by hand, so it may go looking for the listing
                    # endpoint; the pass before each defence may not, because
                    # that one runs unattended and every probe is a request.
                    collector.sync_our_orders(discover=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Order sync failed: %s", exc)

            # Trades for the earnings tab: on demand, and every half hour on
            # its own - one or two requests while nothing new has happened.
            if (collector.db.get_setting("trades_sync_requested") == "1"
                    or time.monotonic() - last_trades[0] >= TRADES_SECONDS):
                asked = collector.db.get_setting("trades_sync_requested") == "1"
                collector.db.set_setting("trades_sync_requested", "0")
                last_trades[0] = time.monotonic()
                try:
                    collector.sync_trades(discover=asked)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Trades sync failed: %s", exc)

            # Defence on its own clock. Off unless turned on, and it may only
            # amend or withdraw - opening a position stays a decision made by
            # hand, because a loop that can also open them can spend the whole
            # budget while nobody is watching.
            if defending(collector.db):
                due = defend_minutes(collector.db) * 60.0
                if time.monotonic() - last_defence[0] >= due:
                    last_defence[0] = time.monotonic()
                    try:
                        collector.defend_orders()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Defence pass failed: %s", exc)

            # Buy-order requests, same gating: on demand, never on a schedule.
            pending = collector.db.pending_order_requests()
            if pending:
                for row in pending:
                    collector.db.clear_order_request(int(row["id"]))
                names = [row["market_hash_name"] for row in pending]
                log.info("Buy orders requested for %d item(s)", len(names))
                # Both halves: the book says who is bidding, the listings say
                # what it is going for. Scoring needs both, and with only the
                # book the exit price falls back to the sales median - which
                # is almost always higher than the cheapest ask, so every
                # ceiling comes out too high.
                #
                # Several at a time when there are keys to do it with; with
                # one key this is the same loop it replaces, one item after
                # another.
                progress = SweepProgress(collector.db, names)
                writer = _state_writer(collector)

                def report(name, result, _w=writer, _p=progress):
                    _p.finish(name, result)
                    _w(name, result)

                done = sweep_items(collector, names, report=report,
                                   on_start=progress.start)
                progress.close(done)
                collector.store_rate_state()
                # Logged whatever the count, including one worker. Written
                # only when several ran, the line's absence meant either "the
                # sweep never happened" or "it ran on one worker", and there
                # was no way to tell which - the exact ambiguity that cost
                # this project several evenings.
                log.info("Swept %d of %d item(s) on %d worker(s), %d failed",
                         len(done["swept"]), len(names), done["workers"],
                         len(done["failed"]))

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


class SweepProgress:
    """What the sweep is doing, for the status panel: how many of how many,
    which items are being read right now, and when it began and ended.
    Written as it goes - a sweep of three hundred items takes minutes, and
    "queued" alone said nothing about whether it had started."""

    KEY = "sweep_state"

    def __init__(self, db, names) -> None:
        import threading
        from src.db import utcnow_iso
        self.db = db
        self.lock = threading.Lock()
        self.state = {"started_at": utcnow_iso(), "finished_at": None,
                      "total": len(names), "done": 0, "failed": 0,
                      "current": [], "last": None}
        self.write()

    def write(self) -> None:
        try:
            self.db.set_setting(self.KEY, json.dumps(self.state, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - a display write is not the sweep
            log.debug("Could not store the sweep state: %s", exc)

    def start(self, name: str) -> None:
        with self.lock:
            self.state["current"] = (self.state["current"] + [name])[-20:]
            self.write()

    def finish(self, name: str, result: dict) -> None:
        with self.lock:
            if name in self.state["current"]:
                self.state["current"].remove(name)
            self.state["done"] += 1
            self.state["last"] = name
            self.write()

    def close(self, done: dict) -> None:
        from src.db import utcnow_iso
        with self.lock:
            self.state["done"] = len(done.get("swept", {}))
            self.state["failed"] = len(done.get("failed", {}))
            self.state["current"] = []
            self.state["finished_at"] = utcnow_iso()
            self.write()


def _state_writer(collector, every: float = 10.0):
    """A sweep of hundreds of items takes minutes, and the load page reads the
    keys' counters from the database: refresh them as items finish rather
    than only once it is all over."""
    import threading
    lock = threading.Lock()
    last = [0.0]

    def report(_name, _result) -> None:
        # A plan approved while a long sweep runs goes out between its items,
        # not after the last of them.
        try:
            collector.apply_pending_actions()
        except Exception as exc:  # noqa: BLE001 - the sweep goes on regardless
            log.warning("Applying the approved plan mid-sweep failed: %s", exc)
        now = time.monotonic()
        with lock:
            if now - last[0] < every:
                return
            last[0] = now
        try:
            collector.store_rate_state()
        except Exception as exc:  # noqa: BLE001 - a display write is not the sweep
            log.debug("Could not store the key state mid-sweep: %s", exc)

    return report


def _attach_keyring(collector) -> None:
    """Give the client a key ring when a key file is configured.

    Silent when there is none, which is the single-key setup the collector has
    always run: one clock, one address at a time, everything as before.
    """
    from src.keyring import KeyRing, read_keys

    keys = read_keys(collector.config.http.keys_file)
    if len(keys) < 2:
        return
    collector.sync_proxies()
    # The keys' own addresses when the load page lists some, the main pool
    # otherwise - and sync_proxies moves the ring between them as that changes.
    ring = KeyRing(keys, collector.key_pool_or_main(),
                   spacing=collector.config.polling.min_seconds_between_requests)
    collector.client.keyring = ring
    log.info("Key ring: %d key(s), %d route(s) each, %d sweep(s) at once",
             len(ring.live()), ring.routes_per_key, ring.concurrency())


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
    _attach_keyring(collector)

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
