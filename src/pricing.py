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
    fee: float = 0.02              # CSFloat's cut when we sell
    min_margin: float = 0.03       # below this the trade is not worth doing
    window_days: float = 28.0      # history used for the rates
    band_step: float = 0.02        # float width of one order
    min_lambda: float = 0.10       # fills per day, under which capital idles
    min_wars: int = 2              # outbids we must be able to answer
    max_fill_days: float = 21.0
    min_sample: int = 8            # sales needed before a median means anything
    # How far, in float, a band may borrow sales from to price itself when it
    # has too few of its own. Past this the neighbours are a different item in
    # all but name. 0 switches the borrowing off.
    max_reach: float = 0.05
    # Raising the bid does buy flow - every listing at or under it executes
    # against the order - but the last dollars of that often buy very little.
    # Among prices that come within this fraction of the best return, take the
    # cheapest: the difference is headroom kept and capital not risked.
    bid_tolerance: float = 0.10
    # A thin margin is not the same trade as a fat one at the same return: it
    # is far more exposed to the exit price being wrong. The median of a
    # band's sales carries its own error, so require the margin to clear that
    # error by this many multiples before the trade is believed.
    sigma_k: float = 2.0


def _median_error(prices: Sequence[float]) -> float:
    """Relative error of a band's median price.

    Taken from the interquartile range rather than the standard deviation, so
    one freak sale does not widen it: sigma = IQR / 1.349, and the median of n
    samples carries 1.2533 * sigma / sqrt(n).
    """
    if len(prices) < 4:
        return 1.0
    ordered = sorted(prices)
    q1, q3 = st.quantiles(ordered, n=4)[0], st.quantiles(ordered, n=4)[2]
    middle = st.median(ordered)
    if middle <= 0:
        return 1.0
    sigma = (q3 - q1) / 1.349
    return 1.2533 * sigma / len(ordered) ** 0.5 / middle


@dataclass
class Band:
    float_min: float
    float_max: float
    sample: int = 0
    market: float | None = None        # what it resells for
    market_error: float | None = None  # how well that median is pinned down
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
    # What the cheapest leading price would have given, so the surcharge the
    # scan paid for flow is visible beside the price it chose rather than
    # having to be taken on trust.
    entry_lam: float | None = None
    entry_t_buy: float | None = None
    entry_monthly: float | None = None
    # When a band had too few sales of its own and borrowed from its
    # neighbours: how many it used and how far it had to reach for them.
    borrowed: int = 0
    reach: float | None = None
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


def neighbourhood(sales: Sequence[dict], lo: float, hi: float,
                  want: int, at: float | None = None
                  ) -> tuple[float | None, float, float, int]:
    """What a band's lot is worth, priced off a line fitted through its sales.

    Two problems, one answer.

    A band's own sales are often too few to place it - eight in a 0.02 slice
    of a wear is a lot to ask - and that was the commonest reason a band was
    dropped. Price moves with float smoothly, so the sales just outside say a
    great deal about the ones inside, and the window widens past the band's
    edges until it holds `want` of them.

    And a band is not one price. An order filters on a float range, and the
    sellers who take it are the ones your bid suits - the cheap end of the
    range, which for float means the high end. You are systematically handed
    the worst lot the filter allows, so the band's median is the price of an
    item you will not receive. That is what `at` is for: price the band where
    it will actually be filled, which is its high-float edge, and the bias
    disappears without anyone having to guess at its size. On an item whose
    price ignores float the slope is flat and nothing changes; on one where
    float drives the price the correction is exactly as large as the slope.

    The slope is a Theil-Sen estimate - the median of the pairwise slopes -
    which ignores a freak sale instead of being dragged by it. Returns the
    price, how far the window reached, and how many sales it used; a wide
    window is a weaker answer and the caller is told so.
    """
    rows = [(float(s["float_value"]), float(s["price"])) for s in sales
            if s.get("float_value") is not None and s.get("price")]
    if len(rows) < 2:
        return None, 1.0, 1.0, len(rows)

    centre = (lo + hi) / 2.0
    at = hi if at is None else at
    rows.sort(key=lambda r: abs(r[0] - centre))

    # Everything inside the band, and only then outward until there is enough.
    # Taking the `want` nearest outright would throw away a well-stocked
    # band's own evidence to honour a count.
    radius = max((hi - lo) / 2.0,
                 abs(rows[min(want, len(rows)) - 1][0] - centre))
    near = [r for r in rows if abs(r[0] - centre) <= radius + 1e-12]
    reach = max(abs(f - centre) for f, _ in near)

    # Pairwise slopes, capped: at n=40 that is 780 pairs, and the estimate is
    # not improved by more. Spread across the window rather than taken from
    # its middle, so the slope is measured over the whole span.
    use = near if len(near) <= 40 else near[::max(1, len(near) // 40)][:40]
    slopes = [(p2 - p1) / (f2 - f1)
              for i, (f1, p1) in enumerate(use)
              for f2, p2 in use[i + 1:]
              if abs(f2 - f1) > 1e-9]
    slope = st.median(slopes) if slopes else 0.0
    # Robust intercept: the median of price - slope*float over the window.
    level = st.median([p - slope * f for f, p in near])
    price = level + slope * at

    floor = min(p for _, p in near) * 0.5
    cap = max(p for _, p in near) * 2.0
    price = min(max(price, floor), cap)

    # How well the line holds: the scatter left over after it, read the same
    # way a median's error is read, and widened by how far the window had to
    # reach. Borrowing from three bands away is an answer, but a softer one.
    residuals = [pr - (level + slope * f) for f, pr in near]
    spread = _iqr(residuals) / 1.349 if len(residuals) >= 4 else None
    # How far past its own edge the window had to reach, in float. Measured in
    # float rather than in band widths: how confidently a price extrapolates
    # depends on the distance, not on how finely we chose to slice. Counting
    # band widths made narrowing the band step - a setting, not a fact about
    # the market - inflate the doubt on its own.
    beyond = max(0.0, reach - (hi - lo) / 2.0)
    stretch = beyond / REACH_UNIT
    if spread is None or price <= 0:
        error = 1.0
    else:
        error = (1.2533 * spread / math.sqrt(len(near))) / price
        error *= 1.0 + stretch
    # Sales lying exactly on a line say the line fits, not that it keeps
    # holding a dozen bands further out. Reaching is itself a doubt, and a
    # multiplier on a residual of zero records none of it.
    return price, min(max(error, REACH_DOUBT * stretch), 1.0), reach, len(near)


def _iqr(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    q1 = ordered[n // 4]
    q3 = ordered[(3 * n) // 4 - (1 if n % 4 == 0 else 0)]
    return q3 - q1


def _exit_price(history: float, depth: Sequence[dict],
                lo: float, hi: float, source: str) -> tuple[float, str]:
    """What the lot sells for, and it is the live book that says so.

    To sell promptly you have to be the cheapest listing, and matching the
    cheapest ask does not make you the cheapest - it puts you level with it
    and therefore behind it. Selling first means going under, by the one step
    the price grid allows. Taking the ask itself was claiming the front of a
    queue while standing second in it, and the whole return is figured on
    getting out at that price.
    """
    asks = [d["cheapest"] for d in depth
            if d.get("cheapest") is not None
            and not (d["float_max"] <= lo or d["float_min"] >= hi)]
    if not asks:
        return history, source
    best = min(asks)
    under = snap_down(best - increment(best))
    if under <= 0:
        return history, source
    return (under, "аск") if under < history else (history, source)


def evaluate(lo: float, hi: float, sales: Sequence[dict],
             orders: Sequence[dict], span: tuple[float, float] | None,
             depth: Sequence[dict] = (),
             params: Params | None = None) -> Band:
    """Score one float range, whoever chose it.

    The grid moves: band edges are cut at the bounds of other people's orders,
    so a rival appearing or leaving reshapes them. An order we already hold has
    a fixed range of its own, and it has to be judged on that range rather than
    looked up in today's grid - a shifted edge would otherwise read as "this
    band no longer qualifies" and withdraw a perfectly good position.
    """
    p = params or Params()
    out: list[Band] = []
    for lo, hi in ((lo, hi),):
        band = [s["price"] for s in sales
                if s.get("float_value") is not None and lo <= s["float_value"] < hi]
        row = Band(float_min=lo, float_max=hi, sample=len(band))

        # One path for both cases. The band's own sales are used when it has
        # them and the window widens past its edges when it does not, and
        # either way the price is read off the fitted line at the band's
        # high-float edge rather than off a median in its middle: an order
        # filters on a range, and the sellers who take it hand over the worst
        # lot the filter allows.
        history, error, reach, used = neighbourhood(sales, lo, hi, p.min_sample)
        if history is None:
            row.reason = f"мало данных: {len(band)} продаж, занять не у кого"
            out.append(row)
            continue
        if len(band) >= p.min_sample:
            source = "история"
        else:
            row.borrowed = used
            row.reach = reach
            if p.max_reach and reach > p.max_reach:
                row.reason = (f"мало данных: {len(band)} продаж, ближайшие "
                              f"{used} — за {reach:.3f} по float")
                out.append(row)
                continue
            source = "соседи"

        market, source = _exit_price(history, depth, lo, hi, source)
        net = market * (1.0 - p.fee)
        step = increment(market)
        ceiling = snap_down(net / (1.0 + p.min_margin))
        rivals = _competing(orders, lo, hi, span)
        top = max((o["price"] for o in rivals), default=0.0)
        entry = next_above(top) if top else snap_down(market * 0.85)

        row.market, row.priced_from, row.step = market, source, step
        row.market_error = error
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
        found: list[Band] = []
        thin = False
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
                    and lam_sell > 0
                    and margin >= p.sigma_k * error):
                t_sell = 1.0 / lam_sell
                monthly = margin * 30.0 / (t_buy + t_sell)
                found.append(Band(
                    float_min=lo, float_max=hi, sample=len(band),
                    market=market, market_error=error,
                    priced_from=source, top=top, entry=entry,
                    ceiling=ceiling, bid=bid, step=step, margin=margin,
                    wars=wars, lam=lam, queue=queue, t_buy=t_buy,
                    t_sell=t_sell, monthly=monthly, take=True))
            elif (lam >= p.min_lambda and wars >= p.min_wars
                  and 0 < margin < p.sigma_k * error):
                # Everything else about this price is fine; only the margin is
                # inside the error bar on what the lot resells for.
                thin = True
            bid = round(bid + step, 2)

        # Measured whether or not the entry passes the filters: when it does
        # not, why it does not is the answer to "why are we bidding over the
        # book", and that is the question the number gets asked.
        entry_fills = [x for x in recent if x["price"] <= entry]
        entry_lam = len(entry_fills) / p.window_days
        entry_queue = sum(int(o.get("qty") or 1) for o in rivals
                          if o["price"] >= entry)
        entry_t_buy = (1 + entry_queue) / entry_lam if entry_lam > 0 else None
        entry_monthly = None
        if entry_t_buy is not None and lam_sell > 0:
            entry_monthly = ((net - entry) / entry) * 30.0 / (
                entry_t_buy + 1.0 / lam_sell)

        if found:
            # The cheapest price that still earns nearly the best return. The
            # scan used to take the maximum outright, which bid dollars over
            # the book for a few percent of turnover - on one glove $48.10
            # where $44.50 made us first, spending the headroom to buy flow
            # that was barely there.
            peak = max(b.monthly or 0 for b in found)
            floor_ = peak * (1.0 - p.bid_tolerance)
            best = min((b for b in found if (b.monthly or 0) >= floor_),
                       key=lambda b: b.bid)
            best.entry_lam = entry_lam
            best.entry_t_buy = entry_t_buy
            best.entry_monthly = entry_monthly
            out.append(best)
        else:
            if thin:
                blocked = (f"маржа не перекрывает погрешность цены "
                           f"(±{error * 100:.1f}% на {len(band)} продажах)")
            row.reason = blocked
            row.entry_lam = entry_lam
            row.entry_t_buy = entry_t_buy
            row.entry_monthly = entry_monthly
            out.append(row)

    return out[0]


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
    out = [evaluate(lo, hi, sales, orders, span, depth, p)
           for lo, hi in _bands(span, p.band_step, orders)]
    out.sort(key=lambda r: (not r.take, -(r.monthly or 0), r.float_min))
    return out
