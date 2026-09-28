"""How many orders there are to place across the whole database, and where.

Every question so far has been about one item worked by hand, or the four on
the analysis list. This asks the one that decides whether any of it is worth
running: over every tracked item, how many rungs clear the margin floor, what
they would cost, and whether the answer rests on two items or twenty.

It reads the database and nothing else, so it runs while the account is rate
limited or flagged.

The division that matters is the order book. An item that has never been swept
has no rivals on record, so the pricing sees an empty book and bids the whole
ceiling - a number that is not wrong so much as unearned, because the
competition is there and simply unmeasured. Totalling those in with the rest
would answer the question with mostly fiction, so they are counted apart and
never added together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


# The rejections, grouped by what one would do about them. Free text is what
# the pricing writes; these are the buckets worth counting.
REASONS: tuple[tuple[str, str], ...] = (
    ("мало продаж", "истории не хватает"),
    ("цена выхода", "выход ниже любой осмысленной цены"),
    ("перебить стоит", "перебить дороже, чем позволяет маржа"),
    ("конкуренты перебивают", "конкуренты забирают дешёвые лоты"),
    ("никто не продавал", "по нашей цене никто не продавал"),
    ("весь поток забрали", "поток уже занят ступенью выше"),
)


def bucket(reason: str) -> str:
    for needle, label in REASONS:
        if needle in reason:
            return label
    return "прочее" if reason else "принята"


@dataclass
class Item:
    name: str
    sales: int = 0
    orders: int = 0            # rival orders on record
    depth_bands: int = 0
    rungs: int = 0
    taken: int = 0
    capital: float = 0.0       # what the taken rungs would cost
    best_rank: float = 0.0
    reasons: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def blind(self) -> bool:
        """No book on record: the pricing cannot see who it is bidding against."""
        return self.orders == 0


def census(items: Sequence[Item]) -> dict[str, Any]:
    """The totals, kept apart for the items whose competition is unmeasured."""
    seen = [i for i in items if not i.blind and not i.error]
    blind = [i for i in items if i.blind and not i.error]

    def over(rows: Sequence[Item]) -> dict[str, Any]:
        capital = sum(i.capital for i in rows)
        by_item = sorted((i.capital for i in rows), reverse=True)
        return {
            "items": len(rows),
            "with_orders": sum(1 for i in rows if i.taken),
            "rungs": sum(i.rungs for i in rows),
            "taken": sum(i.taken for i in rows),
            "capital": round(capital, 2),
            # One item holding most of the money is one bet, not a portfolio.
            "concentration": round(by_item[0] / capital, 3) if capital else 0.0,
        }

    reasons: dict[str, int] = {}
    for item in items:
        for label, count in item.reasons.items():
            reasons[label] = reasons.get(label, 0) + count

    return {"measured": over(seen), "blind": over(blind),
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "failed": [(i.name, i.error) for i in items if i.error]}


def look(name: str, sales: Sequence[dict], orders: Sequence[dict],
         depth: Sequence[dict], span, params) -> Item:
    """Score one item, counting why each rung was refused."""
    from .pricing import plan

    out = Item(name=name, sales=len(sales), orders=len(orders),
               depth_bands=len(depth))
    if not span:
        out.error = "без износа в названии нечего размечать"
        return out
    try:
        bands = plan(sales, orders, span, depth, params)
    except Exception as exc:  # noqa: BLE001 - one item must not lose the rest
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    out.rungs = len(bands)
    for band in bands:
        label = "принята" if band.take else bucket(band.reason)
        out.reasons[label] = out.reasons.get(label, 0) + 1
        if band.take:
            out.taken += 1
            out.capital += band.bid or 0.0
            out.best_rank = max(out.best_rank,
                                (band.lam or 0.0) * (band.margin or 0.0))
    out.capital = round(out.capital, 2)
    return out
