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

Margins are figured as though we pay our own bid. CSFloat charges the lower of
the bid and the listing price, so the true cost is a little less - but a
standing order at the top of the book is exactly what tells a seller where to
list, so the discount is the part we should not count on.
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
    fee: float = 0.02              # CSFloat's cut when we sell
    min_margin: float = 0.03       # below this the trade is not worth doing
    window_days: float = 28.0      # history used for the rates
    band_step: float = 0.02        # float width of one order
    min_lambda: float = 0.10       # fills per day, under which capital idles
    min_wars: int = 2              # outbids we must be able to answer
    max_fill_days: float = 21.0
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
    wars: int | None = None            # outbids the headroom pays for
    lam: float | None = None
    queue: int = 0
    t_buy: float | None = None
    t_sell: float | None = None
    monthly: float | None = None
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


def _bands(span: tuple[float, float], step: float,
           orders: Sequence[dict] = ()) -> list[tuple[float, float]]:
    """Cut the wear where the competition changes, then at most `step` wide.

    An even grid straddles the edges of other people's orders, and the price
    to be first is then set by the fiercest corner of the band. On one book
    every bid above $46 was scoped to 0.15-0.16, so a 0.15-0.17 band was
    priced at $48.30 - when a lot at 0.165 had a single rival at $44.40 and
    $44.50 would have taken it. Cutting at 0.16 first makes the cheap half
    its own band; capping each piece at `step` keeps the valuation honest,
    since a wide band is one the seller fills from its worst end.
    """
    lo, hi = span
    cuts = {round(lo, 4), round(hi, 4)}
    for o in orders:
        for edge in _order_span(o, span):
            if lo + 1e-9 < edge < hi - 1e-9:
                cuts.add(round(edge, 4))
    edges = sorted(cuts)

    out: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        x = a
        while x < b - 1e-9:
            nxt = round(min(x + step, b), 4)
            out.append((round(x, 4), nxt))
            x = nxt
    return out


def _exit_price(band_sales: list[float], depth: Sequence[dict],
                lo: float, hi: float) -> tuple[float, str]:
    """What the lot sells for. The live book wins over the sales median: to
    sell promptly we undercut the cheapest ask, and no history changes that."""
    median = st.median(band_sales)
    asks = [d["cheapest"] for d in depth
            if d.get("cheapest") is not None
            and not (d["float_max"] <= lo or d["float_min"] >= hi)]
    if asks and min(asks) < median:
        return min(asks), "аск"
    return median, "история"


def plan(sales: Sequence[dict], orders: Sequence[dict],
         span: tuple[float, float] | None,
         depth: Sequence[dict] = (),
         params: Params | None = None) -> list[Band]:
    """Score every float band of an item, taken or not.

    Rejected bands are returned with their reason: "why not this one" is the
    question the numbers are read for, and dropping them silently makes an
    over-bid band indistinguishable from one nobody has looked at.
    """
    p = params or Params()
    if not span:
        return []

    out: list[Band] = []
    for lo, hi in _bands(span, p.band_step, orders):
        band = [s["price"] for s in sales
                if s.get("float_value") is not None and lo <= s["float_value"] < hi]
        row = Band(float_min=lo, float_max=hi, sample=len(band))
        if len(band) < p.min_sample:
            row.reason = f"мало данных: {len(band)} продаж"
            out.append(row)
            continue

        market, source = _exit_price(band, depth, lo, hi)
        net = market * (1.0 - p.fee)
        step = increment(market)
        ceiling = snap_down(net / (1.0 + p.min_margin))
        rivals = _competing(orders, lo, hi, span)
        top = max((o["price"] for o in rivals), default=0.0)
        entry = next_above(top) if top else snap_down(market * 0.85)

        row.market, row.priced_from, row.step = market, source, step
        row.top, row.entry, row.ceiling = top, entry, ceiling

        if entry > ceiling:
            row.reason = (f"вход ${entry:.2f} выше потолка ${ceiling:.2f}"
                          " — кто-то ценит полосу выше нас")
            out.append(row)
            continue

        recent = [s for s in sales
                  if s.get("float_value") is not None and lo <= s["float_value"] < hi
                  and (s.get("age_days") is None or s["age_days"] <= p.window_days)]
        lam_sell = len(recent) / p.window_days
        best: Band | None = None
        blocked = "нет цены с потоком и запасом"
        bid = entry
        while bid <= ceiling + 1e-9:
            fills = [s for s in recent if s["price"] <= bid]
            lam = len(fills) / p.window_days
            wars = int(round((ceiling - bid) / step))
            queue = sum(int(o.get("qty") or 1) for o in rivals
                        if o["price"] >= bid)
            margin = (net - bid) / bid
            t_buy = (1 + queue) / lam if lam > 0 else None
            if (lam >= p.min_lambda and wars >= p.min_wars and margin > 0
                    and t_buy is not None and t_buy <= p.max_fill_days
                    and lam_sell > 0):
                t_sell = 1.0 / lam_sell
                monthly = margin * 30.0 / (t_buy + t_sell)
                if best is None or monthly > (best.monthly or 0):
                    best = Band(
                        float_min=lo, float_max=hi, sample=len(band),
                        market=market, priced_from=source, top=top, entry=entry,
                        ceiling=ceiling, bid=bid, step=step, margin=margin,
                        wars=wars, lam=lam, queue=queue, t_buy=t_buy,
                        t_sell=t_sell, monthly=monthly, take=True)
            bid = round(bid + step, 2)

        if best is not None:
            out.append(best)
        else:
            row.reason = blocked
            out.append(row)

    out.sort(key=lambda r: (not r.take, -(r.monthly or 0), r.float_min))
    return out
