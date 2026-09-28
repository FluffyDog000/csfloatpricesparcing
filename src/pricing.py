"""Which buy orders to place, and at what price.

The price is not ours to choose freely. CSFloat matches a listing to the
highest-priced order whose filters accept it, so anything below the top of a
band never fills; and order prices move on a tier grid ($1 steps between $100
and $500), so "outbid by a cent" is not a move that exists. What is ours to
choose is the price we refuse to go above.

    ceiling  = what the lot resells for, less fee, less the margin we demand
    entry    = the cheapest valid price that puts us at the front of the band
    headroom = how many outbids fit between the two

Below the entry we are invisible; above the ceiling we are buying at a loss.
The band is worth taking only if something profitable lies between them, and
the entry has to be high enough that sellers actually come to it - being first
in a queue nobody joins fills nothing, which is the mistake this module's
`min_lambda` filter exists to catch.

Margins are figured on what a fill costs, which is the listing's price and not
ours: CSFloat charges the lower of the two. An order does not wait for someone
who means to sell to it - it takes any listing that appears at or under it,
and the ones that pay are the listings priced as an ordinary example of the
skin by a seller who did not notice what its float was worth.

Costing every fill at the bid was the earlier reading, defended on the grounds
that a standing order tells a seller where to price. That holds for a seller
who reads the book, and it is exactly wrong for the one this strategy exists
to catch. It survives as the worst case: the ceiling guarantees that even a
fill at the full bid leaves the margin demanded.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from typing import Any, Sequence

# CSFloat rounds every order price to a tier step, so these are the only
# prices that exist. $13.37 is not a valid order in the $10-$100 tier.
PRICE_TIERS: tuple[tuple[float, float], ...] = (
    (5.0, 0.01), (10.0, 0.05), (100.0, 0.10),
    (500.0, 1.00), (1000.0, 5.00),
)
TOP_TIER_STEP = 10.00

# An uncontested band is scanned from its cheapest sale rather than from a
# fixed fraction of the market, which can be a long way below the ceiling.
# The walk is one grid step at a time, so it needs an end that does not depend
# on the spread being sane.
MAX_SCAN_STEPS = 2000

# Borrowing a price from neighbouring sales is worth a point of doubt for
# every hundredth of float the window had to reach past the band's own edge.
# Both in float, so the band step - a setting, not a fact about the market -
# does not change the answer.
REACH_UNIT = 0.01
REACH_DOUBT = 0.01


def increment(price: float) -> float:
    """The step the order grid uses at this price."""
    for ceiling, step in PRICE_TIERS:
        if price < ceiling:
            return step
    return TOP_TIER_STEP


def snap_down(price: float) -> float:
    step = increment(price)
    return round(math.floor(round(price / step, 6)) * step, 2)


def snap_up(price: float) -> float:
    step = increment(price)
    return round(math.ceil(round(price / step, 6)) * step, 2)


def next_above(price: float) -> float:
    """The cheapest valid order price strictly above `price`."""
    step = increment(price)
    up = snap_up(price)
    return round(up + step, 2) if abs(up - price) < 1e-9 else up


@dataclass
class Params:
    """The three numbers the model reads, and no more.

    Everything else that used to live here - band width, a minimum flow, a
    fill deadline, an outbid reserve, a sigma multiplier, a borrowing reach -
    described machinery the ladder does not have. Leaving them on the settings
    page as knobs that quietly did nothing would be worse than removing them.

    `min_margin` is the one that matters: it is the entire allowance for the
    exit price being wrong, and it is what decides how many orders there are.
    """
    fee: float = 0.02              # CSFloat's cut when we sell
    min_margin: float = 0.05       # the allowance for a wrong exit price
    window_days: float = 16.0      # history the medians are read from
    min_sample: int = 8            # sales needed before a median means anything


@dataclass
class Band:
    float_min: float
    float_max: float
    sample: int = 0
    market: float | None = None        # what it resells for
    priced_from: str = "история"
    top: float = 0.0                   # best competing bid in the band
    entry: float | None = None         # cheapest price that puts us first
    ceiling: float | None = None       # highest price still worth paying
    bid: float | None = None           # what we would actually place
    step: float = 0.0
    margin: float | None = None
    # What a fill is expected to cost, and the margin if every fill cost the
    # full bid instead. The first is the ordinary case, the second the bound
    # the ceiling guarantees.
    paid: float | None = None
    margin_worst: float | None = None
    wars: int | None = None            # outbids the headroom pays for
    lam: float | None = None
    queue: int = 0
    t_buy: float | None = None
    # How long until someone buys in this band at all. Reported, never
    # divided by: turning a margin into a rate needs the trade lock and the
    # payout wait, a fortnight that nothing here measures.
    t_sell: float | None = None
    # What the cheapest leading price would have given, so the surcharge the
    # scan paid for flow is visible beside the price it chose rather than
    # having to be taken on trust.
    entry_lam: float | None = None
    entry_t_buy: float | None = None
    # When a band had too few sales of its own and borrowed from its
    # neighbours: how many it used and how far it had to reach for them.
    borrowed: int = 0
    reach: float | None = None
    # How many sales the flow estimate rested on. Counting only the slice gave
    # one or two, which is not a rate; this counts the window.
    flow_sample: int = 0
    take: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _order_span(order: dict, span: tuple[float, float] | None) -> tuple[float, float]:
    lo, hi = order.get("float_min"), order.get("float_max")
    if lo is None or hi is None:
        return span or (0.0, 1.0)
    return float(lo), float(hi)


def _competing(orders: Sequence[dict], lo: float, hi: float,
               span: tuple[float, float] | None) -> list[dict]:
    """Orders that would take a lot from this band.

    Bands are half-open, [lo, hi): an order ending exactly where one begins
    competes for the band below, not this one.
    """
    out = []
    for o in orders:
        o_lo, o_hi = _order_span(o, span)
        if o_lo < hi and o_hi > lo:
            out.append(o)
    return out


def evaluate(lo: float, hi: float, sales: Sequence[dict],
             orders: Sequence[dict], span: tuple[float, float] | None = None,
             depth: Sequence[dict] = (),
             params: Params | None = None) -> Band:
    """Price one order that already exists, over exactly [lo, hi].

    Planning asks "which orders are worth placing"; this asks "what is the one
    we are holding worth now", which the defence and the holdings report both
    need. Same model, same numbers - the range is simply given rather than
    scanned, so a held order whose bounds match no scan step is still priced.
    """
    from . import ladder as _ladder

    p = params or Params()
    lots, prices, lot_span = _ladder.lots_in_band(depth, hi)
    # The same windowing `ladder` does for a whole climb. Without it this path
    # priced a held order off a quarter of history at a fortnight's rate.
    rung = _ladder.evaluate(
        hi, _ladder.within(sales, p.window_days), orders, (lo, hi), lots, prices,
        _ladder.Params(fee=p.fee, min_margin=p.min_margin,
                       window_days=p.window_days, min_sample=p.min_sample),
        lot_span=lot_span)
    return _as_band(rung)


def plan(sales: Sequence[dict], orders: Sequence[dict],
         span: tuple[float, float] | None,
         depth: Sequence[dict] = (),
         params: Params | None = None) -> list[Band]:
    """Score every candidate order for an item, taken or not.

    The scoring lives in `ladder`, which prices nested orders running from the
    wear minimum to their own top. This keeps the Band shape the executor and
    the dashboard already read, so the change of model does not ripple through
    them; the fields the old disjoint-band model needed and the new one does
    not are left at their defaults rather than filled with invented numbers.

    Rejected rungs are returned with their reason: "why not this one" is the
    question the table is read for, and dropping them silently makes an
    over-bid top indistinguishable from one nobody has looked at.
    """
    from . import ladder as _ladder

    p = params or Params()
    if not span:
        return []
    rungs = _ladder.ladder(
        sales, orders, span, depth,
        _ladder.Params(fee=p.fee, min_margin=p.min_margin,
                       window_days=p.window_days, min_sample=p.min_sample))
    return [_as_band(r) for r in rungs]


def _as_band(rung) -> Band:
    """One rung, in the shape the rest of the bot already speaks.

    Two limits, not one, because opening and defending are different
    decisions. The bid is where we open: one step over the best rival, the
    cheapest price that leads. The ceiling is where we stop: the highest price
    still clearing the margin floor. The gap between them is the room the
    defence has to answer an outbid, and it is bought by not overpaying at the
    start.
    """
    return Band(
        float_min=rung.low,
        float_max=rung.top,
        sample=rung.sample,
        market=rung.market,
        priced_from=rung.priced_from or "история",
        top=rung.rival,
        ceiling=rung.ceiling,
        bid=rung.bid,
        step=increment(rung.bid) if rung.bid else 0.0,
        margin=rung.margin,
        paid=rung.bid,
        margin_worst=rung.margin,
        lam=rung.lam,
        queue=rung.lots,
        flow_sample=rung.sample,
        take=rung.take,
        reason=rung.reason,
    )
