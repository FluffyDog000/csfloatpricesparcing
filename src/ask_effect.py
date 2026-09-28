"""What the stored lot prices are worth, measured against not having them.

The `asks` column was added on an argument: with only the cheapest lot stored,
one seller undercutting the rest prices the whole queue, and the exit price
that follows is too low - so sound rungs get rejected for a reason that is not
real. The argument is plausible and it is not evidence. This measures it.

The comparison is exact rather than historical: the same item, the same sales,
the same book, priced twice. Once with every lot's price, and once with the
band reduced to what an old row holds - a count and a minimum. Nothing is
fetched; both readings come from what is already stored, so it runs while the
account is rate limited, flagged, or offline.

The honest outcome to watch for is "no difference". If a band's queue clears
during the seven-day lock, the standing lots impose no ceiling either way and
the history median prices the rung alone. Then the column changes nothing for
that item, and the effort belongs elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass
class Change:
    """One rung, priced both ways."""
    top: float
    lots: int
    priced: int                     # how many of those lots we have a price for
    blind_exit: float | None
    full_exit: float | None
    blind_ceiling: float | None
    full_ceiling: float | None
    blind_margin: float | None
    full_margin: float | None
    blind_take: bool = False
    full_take: bool = False
    # Why the exit price is what it is: how many of the standing lots the
    # seven-day lock disposes of, and which of the two candidates bound.
    # Without these, "no difference" is a result with no explanation, and an
    # unexplained null is indistinguishable from a broken measurement.
    lots_cleared: float = 0.0
    priced_from: str = ""
    reason: str = ""

    @property
    def flipped(self) -> str:
        if self.full_take and not self.blind_take:
            return "открылась"
        if self.blind_take and not self.full_take:
            return "закрылась"
        return ""

    @property
    def ceiling_gain(self) -> float:
        return (self.full_ceiling or 0.0) - (self.blind_ceiling or 0.0)


def blinded(depth: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same bands as an old row holds them: the count and the minimum.

    Not the same as an empty band. The pricing falls back to the cheapest ask
    when the list is missing, so dropping the list without keeping `cheapest`
    would measure the wrong thing - it would show what having no sell side at
    all does, which is a different and more flattering question.
    """
    out = []
    for band in depth:
        copy = dict(band)
        asks = band.get("asks") or []
        copy["asks"] = []
        if copy.get("cheapest") is None and asks:
            copy["cheapest"] = min(float(a[0]) for a in asks)
        out.append(copy)
    return out


def compare(sales: Sequence[dict], orders: Sequence[dict],
            span: tuple[float, float] | None,
            depth: Sequence[dict[str, Any]],
            params) -> list[Change]:
    """Every rung of one item, priced with the lot prices and without them."""
    from . import ladder as _ladder

    if not span:
        return []
    full = {r.top: r for r in _ladder.ladder(sales, orders, span, depth, params)}
    blind = {r.top: r for r in
             _ladder.ladder(sales, orders, span, blinded(depth), params)}

    out = []
    for top in sorted(set(full) | set(blind)):
        f, b = full.get(top), blind.get(top)
        if f is None or b is None:
            continue
        out.append(Change(
            top=top,
            lots=f.lots,
            priced=_priced_lots(depth, top),
            blind_exit=b.queue_price, full_exit=f.queue_price,
            blind_ceiling=b.ceiling, full_ceiling=f.ceiling,
            blind_margin=b.margin, full_margin=f.margin,
            blind_take=b.take, full_take=f.take,
            lots_cleared=f.lots_cleared, priced_from=f.priced_from,
            reason=f.reason or b.reason,
        ))
    return out


def _priced_lots(depth: Sequence[dict[str, Any]], top: float) -> int:
    from .ladder import lots_in_band

    _, prices, _ = lots_in_band(depth, top)
    return len(prices)


def summarise(changes: Sequence[Change]) -> dict[str, Any]:
    """The answer in numbers, so "it helped" is not taken on impression."""
    moved = [c for c in changes if abs(c.ceiling_gain) > 1e-9]
    return {
        "rungs": len(changes),
        "moved": len(moved),
        # The two ways the sell side can fail to matter, counted apart. A
        # queue that empties during the lock caps nothing at any price; a
        # queue that stands but sits above the history median is outranked by
        # it. Only the second would start binding if the market cooled.
        "queue_gone": sum(1 for c in changes if c.full_exit is None),
        "median_binds": sum(1 for c in changes
                            if c.full_exit is not None
                            and c.priced_from == "история"),
        "queue_binds": sum(1 for c in changes if c.priced_from == "очередь"),
        "lots_cleared": max((c.lots_cleared for c in changes), default=0.0),
        "lots": max((c.lots for c in changes), default=0),
        "opened": sum(1 for c in changes if c.flipped == "открылась"),
        "closed": sum(1 for c in changes if c.flipped == "закрылась"),
        "best_gain": max((c.ceiling_gain for c in changes), default=0.0),
        "worst_gain": min((c.ceiling_gain for c in changes), default=0.0),
        "take_before": sum(1 for c in changes if c.blind_take),
        "take_after": sum(1 for c in changes if c.full_take),
    }
