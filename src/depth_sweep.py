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

from typing import Any, Callable, Iterable, Sequence


def needs_prices(depth: Sequence[dict[str, Any]]) -> bool:
    """Whether this item's stored sell side predates the price list.

    An empty band is not evidence of anything: a float range nobody is selling
    in has no prices to store. What gives the old rows away is a band that
    counted lots and kept none of their prices.
    """
    if not depth:
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
        if needs_prices(depth):
            wanted.append((name, item_id))
        else:
            skipped.append((name, f"цены уже есть ({len(depth)} полос)"))
    return wanted, skipped


def sweep(collector, targets: Sequence[tuple[str, int]],
          report: Callable[[str, dict], None] | None = None) -> dict[str, Any]:
    """Read the sell side of each target, newest first, until the quota says no.

    A rate limit stops the whole run rather than moving on. The next item would
    be refused too, and a hundred refusals spend the reset window learning that
    once per item; stopping leaves the ones already read stored and the rest to
    be picked up by the next run, which by then skips what this one finished.
    """
    out: dict[str, Any] = {"swept": [], "failed": [], "requests": 0,
                           "listings": 0, "stopped": ""}
    for name, item_id in targets:
        try:
            result = collector.sweep_listing_depth(name, item_id)
        except Exception as exc:  # noqa: BLE001 - one item must not lose the rest
            result = {"error": f"{type(exc).__name__}: {exc}", "requests": 0,
                      "bands": 0, "listings": 0}
        out["requests"] += int(result.get("requests") or 0)
        out["listings"] += int(result.get("listings") or 0)
        if result.get("bands"):
            out["swept"].append((name, result))
        else:
            out["failed"].append((name, result.get("error") or "полос не прочитано"))
        if report:
            report(name, result)
        if result.get("rate_limited"):
            out["stopped"] = (f"CSFloat отказал на '{name}' — остальные "
                              "предметы не тронуты, запусти ещё раз позже")
            break
    return out
