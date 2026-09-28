"""Fill in the sell side: every lot's price, for items that only have a count.

The order book and the listings are read on different budgets. The book needs
the cookie and the residential route - the expensive, rate limited path - while
the listings answer to the API key on the documented endpoint. So the sell side
can be refreshed on its own, without queueing for the side that is scarce.

This exists because the `asks` column arrived after the data did. Bands written
before it hold a count and a minimum and nothing else, and the pricing reads
that minimum as the whole queue: one seller undercutting the rest sets the exit
price for a band with fifty lots behind him. Until a band is read again there is
nothing to correct it with - the prices were never stored.

Hence the default: sweep the items that are missing prices and leave the rest
alone. A band that legitimately has no lots is not missing anything, and
re-reading a book already priced spends quota to learn what is known.
"""
from __future__ import annotations

import time

from typing import Any, Callable, Iterable, Sequence

from .orders import wear_range


def needs_prices(depth: Sequence[dict[str, Any]],
                 span: tuple[float, float] | None = None) -> bool:
    """Whether this item's stored sell side is missing prices, or bands.

    Two ways to be short. A band that counted lots and kept none of their
    prices is a row written before the column existed. A band that is not
    there at all is a sweep a rate limit cut short - and that one has to be
    caught by counting against the wear range, because the rows that did get
    written look perfectly healthy on their own.

    An empty band is not evidence of either: a float range nobody is selling
    in has no prices to store, and reading it again buys the same nothing.
    """
    if not depth:
        return True
    if span:
        from .depth import depth_profile

        if len(depth) < len(depth_profile([], span)):
            return True
    return any(int(b.get("listings") or 0) > 0 and not (b.get("asks") or [])
               for b in depth)


def pick(db, names: Iterable[str], force: bool = False) -> tuple[list[tuple[str, int]],
                                                                list[tuple[str, str]]]:
    """Split the names into what to sweep and what to leave, with reasons.

    Reasons rather than a silent filter: "it skipped my item" is the question
    this run gets asked, and the answer is either "it is already priced" or
    "it is not in the database", which are not the same thing.
    """
    wanted: list[tuple[str, int]] = []
    skipped: list[tuple[str, str]] = []
    for name in names:
        item_id = db.get_item_id(name)
        if item_id is None:
            skipped.append((name, "не отслеживается"))
            continue
        if force:
            wanted.append((name, item_id))
            continue
        try:
            depth = db.listing_depth(item_id)
        except Exception:  # noqa: BLE001 - an older DB has no such table
            depth = []
        if needs_prices(depth, wear_range(name)):
            wanted.append((name, item_id))
        else:
            skipped.append((name, f"цены уже есть ({len(depth)} полос)"))
    return wanted, skipped


def quota(collector) -> str:
    """What CSFloat last said about the budget, in one line.

    Without it a refusal is indistinguishable from a bug: the same message
    twice tells you nothing about whether waiting longer would have helped, or
    whether something else on this address is spending the quota faster than
    the sweep can use it.
    """
    client = getattr(collector, "client", None)
    state = getattr(client, "rate_state", None) or {}
    if not state:
        return refusal(client)
    left, limit = state.get("remaining"), state.get("limit")
    said = []
    if left is not None:
        said.append(f"осталось {left}" + (f" из {limit}" if limit else ""))
    reset = getattr(client, "reset_in", None)
    if callable(reset):
        try:
            seconds = float(reset())
        except Exception:  # noqa: BLE001
            seconds = 0.0
        if seconds > 0:
            said.append(f"сброс через {seconds / 60:.1f} мин")
    return ", ".join(said) or refusal(client)


def refusal(client) -> str:
    """What the 429 itself said, when it carried no quota headers at all.

    A refusal with no numbers is the one that cannot be waited out blindly:
    CSFloat saying the budget is spent and Cloudflare saying it does not like
    the address look identical from here, and only one of them is fixed by
    sitting still. The body names which.
    """
    if client is None:
        return ""
    headers = getattr(client, "last_429_headers", None) or {}
    body = (getattr(client, "last_429_body", None) or "").strip()
    said = []
    for key in ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"):
        for name, value in headers.items():
            if name.lower() == key:
                said.append(f"{name}: {value}")
    if body:
        flat = " ".join(body.split())
        said.append(f"ответ: {flat[:140]}")
    if not said:
        said.append("CSFloat не прислал ни заголовков квоты, ни текста — "
                    "отказ пустой")
    return "; ".join(said)


def cooldown(collector) -> float:
    """How long the client says to wait, from whichever clock knows.

    Two of them: the account-wide pause a 429 arms, and the pool's own, since
    the route that drew the refusal is parked separately. Waiting the shorter
    one walks straight back into the limit.
    """
    seconds = [5.0]
    client = getattr(collector, "client", None)
    remaining = getattr(client, "cooldown_remaining", None)
    if callable(remaining):
        try:
            seconds.append(float(remaining()))
        except Exception:  # noqa: BLE001 - a clock must not end the run
            pass
    pool = getattr(client, "pool", None)
    waiting = getattr(pool, "wait_seconds", None)
    if callable(waiting):
        try:
            seconds.append(float(waiting()))
        except Exception:  # noqa: BLE001
            pass
    return max(seconds)


def sweep(collector, targets: Sequence[tuple[str, int]],
          report: Callable[[str, dict], None] | None = None,
          patience: float = 0.0,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Read the sell side of each target, waiting out the limits it draws.

    A rate limit never moves on to the next item: it would be refused too, and
    a hundred refusals spend the whole reset window learning that once per
    item. With `patience` seconds to spend it waits for the pause to lift and
    resumes the same item at the float it stopped on - the bands below are
    already stored. With none, it stops, and the next run picks up the rest.

    The waiting is bounded rather than open-ended because the pauses escalate:
    1, 2, 4 minutes and up. A run that waits forever is one nobody can tell
    from a hang.
    """
    out: dict[str, Any] = {"swept": [], "failed": [], "requests": 0,
                           "listings": 0, "stopped": "", "waited": 0.0}
    queue = list(targets)
    start: float | None = None
    while queue:
        name, item_id = queue[0]
        try:
            result = collector.sweep_listing_depth(name, item_id, start=start)
        except Exception as exc:  # noqa: BLE001 - one item must not lose the rest
            result = {"error": f"{type(exc).__name__}: {exc}", "requests": 0,
                      "bands": 0, "listings": 0}
        out["requests"] += int(result.get("requests") or 0)
        out["listings"] += int(result.get("listings") or 0)
        if result.get("bands"):
            out["swept"].append((name, result))
        elif not result.get("rate_limited"):
            out["failed"].append((name, result.get("error") or "полос не прочитано"))
        if report:
            report(name, result)

        if not result.get("rate_limited"):
            queue.pop(0)
            start = None
            continue

        pause = cooldown(collector)
        left = patience - out["waited"]
        if pause > left:
            said = quota(collector)
            out["stopped"] = (
                f"CSFloat отказал на '{name}' — ждать ещё "
                f"{pause / 60:.0f} мин, это больше отпущенного"
                + (f" (квота: {said})" if said else "")
                + ". Остальные предметы не тронуты, запусти ещё раз позже")
            break
        out["waited"] += pause
        if report:
            said = quota(collector)
            report(name, {"bands": 0, "waiting": pause,
                          "error": f"жду {pause / 60:.1f} мин до снятия лимита"
                                   + (f" (квота: {said})" if said else "")})
        sleep(pause)
        # Resume where it stopped: the bands below are stored, and reading
        # them again buys nothing but the next refusal.
        start = result.get("stopped_at")
    return out
