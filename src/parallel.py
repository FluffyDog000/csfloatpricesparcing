"""Sweep several items at once, one per API key.

A sweep is a burst of requests against one address, and the bot has always run
them one item after another: with a single key that is the only shape possible,
since every request waits out the same 2.5 second gap. Three hundred items at
roughly twenty-five requests each is over five hours that way, which is why
"look at the whole list a few times a day" has never been on offer.

With a key ring each key has its own clock, its own two-to-four addresses and
its own cooldown, so the items can go in parallel - as many at a time as there
are keys that can currently speak. The work itself is unchanged: each worker
calls the same `sweep_both_sides` the sequential path calls.

What makes it safe is the three steps before this one. The database hands each
thread its own connection; a pinned address is private to its thread and not
offered to another; and a refusal holds back the key that drew it rather than
every key at once. Without those this module would be a way to draw the
account-level complaint faster.

With one key it degrades to exactly what it replaces: one worker, one item at
a time.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Sequence

log = logging.getLogger("csfloat.parallel")

# A ceiling on workers regardless of how many keys exist. Each worker waits on
# the network most of the time, so a worker per key is what keeps every key
# busy; past a couple of hundred the threads only add contention.
MAX_WORKERS = 200


def workers_for(collector, wanted: int | None = None) -> int:
    """How many sweeps to run at once.

    Never more than there are items to sweep, never more than there are keys
    that can speak, and never more than MAX_WORKERS. With no ring the answer
    is one, which is the behaviour this replaces.
    """
    ring = getattr(getattr(collector, "client", None), "keyring", None)
    if ring is None:
        return 1
    limit = min(ring.concurrency(), MAX_WORKERS)
    return max(1, min(limit, wanted) if wanted else limit)


def sweep_items(collector, names: Sequence[str],
                report: Callable[[str, dict], None] | None = None,
                workers: int | None = None) -> dict[str, Any]:
    """Sweep both sides of each item, several at a time.

    One worker per key, each taking the next item as it frees up rather than
    being handed a fixed share: items differ by a factor of three in cost -
    fifteen requests for a Minimal Wear range against thirty-eight for a
    Field-Tested one - and a fixed split leaves workers idle at the end.

    An item that raises is recorded and the rest carry on. A sweep dying takes
    its requests with it either way; taking the other workers too would waste
    the quota they had already spent.
    """
    out: dict[str, Any] = {"swept": {}, "failed": {}, "workers": 0}
    if not names:
        return out

    count = workers or workers_for(collector, len(names))
    out["workers"] = count
    lock = threading.Lock()

    def one(name: str) -> None:
        item_id = collector.db.get_item_id(name)
        if item_id is None:
            with lock:
                out["failed"][name] = "не отслеживается"
            return
        try:
            result = collector.sweep_both_sides(name, int(item_id))
        except Exception as exc:  # noqa: BLE001 - one item is not the run
            log.warning("Sweep of '%s' failed: %s", name, exc)
            with lock:
                out["failed"][name] = f"{type(exc).__name__}: {exc}"
            return
        with lock:
            out["swept"][name] = result
        if report:
            report(name, result)

    if count == 1:
        for name in names:
            one(name)
        return out

    with ThreadPoolExecutor(max_workers=count,
                            thread_name_prefix="sweep") as pool:
        futures = [pool.submit(one, name) for name in names]
        for future in as_completed(futures):
            # `one` swallows its own failures; anything arriving here escaped
            # the worker entirely and would otherwise vanish silently.
            exc = future.exception()
            if exc is not None:  # pragma: no cover - defensive
                log.error("Sweep worker died: %s", exc)
    return out
