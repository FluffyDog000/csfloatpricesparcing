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
to wait, and not before. When we do answer, the order is amended in place
rather than replaced, so it keeps whatever standing it has.

Concentration is a cap, not an outcome. Five orders on one item are one bet in
five pieces: they fill together when that market drops. Limits are applied per
item before the portfolio is assembled, so a single item cannot crowd out the
rest simply by scoring well.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .ladder import LOCK_DAYS
from .pricing import Band, increment, next_above

def human_wait(days: float) -> str:
    """A wait, in the unit a person would say it in.

    Patience is set in minutes now, so "0.0 дн" would be the answer to most
    questions - and it is exactly the answer that says nothing.
    """
    if days == float("inf"):
        return "никогда"
    minutes = days * 1440.0
    if minutes < 90:
        return f"{minutes:.0f} мин"
    if minutes < 2880:
        return f"{minutes / 60:.1f} ч"
    return f"{days:.1f} дн"


PLACE = "place"
RAISE = "raise"
CANCEL = "cancel"
KEEP = "keep"


# CSFloat lets the outstanding value of your buy orders run to ten times your
# balance, on the reasoning that they will not all fill at once. The allowance
# is real, but it is not money: an order whose turn comes while the balance is
# short is removed, not queued. So what the plan spends is the money the fills
# keep busy, against the balance (`tied_up`); the allowance is shown, and
# never binds before it.
LEVERAGE = 10.0
MAX_ORDERS_CSFLOAT = 1000

# How far down from a band's estimated return to rank it. One standard error
# is deliberately mild: this orders candidates, it does not decide whether to
# take them - the thresholds do that.
RANK_Z = 1.0


@dataclass
class Limits:
    # Money the fills may keep busy at once, not the face value of the orders:
    # see `tied_up`. 0 = nothing may be placed.
    total_capital: float = 0.0
    per_item_capital: float = 0.0   # 0 = only the share implied by the count
    max_orders: int = 20
    max_orders_per_item: int = 3
    # How long we will wait for the queue ahead of us to clear before paying
    # to jump it. Minutes, not days: the defence re-reads the book on its own
    # interval, so the question it asks is "will this clear before I look
    # again", and a threshold coarser than the look is a threshold that never
    # fires.
    patience_minutes: float = 60.0
    # What sits on the CSFloat account. 0 means "not told", and then the
    # leverage cap cannot be worked out and only total_capital applies.
    balance: float = 0.0
    # The brake: fills worth more than this share of the balance within a day
    # take every order down and disarm. See `guard`. 0 turns it off.
    guard_share: float = 0.3

    @property
    def allowance(self) -> float:
        """The most CSFloat would let us have outstanding."""
        return self.balance * LEVERAGE if self.balance > 0 else float("inf")

    @property
    def budget(self) -> float:
        """Money we may keep busy: our own limit, under what the account has."""
        if self.balance > 0:
            return min(self.total_capital, self.balance)
        return self.total_capital

    @property
    def capped_by_balance(self) -> bool:
        """Our limit is above what the account holds."""
        return self.balance > 0 and self.total_capital > self.balance

    def as_dict(self) -> dict[str, Any]:
        """Fields plus what they work out to - the page needs both, and
        __dict__ on a dataclass leaves the derived values behind."""
        out = dict(self.__dict__)
        out["allowance"] = None if self.balance <= 0 else round(self.allowance, 2)
        out["budget"] = round(self.budget, 2) if self.budget != float("inf") else None
        out["capped_by_balance"] = self.capped_by_balance
        return out


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
    # margin / (lock + t_sell): return per dollar per day, and the order the
    # plan is listed in. It rides on the action rather than being recomputed by each reader,
    # so the number shown and the number sorted by cannot drift apart.
    rank: float = 0.0
    # The body actually sent, filled in by the sender just before the request.
    # A refusal that does not say what was sent leaves "the code was fixed" and
    # "the saved request was fixed" looking identical from the outside, and only
    # the second one is what travels.
    sent: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _key(row: Any) -> tuple[float, float]:
    if isinstance(row, Band):
        return (round(row.float_min, 4), round(row.float_max, 4))
    return (round(float(row["float_min"]), 4), round(float(row["float_max"]), 4))


def rank(band: Band) -> float:
    """What a band is worth, for choosing between them.

    What a dollar earns per day it is tied up: margin over the lock plus the
    days on sale. Worked out in `ladder.turnover`, which also says why this
    replaced `lam × margin`: the order waiting for a fill ties up no money on
    CSFloat, the fill does, and from then on it is the lock and the queue that
    decide how long.

    Flow still matters, but not here: it decides how much money a band can
    use (`tied_up`), and the selection spends by that. A band that never
    fills is refused before it gets a rank.
    """
    if band.margin is None or not band.lam:
        return 0.0 if band.margin is not None else -1.0
    if band.rank:
        return band.rank
    return band.margin / (LOCK_DAYS + (band.t_sell or 0.0))


def tied_up(band: Band, extra_lam: float = 0.0) -> float:
    """Money a band keeps busy, on average, while it runs.

    Little's law: it buys `lam` a day, each for `bid`, and each purchase is
    money until it is sold - the lock, then the days on sale. Never less than
    one bid, because a single fill has to be paid for in full whenever it
    comes.

    `extra_lam` is what other bands of ours already buy into the same lot
    band; they stand in the same queue, so the wait is worked out again with
    them in it, and a band that could not sell the sum costs more than any
    budget.
    """
    if band.bid is None:
        return float("inf")
    lam = band.lam or 0.0
    t = band.t_sell or 0.0
    if extra_lam and band.sell_rate:
        spare = band.sell_rate - lam - extra_lam
        if spare <= 1e-12:
            return float("inf")
        t = (band.own_queue + 1) / spare
    return band.bid * max(1.0, lam * (LOCK_DAYS + t))


def _lot_key(item: str, band: Band) -> tuple | None:
    """The lot band a band's items sell into, when it is known."""
    if band.lot_min is None or band.lot_max is None:
        return None
    return (item, round(band.lot_min, 4), round(band.lot_max, 4))


def select(bands: Sequence[Band], limits: Limits,
           spent: float = 0.0, placed: int = 0) -> list[Band]:
    """The bands worth holding for one item, best first, within its caps."""
    return select_portfolio([("", b) for b in bands], limits,
                            spent=spent, placed=placed).get("", [])


def select_portfolio(candidates: Sequence[tuple[str, Band]], limits: Limits,
                     held: Sequence[tuple[str, float, float]] = (),
                     spent: float = 0.0, placed: int = 0
                     ) -> dict[str, list[Band]]:
    """Which bands to hold, chosen across every item at once.

    The per-item version spent the budget in the order items happened to be
    listed. With three items nobody notices; with three hundred it decides the
    whole result - at twenty orders and three per item, the first seven names
    on the list take everything and the other two hundred and ninety-three are
    scored for nothing. A band returning 40%/month loses to one returning 4%
    because it was typed in later.

    Bands we already hold are seeded first, before anything is ranked. Dropping
    a standing order because a marginally better one turned up somewhere else
    costs a cancel, a replacement, and the queue position that came with it -
    and the replacement may not fill at all. Churn is a real expense, and
    "slightly better on paper" does not cover it.

    What is spent is the money the fills keep busy (`tied_up`), against the
    budget and the per-item cap, because that is what runs out. The face
    value of the orders is not counted: CSFloat allows ten times the balance
    of it, every band keeps at least its own bid busy, and the budget is never
    more than the balance - so the face value is always the slack one.

    Bands of one item selling into the same lot band share its buyers, so
    each one taken lengthens the wait of the next, and one the band cannot
    absorb on top of the others is not taken.
    """
    chosen: dict[str, list[Band]] = {}
    per_item_spent: dict[str, float] = {}
    flow: dict[tuple, float] = {}
    budget = limits.budget
    item_cap = limits.per_item_capital or budget

    def cost(item: str, band: Band) -> float:
        key = _lot_key(item, band)
        return tied_up(band, flow.get(key, 0.0) if key else 0.0)

    def room_for(item: str, band: Band) -> bool:
        if band.bid is None:
            return False
        if placed >= limits.max_orders:
            return False
        if len(chosen.get(item, ())) >= limits.max_orders_per_item:
            return False
        need = cost(item, band)
        if spent + need > budget + 1e-9:
            return False
        return per_item_spent.get(item, 0.0) + need <= item_cap + 1e-9

    def take(item: str, band: Band) -> None:
        nonlocal spent, placed
        need = cost(item, band)
        chosen.setdefault(item, []).append(band)
        spent += need
        per_item_spent[item] = per_item_spent.get(item, 0.0) + need
        key = _lot_key(item, band)
        if key:
            flow[key] = flow.get(key, 0.0) + (band.lam or 0.0)
        placed += 1

    holding = {(name, round(lo, 4), round(hi, 4)) for name, lo, hi in held}
    ranked = sorted((c for c in candidates if c[1].take),
                    key=lambda c: -rank(c[1]))

    for item, band in ranked:
        if (item, round(band.float_min, 4), round(band.float_max, 4)) in holding \
                and room_for(item, band):
            take(item, band)
    for item, band in ranked:
        if band in chosen.get(item, ()):
            continue
        if room_for(item, band):
            take(item, band)
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

        if band.ceiling is None:
            # Nothing near this order's top has sold, so there is no honest
            # price for it. Not knowing what a position is worth is not a
            # reason to close it; acting on no information is worse than
            # waiting for some.
            actions.append(Action(
                KEEP, item, key[0], key[1], price, ceiling,
                "нечем оценить: у верха полосы нет продаж — оставляем",
                order_id=row.get("id"), remote_id=row.get("remote_id")))
            continue
        new_ceiling = band.ceiling

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
        if wait * 1440.0 <= limits.patience_minutes:
            actions.append(Action(
                KEEP, item, key[0], key[1], price, new_ceiling,
                f"перебили, но очередь разойдётся за {human_wait(wait)} — ждём",
                order_id=row.get("id"), remote_id=row.get("remote_id")))
            continue

        top = max(o["price"] for o in rivals)
        answer = next_above(top)
        # Answering is held to the ceiling, which is the margin floor turned
        # into a price. We opened below it on purpose; this is what that room
        # was for.
        limit = new_ceiling
        if answer > limit + 1e-9:
            actions.append(Action(
                CANCEL, item, key[0], key[1], price, new_ceiling,
                f"перебили до ${top:.2f}, ответ ${answer:.2f} выше того, "
                f"что позволяет маржа (${limit:.2f})", order_id=row.get("id"),
                remote_id=row.get("remote_id")))
            continue

        actions.append(Action(
            RAISE, item, key[0], key[1], answer, new_ceiling,
            f"ждать {human_wait(wait)} дольше терпения — перебиваем ${top:.2f}",
            order_id=row.get("id"), remote_id=row.get("remote_id"),
            was=price))

    for key, band in by_band.items():
        if key in seen:
            continue
        # Headroom in grid steps, which is how many times the defence can
        # answer before the margin floor stops it. The old count of "outbids
        # the reserve pays for" was a field nothing fills any more, and the
        # line read "запас None перебив." on every row.
        room = (band.ceiling or 0.0) - (band.bid or 0.0)
        steps = int(room / increment(band.bid)) if band.bid else 0
        actions.append(Action(
            PLACE, item, key[0], key[1], band.bid, band.ceiling,
            f"маржа {(band.margin or 0) * 100:.1f}%, "
            f"налив {(band.lam or 0.0):.2f}/день, "
            f"запас ${room:.2f} = {steps} перебив."))

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
