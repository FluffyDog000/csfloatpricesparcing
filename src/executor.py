"""From a plan to a list of actions: place, defend, withdraw.

Deciding what a band is worth is one problem; deciding what to do about the
orders already standing is another. This does the second, and does it without
touching the network - every run produces actions and reasons, and placing
them is a separate, deliberate step.

Three rules carry most of the weight.

Never past the ceiling. The ceiling is the price above which the trade stops
being worth doing, worked out before the fight rather than during it, so a
position that gets bid past it is not chased - but it is not taken down
either. It stands behind, costing nothing, and leads again when the rival
fills or leaves; taking it down would mean placing it again out of the
day's 200 creates. When the ceiling itself falls under our price, the order
is brought down to it in place.

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
LOWER = "lower"
CANCEL = "cancel"
KEEP = "keep"

# How far above the cheapest price that still leads we may stand before the
# defence brings the order down: two grid steps. Closer than that, a rival
# coming and going would have the price moved on every pass for cents.
LOWER_STEPS = 2


# CSFloat lets the outstanding value of your buy orders run to ten times your
# balance, on the reasoning that they will not all fill at once. The allowance
# is real, but it is not money: an order whose turn comes while the balance is
# short is removed, not queued. So what the plan spends is the money the fills
# keep busy, against the balance (`tied_up`), and the face value of the orders
# against the allowance.
LEVERAGE = 10.0

# How sure the plan wants to be that the fills never need more than the
# budget. Fills arrive at random, so the money they keep busy is a sum of
# random amounts: its mean is what `tied_up` adds up, and the plan also keeps
# this many standard deviations of room above it (Limits.surge_z): one is
# about 84% of days, two about 98%, none plans on the average alone. Past it
# nothing is lost - CSFloat drops the order that cannot be paid - and the
# guard (`guard_share`) still takes everything down on a day that fills far
# more than the model expected.
BUDGET_Z = 1.0
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
    # Room kept above the average money in use, in standard deviations.
    surge_z: float = BUDGET_Z
    # Items per order: up to max_quantity, as many as the band is expected
    # to fill over order_days. One create out of CSFloat's 200 a day then
    # buys several items where they come fast.
    max_quantity: int = 3
    order_days: float = 4.0
    # Spend CSFloat's allowance rather than the money: orders up to ten times
    # the budget, ranked as usual, with no check on what their fills would
    # keep busy. More orders standing is more chances of a fill soon; the
    # price is that once the balance runs out, whichever orders fill first
    # take it and CSFloat drops the rest - the rank no longer decides where
    # the money goes.
    full_allowance: bool = False

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
    def order_cap(self) -> float:
        """The face value the plan may place: CSFloat's allowance, or - when
        it is spent in full - ten times our own budget, so the limit typed on
        the page still means something."""
        if self.full_allowance:
            return LEVERAGE * self.budget
        return self.allowance

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
        cap = self.order_cap
        out["order_cap"] = round(cap, 2) if cap != float("inf") else None
        out["full_allowance"] = bool(self.full_allowance)
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
    # Items the order asks for. Sent on create and kept on every amend.
    quantity: int = 1

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def size_orders(bands: Iterable[Band], limits: Limits) -> None:
    """How many items each band's order asks for.

    As many as it is expected to fill over `order_days`, at least one and at
    most `max_quantity`: a band filling twice a day gets three, one filling
    once a fortnight gets one. Rounded down - a band that would fill 1.9
    times is asked for one, not two.
    """
    cap = max(int(limits.max_quantity or 1), 1)
    days = max(float(limits.order_days or 0.0), 0.0)
    for band in bands:
        expected = (band.lam or 0.0) * days
        band.quantity = max(1, min(cap, int(expected)))


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


def held_count(band: Band, extra_lam: float = 0.0) -> float:
    """How many of this band's purchases are ours at once, on average.

    Little's law: it buys `lam` a day, and each purchase stays ours for the
    lock and then the days on sale. Infinite when the lot band cannot sell
    what we would buy into it; one when the fill rate was never measured,
    which is the cautious reading of "we do not know".

    `extra_lam` is what other bands of ours already buy into the same lot
    band; they stand in the same queue, so the wait is worked out again with
    them in it.
    """
    if band.bid is None:
        return float("inf")
    if band.lam is None:
        return 1.0
    lam = band.lam
    t = band.t_sell or 0.0
    if extra_lam and band.sell_rate:
        spare = band.sell_rate - lam - extra_lam
        if spare <= 1e-12:
            return float("inf")
        t = (band.own_queue + 1) / spare
    return lam * (LOCK_DAYS + t)


def tied_up(band: Band, extra_lam: float = 0.0) -> float:
    """Money a band keeps busy, on average, while it runs: the bid times how
    many of its purchases are ours at once.

    An average, and it can be well under one bid: a band that fills once a
    month holds its money a fraction of the time. That was floored at one
    bid, so the face value of the orders could never pass the balance - which
    left nine tenths of what CSFloat allows unused, on orders that almost
    never all fill together. What a single fill costs in full is the
    variance's job (`select_portfolio`), not the mean's.
    """
    n = held_count(band, extra_lam)
    return float("inf") if n == float("inf") else band.bid * n


def lowest_lead(book: Iterable[dict], hi: float) -> float | None:
    """The cheapest price that still leads every bid for the item at our top
    float: one step over the best of them. None when nobody bids for it -
    then there is nothing to measure a lower price against."""
    from .ladder import price_step, rival_bid, snap_up
    rival = rival_bid(list(book), hi)
    if rival <= 0:
        return None
    return snap_up(rival + price_step(rival), price_step(rival))


def ahead_of(book: Iterable[dict], lo: float, hi: float,
             price: float) -> list[dict]:
    """The orders standing in front of ours [lo, hi] at `price`.

    The ones that would take the item we actually get - a seller hands over
    the worst float an order accepts, which is our top - at our price or
    above. The same question the ladder asks when it prices the order
    (`ladder.rival_bid` at the top). Counting every order whose range merely
    overlaps ours put a 0.00-0.01 bid at $28 in front of a 0.00-0.02 order at
    $23 that was first for everything from 0.01 up - and the defence, seeing
    "outbid to $28, above the ceiling", took down orders that were winning.
    """
    out = []
    for o in book:
        o_lo = o.get("float_min")
        o_hi = o.get("float_max")
        o_lo = 0.0 if o_lo is None else float(o_lo)
        o_hi = 1.0 if o_hi is None else float(o_hi)
        if o_lo <= hi <= o_hi and float(o["price"]) >= price:
            out.append(o)
    return out


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
                     spent: float = 0.0, placed: int = 0,
                     trace: list | None = None
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

    What is spent is the money the fills keep busy, against the budget,
    because that is what runs out. Fills are random, so the plan holds the
    mean of it (`tied_up`) plus `surge_z` standard deviations under the
    budget: each band's purchases are a Poisson count of its bid, mean and
    variance both `held_count`. A rare, expensive band adds little to the
    mean and a lot to the spread, which is the right way round. The face
    value of the orders is held under CSFloat's allowance (ten times the
    balance), and no single bid may exceed the budget.

    Bands of one item selling into the same lot band share its buyers, so
    each one taken lengthens the wait of the next, and one the band cannot
    absorb on top of the others is not taken.

    `trace`, when given, is filled with every candidate in rank order: taken
    or not, why not, and what it costs - the placement queue the page shows.
    """
    chosen: dict[str, list[Band]] = {}
    outcome: dict[int, dict] = {}
    per_item_spent: dict[str, float] = {}
    flow: dict[tuple, float] = {}
    budget = limits.budget
    item_cap = limits.per_item_capital or budget
    allowance = limits.order_cap
    # `spent` is the mean of the money in use; these are its variance and the
    # face value of the orders, for the two other checks.
    var = 0.0
    face = 0.0

    def count(item: str, band: Band) -> float:
        key = _lot_key(item, band)
        return held_count(band, flow.get(key, 0.0) if key else 0.0)

    def cost(item: str, band: Band) -> float:
        n = count(item, band)
        return float("inf") if n == float("inf") else band.bid * n

    def peak(extra_mean: float = 0.0, extra_var: float = 0.0) -> float:
        """The money in use on a bad day: mean plus BUDGET_Z deviations."""
        return spent + extra_mean + limits.surge_z * (var + extra_var) ** 0.5

    def why_not(item: str, band: Band) -> str:
        """Empty when the band fits; otherwise the first limit it hits."""
        if band.bid is None:
            return "нет ставки"
        if placed >= limits.max_orders:
            return f"лимит ордеров ({limits.max_orders})"
        if len(chosen.get(item, ())) >= limits.max_orders_per_item:
            return f"не больше {limits.max_orders_per_item} на предмет"
        n = count(item, band)
        if n == float("inf"):
            return "полоса не продаст столько вместе с другими ступенями"
        if budget <= 0:
            return "бюджет не задан"
        if band.bid > budget + 1e-9:
            return f"одна покупка (${band.bid:.0f}) дороже бюджета"
        if face + band.bid * band.quantity > allowance + 1e-9:
            return (f"лимит CSFloat: ордеров не больше чем на "
                    f"${allowance:.0f} (10× баланса)")
        need = band.bid * n
        if limits.full_allowance:
            return ""
        if peak(need, band.bid ** 2 * n) > budget + 1e-9:
            return (f"не хватает бюджета: с запасом на всплеск занято "
                    f"${peak():.0f} из ${budget:.0f}")
        if per_item_spent.get(item, 0.0) + need > item_cap + 1e-9:
            return "лимит денег на предмет"
        return ""

    def room_for(item: str, band: Band) -> bool:
        return not why_not(item, band)

    def take(item: str, band: Band, already: bool = False) -> None:
        nonlocal spent, placed, var, face
        n = count(item, band)
        need = band.bid * n
        chosen.setdefault(item, []).append(band)
        spent += need
        var += band.bid ** 2 * n
        face += band.bid * band.quantity
        outcome[id(band)] = {"taken": True, "reason": "", "cost": need,
                             "held": already, "peak": peak(), "face": face}
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
            take(item, band, already=True)
    for item, band in ranked:
        if band in chosen.get(item, ()):
            continue
        reason = why_not(item, band)
        if reason:
            outcome[id(band)] = {"taken": False, "reason": reason,
                                 "cost": cost(item, band), "held": False}
        else:
            take(item, band)
    if trace is not None:
        for item, band in ranked:
            trace.append({"item": item, "band": band, **outcome[id(band)]})
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

        # Whoever would take the item we get, at our price or above.
        rivals = ahead_of(book, key[0], key[1], price)
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

        qty = int(row.get("quantity") or 1)
        if price > new_ceiling + 1e-9:
            # The market moved down under a standing order. Brought down to
            # the new ceiling rather than taken down: an amend is free, and
            # placing it again would spend one of the day's 200 creates.
            actions.append(Action(
                LOWER, item, key[0], key[1], new_ceiling, new_ceiling,
                f"потолок упал до ${new_ceiling:.2f} — снижаем с ${price:.2f}, "
                f"а не снимаем: ордер сохраняется",
                order_id=row.get("id"), remote_id=row.get("remote_id"),
                was=price, quantity=qty))
            continue

        if not ahead:
            # First - and maybe by more than it takes. A rival who stood just
            # under us and left leaves us paying for a fight that is over: at
            # $100 over a $90 book, every fill costs ten dollars it need not.
            lead = lowest_lead(book, key[1])
            if lead is not None and price - lead >= LOWER_STEPS * increment(lead) - 1e-9:
                actions.append(Action(
                    LOWER, item, key[0], key[1], lead, new_ceiling,
                    f"первыми будем и за ${lead:.2f}: выше нас никого, "
                    f"лучший соперник ниже — снижаем с ${price:.2f}",
                    order_id=row.get("id"), remote_id=row.get("remote_id"),
                    was=price, quantity=qty))
                continue
            actions.append(Action(
                KEEP, item, key[0], key[1], price, new_ceiling,
                "мы первые в полосе", order_id=row.get("id"),
                remote_id=row.get("remote_id"), quantity=qty))
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
            # Not answered, and not taken down either. Standing behind costs
            # nothing - no money is held by an order - and when the rival
            # fills or leaves we lead again for free. Taking it down meant
            # placing it again later, out of the day's 200 creates.
            actions.append(Action(
                KEEP, item, key[0], key[1], price, new_ceiling,
                f"перебили до ${top:.2f}, ответ ${answer:.2f} выше потолка "
                f"${limit:.2f} — стоим позади, ордер сохраняем",
                order_id=row.get("id"), remote_id=row.get("remote_id"),
                quantity=qty))
            continue

        actions.append(Action(
            RAISE, item, key[0], key[1], answer, new_ceiling,
            f"ждать {human_wait(wait)} дольше терпения — перебиваем ${top:.2f}",
            order_id=row.get("id"), remote_id=row.get("remote_id"),
            was=price, quantity=qty))

    for key, band in by_band.items():
        if key in seen:
            continue
        # Headroom in grid steps, which is how many times the defence can
        # answer before the margin floor stops it. The old count of "outbids
        # the reserve pays for" was a field nothing fills any more, and the
        # line read "запас None перебив." on every row.
        room = (band.ceiling or 0.0) - (band.bid or 0.0)
        steps = int(room / increment(band.bid)) if band.bid else 0
        expected = ""
        if (band.margin_expected is not None
                and abs(band.margin_expected - (band.margin or 0)) >= 0.001):
            expected = f" (ожид. {band.margin_expected * 100:.1f}%)"
        actions.append(Action(
            PLACE, item, key[0], key[1], band.bid, band.ceiling,
            f"маржа {(band.margin or 0) * 100:.1f}%{expected}, "
            f"налив {(band.lam or 0.0):.2f}/день, "
            f"запас ${room:.2f} = {steps} перебив."
            + (f" Количество {band.quantity}: столько придёт примерно за "
               f"срок ордера." if band.quantity > 1 else ""),
            quantity=band.quantity))

    order = {CANCEL: 0, RAISE: 1, LOWER: 1, PLACE: 2, KEEP: 3}
    actions.sort(key=lambda a: (order[a.kind], a.float_min))
    return actions


def exposure(actions: Iterable[Action], existing_by_item: dict[str, float]
             ) -> dict[str, float]:
    """Capital each item would hold once these actions are applied."""
    out = dict(existing_by_item)
    for a in actions:
        if a.kind == PLACE:
            out[a.item] = out.get(a.item, 0.0) + a.price * a.quantity
        elif a.kind == CANCEL:
            out[a.item] = out.get(a.item, 0.0) - a.price * a.quantity
        elif a.kind in (RAISE, LOWER) and a.was is not None:
            out[a.item] = out.get(a.item, 0.0) + (a.price - a.was) * a.quantity
    return out
