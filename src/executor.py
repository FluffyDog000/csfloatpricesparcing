"""From a plan to a list of actions: place, defend, withdraw.

Deciding what a band is worth is one problem; deciding what to do about the
orders already standing is another. This does the second, and does it without
touching the network - every run produces actions and reasons, and placing
them is a separate, deliberate step.

Three rules carry most of the weight.

Withdraw at the ceiling. The ceiling is the price above which the trade stops
being worth doing, worked out before the fight rather than during it, so a
position that gets bid past it is abandoned rather than chased.

Prefer patience to re-bidding. Being outbid is not by itself a reason to
answer: whoever went above us is filled first, and then we lead again for
nothing. What makes it worth answering is the wait that queue implies - so we
re-bid when the queue ahead would push the fill past the time we are willing
to wait, and not before.

Concentration is a cap, not an outcome. Five orders on one item are one bet in
five pieces: they fill together when that market drops. Limits are applied per
item before the portfolio is assembled, so a single item cannot crowd out the
rest simply by scoring well.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .pricing import Band, next_above

PLACE = "place"
RAISE = "raise"
CANCEL = "cancel"
KEEP = "keep"


@dataclass
class Limits:
    total_capital: float = 0.0      # 0 = nothing may be placed
    per_item_capital: float = 0.0   # 0 = only the share implied by the count
    max_orders: int = 20
    max_orders_per_item: int = 3
    patience_days: float = 14.0     # a queue longer than this is worth answering


@dataclass
class Action:
    kind: str
    item: str
    float_min: float
    float_max: float
    price: float
    ceiling: float
    reason: str
    order_id: int | None = None
    remote_id: str | None = None
    was: float | None = None        # the price we were bidding, when raising

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _key(row: Any) -> tuple[float, float]:
    if isinstance(row, Band):
        return (round(row.float_min, 4), round(row.float_max, 4))
    return (round(float(row["float_min"]), 4), round(float(row["float_max"]), 4))


def select(bands: Sequence[Band], limits: Limits,
           spent: float = 0.0, placed: int = 0) -> list[Band]:
    """The bands worth holding for one item, best first, within its caps."""
    take = sorted((b for b in bands if b.take),
                  key=lambda b: -(b.monthly or 0))
    room_orders = min(limits.max_orders_per_item, limits.max_orders - placed)
    cap = limits.per_item_capital or limits.total_capital
    cap = min(cap, limits.total_capital - spent)

    chosen: list[Band] = []
    used = 0.0
    for band in take:
        if len(chosen) >= room_orders:
            break
        if band.bid is None or used + band.bid > cap + 1e-9:
            continue
        chosen.append(band)
        used += band.bid
    return chosen


def reconcile(item: str, wanted: Sequence[Band], existing: Sequence[dict],
              book: Sequence[dict], limits: Limits) -> list[Action]:
    """What to do about one item's orders, given what we now want to hold."""
    by_band = {_key(b): b for b in wanted}
    seen: set[tuple[float, float]] = set()
    actions: list[Action] = []

    for row in existing:
        key = _key(row)
        seen.add(key)
        band = by_band.get(key)
        price = float(row["price"])
        ceiling = float(row["ceiling"])

        if band is None:
            actions.append(Action(
                CANCEL, item, key[0], key[1], price, ceiling,
                "полоса больше не проходит фильтры",
                order_id=row.get("id"), remote_id=row.get("remote_id")))
            continue

        # Whoever is bidding for the same lots, above us.
        rivals = [o for o in book
                  if (o.get("float_min") if o.get("float_min") is not None else 0.0) < key[1]
                  and (o.get("float_max") if o.get("float_max") is not None else 1.0) > key[0]
                  and o["price"] >= price]
        ahead = sum(int(o.get("qty") or 1) for o in rivals)
        new_ceiling = band.ceiling if band.ceiling is not None else ceiling

        if price > new_ceiling + 1e-9:
            actions.append(Action(
                CANCEL, item, key[0], key[1], price, new_ceiling,
                f"цена ${price:.2f} выше потолка ${new_ceiling:.2f}",
                order_id=row.get("id"), remote_id=row.get("remote_id")))
            continue

        if not ahead:
            actions.append(Action(
                KEEP, item, key[0], key[1], price, new_ceiling,
                "мы первые в полосе", order_id=row.get("id"),
                remote_id=row.get("remote_id")))
            continue

        # Outbid. Patience first: the queue clears by itself, and only the
        # wait it implies makes answering worth the money.
        lam = band.lam or 0.0
        wait = (1 + ahead) / lam if lam > 0 else float("inf")
        if wait <= limits.patience_days:
            actions.append(Action(
                KEEP, item, key[0], key[1], price, new_ceiling,
                f"перебили, но очередь разойдётся за {wait:.1f} дн — ждём",
                order_id=row.get("id"), remote_id=row.get("remote_id")))
            continue

        top = max(o["price"] for o in rivals)
        answer = next_above(top)
        if answer > new_ceiling + 1e-9:
            actions.append(Action(
                CANCEL, item, key[0], key[1], price, new_ceiling,
                f"перебили до ${top:.2f}, ответ ${answer:.2f} выше потолка "
                f"${new_ceiling:.2f}", order_id=row.get("id"),
                remote_id=row.get("remote_id")))
            continue

        actions.append(Action(
            RAISE, item, key[0], key[1], answer, new_ceiling,
            f"ждать {wait:.1f} дн дольше терпения — перебиваем ${top:.2f}",
            order_id=row.get("id"), remote_id=row.get("remote_id"),
            was=price))

    for key, band in by_band.items():
        if key in seen:
            continue
        actions.append(Action(
            PLACE, item, key[0], key[1], band.bid, band.ceiling,
            f"{(band.monthly or 0) * 100:.0f}%/мес, запас {band.wars} перебив."))

    order = {CANCEL: 0, RAISE: 1, PLACE: 2, KEEP: 3}
    actions.sort(key=lambda a: (order[a.kind], a.float_min))
    return actions


def exposure(actions: Iterable[Action], existing_by_item: dict[str, float]
             ) -> dict[str, float]:
    """Capital each item would hold once these actions are applied."""
    out = dict(existing_by_item)
    for a in actions:
        if a.kind == PLACE:
            out[a.item] = out.get(a.item, 0.0) + a.price
        elif a.kind == CANCEL:
            out[a.item] = out.get(a.item, 0.0) - a.price
        elif a.kind == RAISE and a.was is not None:
            out[a.item] = out.get(a.item, 0.0) + (a.price - a.was)
    return out
