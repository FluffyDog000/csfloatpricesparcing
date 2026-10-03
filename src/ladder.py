"""Nested buy orders from the wear minimum, differing only in their top.

The previous model cut the wear range into disjoint bands. That was wrong, and
the market says so: 34 of the 36 float-scoped rival orders on a live item start
at the wear minimum and differ only in where they stop. The reason is that a
seller hands over the WORST float the order accepts, so the top sets what the
order is worth; the bottom only limits reach, and narrowing it costs items
while buying nothing. Measured on that item: orders 0.16-0.17 and 0.15-0.17
caught the same sales, and the worst float received was identical.

So the shape is a ladder. Every rung runs from the wear minimum to its own top,
and the rungs do not conflict: an item both accept goes to the dearer rung,
and one the dearer rung refuses on float falls to the next.

Four numbers decide a rung, and each is measured rather than assumed:

  what it resells for   the median of sales in the last 0.01 before the top -
                        the sales that resemble what we will actually receive.
                        The median, not a low quantile: pricing at the top
                        already handles adverse selection, and stacking a
                        second discount on it lost rungs that cleared the
                        margin floor honestly.

  what the queue allows  a lot cannot be sold above the ones already listed
                        below it. But the item sits under a 7-day trade lock
                        anyway, and lots sell during it, so a queue that
                        clears within the lock costs nothing. Only what
                        remains standing when the lock lifts sets the price.

  what we may pay       the exit divided by one plus the margin floor. The
                        floor is the single knob: it is the whole allowance
                        for the exit estimate being wrong.

  how often it fills    sales at or below our bid, where no rival outbids us
                        for that float. Two filters, and the second is the
                        severe one: most sellers get more elsewhere.

And one number ranks them: what a dollar earns per day it is tied up. See
`turnover`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .depth import DEPTH_PAGE
from .trend import weekly

# CSFloat holds a bought item for seven days before it can be listed. That is
# not a setting: it is the platform's rule, and it doubles as the natural
# patience for the sell queue, since the wait is already being paid.
LOCK_DAYS = 7.0

# Resolution of the top scan. Finer than the price structure needs, coarse
# enough that every step still has sales behind it. A coarser grid could not
# place a top where the value was: on a live item a 0.02 grid never tried
# 0.17, which was the only profitable top, and which of 0.17 or 0.20 it found
# depended on nothing but where the grid happened to start.
TOP_STEP = 0.01

# A hundredth of float needs this many sales before its median is used as the
# resale price of an item landing in it. Below that the item is valued as if
# it were at the rung's top - the worst case, which is never optimistic.
BUCKET_MIN_SALES = 5


@dataclass
class Params:
    fee: float = 0.02            # CSFloat's cut when we sell
    min_margin: float = 0.05     # the one knob: allowance for a wrong exit
    window_days: float = 16.0    # how much history the medians are read from
    min_sample: int = 8          # sales needed before a median means anything
    lock_days: float = LOCK_DAYS
    top_step: float = TOP_STEP
    # How far the skin may have fallen over the last week, float-adjusted,
    # before no rung of it is opened. 0 turns the check off. See `trend`.
    max_drop: float = 0.05


@dataclass
class Rung:
    """One order: [low, top] at `bid`."""
    low: float
    top: float
    sample: int = 0
    market: float | None = None      # median of sales near the top
    queue_price: float | None = None  # what the standing lots allow
    priced_from: str = ""            # which of the two bound
    exit_net: float | None = None    # after the fee
    ceiling: float | None = None     # most we may pay: the margin floor
    bid: float | None = None         # what we place: one step over the rival
    margin: float | None = None      # if every fill were the worst float
    # What the fills are expected to resell for, and the margin on it. A wide
    # rung also takes good floats from sellers who listed them as ordinary
    # examples; pricing all of them at the top undervalued exactly the rungs
    # that take the most. See `expected_exit`.
    exit_expected: float | None = None
    margin_expected: float | None = None
    rival: float = 0.0               # best competing bid over our range
    lots: int = 0                    # lots listed in the band
    # The band was read to the endpoint's page limit AND the median is above
    # the dearest lot stored, so cheaper-than-median lots may exist that were
    # never seen. The count is then a floor, not a figure.
    lots_capped: bool = False
    # How many lots the band held before either filter. Reported beside the
    # filtered count because "8+" on its own invites the question it is meant
    # to answer: eight of what?
    lots_read: int = 0
    # Their prices, cheapest first - the ones we would actually queue behind,
    # after the lots worse than our top are dropped. Carried because a count
    # alone cannot be checked against anything: "21 lots" is a claim, and the
    # prices are the evidence for it.
    asks: list[float] = field(default_factory=list)
    lots_cleared: float = 0.0        # ...of which the lock disposes
    fills: int = 0                   # past sales our bid would have taken
    lam: float = 0.0                 # fills per day
    # The band our items will be sold into, how fast it sells, and how many
    # of our own are already on their way to it. Together they say how long
    # one of ours stands before a buyer reaches it - see `turnover`.
    lot_lo: float | None = None
    lot_hi: float | None = None
    sell_rate: float = 0.0           # sales a day in that band
    own: int = 0                     # ours bought in the last lock, same band
    t_sell: float | None = None      # days on sale once the lock lifts
    trend: float | None = None       # the skin's last week, float-adjusted
    rank: float = 0.0
    take: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "low": self.low, "top": self.top, "sample": self.sample,
            "market": self.market, "queue_price": self.queue_price,
            "priced_from": self.priced_from, "exit_net": self.exit_net,
            "ceiling": self.ceiling, "bid": self.bid, "margin": self.margin,
            "exit_expected": self.exit_expected,
            "margin_expected": self.margin_expected, "rival": self.rival,
            "lots": self.lots, "lots_cleared": round(self.lots_cleared, 1),
            "lots_capped": self.lots_capped, "lots_read": self.lots_read,
            "asks": list(self.asks),
            "fills": self.fills, "lam": self.lam,
            "lot_lo": self.lot_lo, "lot_hi": self.lot_hi,
            "sell_rate": self.sell_rate, "own": self.own,
            "t_sell": self.t_sell, "trend": self.trend, "rank": self.rank,
            "take": self.take, "reason": self.reason,
        }


# -- small helpers ---------------------------------------------------------

def median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def sale_float(row: dict) -> float | None:
    for key in ("float_value", "float", "wear"):
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def sale_price(row: dict) -> float | None:
    value = row.get("price")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def within(rows: Sequence[dict], window_days: float) -> list[dict]:
    """Sales young enough to count, by the same window the rates divide by.

    Nothing here read `age_days` at all, while the caller loaded ninety days
    of history and every rate divided by `window_days`. A fortnight's setting
    against a quarter's sales overstated both the flow and our own fill rate
    by the ratio between them - over six times at the defaults - and an
    overstated flow is what decides that a sell queue always clears.

    A sale whose date would not parse has no age. It is dropped rather than
    kept: an unknown date cannot be placed inside the window, and counting it
    towards a rate per day is the one reading that is certainly wrong.
    """
    if not window_days or window_days <= 0:
        return list(rows)
    out = []
    for row in rows:
        age = row.get("age_days")
        if age is None:
            continue
        try:
            if float(age) <= window_days:
                out.append(row)
        except (TypeError, ValueError):
            continue
    return out


def in_range(rows: Sequence[dict], lo: float, hi: float) -> list[dict]:
    """Sales whose float lands in [lo, hi]. Both ends inclusive: the bounds
    are the order's own, and CSFloat accepts an item sitting exactly on one."""
    out = []
    for row in rows:
        f = sale_float(row)
        if f is not None and lo <= f <= hi and sale_price(row) is not None:
            out.append(row)
    return out


def rival_bid(orders: Sequence[dict], f: float) -> float:
    """The best standing bid that would accept an item at this float.

    An order with no float bounds accepts anything, which is what the large
    unrestricted orders on a liquid item do - they set a floor under the whole
    wear range.
    """
    best = 0.0
    for order in orders:
        lo = order.get("float_min")
        hi = order.get("float_max")
        lo = 0.0 if lo is None else float(lo)
        hi = 1.0 if hi is None else float(hi)
        if lo <= f <= hi:
            try:
                price = float(order.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            best = max(best, price)
    return best


def top_rival(orders: Sequence[dict], lo: float, hi: float,
              probes: int = 25) -> float:
    """The strongest bid anywhere in our range.

    Sampled rather than solved: the bound that matters is whether we clear the
    range's best rival, and a rival covering a sliver we never sample is one
    covering a sliver of the flow.
    """
    if hi <= lo:
        return rival_bid(orders, lo)
    step = (hi - lo) / max(probes - 1, 1)
    return max(rival_bid(orders, lo + step * i) for i in range(probes))


# -- the four numbers ------------------------------------------------------

def sale_rate(sales: Sequence[dict], lo: float, hi: float,
              window_days: float) -> float:
    """Sales a day through the float range - the whole flow, not our share.

    Used only for the sell queue: how fast the lots ahead of us disappear.
    Not to be confused with `lam`, which is the far smaller rate at which our
    own order would fill.
    """
    if window_days <= 0:
        return 0.0
    return len(in_range(sales, lo, hi)) / window_days


def _pairs(asks: Sequence):
    """(price, float) for each lot, tolerating a bare price list."""
    out = []
    for item in asks or ():
        if isinstance(item, (int, float)):
            out.append((float(item), None))
            continue
        try:
            price = float(item[0])
        except (TypeError, ValueError, IndexError):
            continue
        f = item[1] if len(item) > 1 else None
        out.append((price, None if f is None else float(f)))
    return out


def queue_price(prices: Sequence[float], lots: int, cleared: float,
                step: float) -> float | None:
    """What the standing lots let us sell for, once the lock has run.

    `cleared` lots go during the seven days the item cannot be listed, so they
    cost nothing. We must undercut whatever is still standing: the cheapest of
    the survivors, by one grid step.

    Returns None when the queue clears entirely - then the lots impose no
    ceiling at all and the history median stands.

    `prices` may be empty even when `lots` is not: only the cheapest ask is
    stored today. With the count alone we can still tell that a queue clears;
    when it does not, the cheapest is the one survivor price we know, and
    using it keeps the old, pessimistic answer rather than inventing one.
    """
    n = int(cleared)
    if lots <= n:
        return None                       # nothing left to undercut
    ordered = sorted(p for p in prices if p is not None)
    if len(ordered) > n:
        return ordered[n] - step          # the first survivor, undercut
    return ordered[0] - step if ordered else None


def fills_at(sales: Sequence[dict], orders: Sequence[dict],
             lo: float, hi: float, bid: float) -> list[int]:
    """Indices of the sales our order would have taken: we outbid every rival
    for that float, and the seller accepted no more than we offer.

    Indices, not rows: two sales can be identical in every field, and matching
    them by value would let one rung's claim swallow another's.

    The second filter is the one that bites. On a live item it cut 80 sales to
    2: the rest went for more than we could pay, and a seller who got more had
    no reason to come to us.
    """
    out = []
    for i, row in enumerate(sales):
        price = sale_price(row)
        f = sale_float(row)
        if price is None or f is None or not (lo <= f <= hi):
            continue
        if rival_bid(orders, f) < bid and price <= bid:
            out.append(i)
    return out


def snap_up(price: float, step: float) -> float:
    """Up to the grid: a bid between ticks is not one CSFloat will take, and a
    bid rounded DOWN onto a rival's own price does not lead them."""
    if step <= 0:
        return price
    ticks = int(price / step + 1e-9)
    return ticks * step if abs(ticks * step - price) < 1e-9 else (ticks + 1) * step


def snap_down(price: float, step: float) -> float:
    """Down to the grid: a bid between ticks is not one CSFloat will take.

    Down rather than to nearest, because rounding up would quietly spend the
    margin the floor was there to protect.
    """
    if step <= 0:
        return price
    return int(price / step + 1e-9) * step


def bucket_medians(sales: Sequence[dict], step: float = TOP_STEP,
                   least: int = BUCKET_MIN_SALES) -> dict[int, float]:
    """Median price per hundredth of float, where there are enough sales."""
    groups: dict[int, list[float]] = {}
    for row in sales:
        f, price = sale_float(row), sale_price(row)
        if f is None or price is None:
            continue
        groups.setdefault(int(f / step + 1e-9), []).append(price)
    return {k: median(v) for k, v in groups.items() if len(v) >= least}


def expected_exit(rung: Rung, sales: Sequence[dict], picked: Sequence[int],
                  params: Params) -> float | None:
    """What the items this rung would have bought resell for, on average,
    before the fee.

    The ceiling assumes the worst: every fill is the worst float the order
    accepts, priced at the rung's top. That is the right bound for what we may
    pay, and it is how a narrow rung actually fills. A wide one does not only
    fill at its top. Sellers who list a good float as an ordinary example of
    the skin sell into it too, and those items resell at their own float's
    price. The past sales this rung would have taken say which floats arrive,
    so each is valued where it lands:

      at the top    the exit already worked out - the median or the queue,
                    whichever binds;
      below it      the median of its own hundredth, when that has enough
                    sales; otherwise the top's exit, never more.

    No queue is read for the lower hundredths: the lots stored are the top's
    band. That leaves them at the history median, which the top's own queue
    check showed is usually the one that binds anyway.
    """
    if not picked or rung.exit_net is None:
        return None
    top_exit = rung.exit_net / (1 - params.fee) if params.fee < 1 else rung.exit_net
    medians = bucket_medians(sales, params.top_step)
    edge = rung.top - params.top_step
    values = []
    for i in picked:
        f = sale_float(sales[i])
        if f is None or f > edge - 1e-9:
            values.append(top_exit)
            continue
        own = medians.get(int(f / params.top_step + 1e-9))
        values.append(top_exit if own is None else own)
    return sum(values) / len(values)


def price_fills(rung: Rung, sales: Sequence[dict], picked: Sequence[int],
                params: Params) -> None:
    """Set the expected exit and margin from the sales this rung takes."""
    gross = expected_exit(rung, sales, picked, params)
    if gross is None or not rung.bid:
        rung.exit_expected = rung.margin_expected = None
        return
    rung.exit_expected = gross * (1 - params.fee)
    rung.margin_expected = (rung.exit_expected - rung.bid) / rung.bid


def turnover(rung: Rung, lock_days: float) -> bool:
    """Days on sale and the rank that follows from them. False when the band
    cannot sell what we would buy into it.

    The rank is what a dollar earns per day it is tied up:

        rank = margin / (lock_days + t_sell)

    CSFloat takes the money when an order fills, not when it is placed - the
    outstanding orders may run to ten times the balance, and one whose turn
    comes while the balance is short is simply removed. So the days an order
    stands waiting cost nothing; the money is in the balance, paying for other
    fills. It is tied up from the fill: seven days of lock, then however long
    the item stands before somebody buys it. How often the rung fills does not
    change what a dollar in it earns - it changes how many dollars the rung can
    use, which is the executor's business, not the rank's.

    `lam × margin` was the rank before this. It is right when the ten-times
    allowance runs out first, and it never does: a rung that fills at all has
    lam of at least one in the window, and then lam × 7 already exceeds the
    tenth of a dollar of balance each dollar of allowance stands for.
    `margin / (1/lam + 7)` charged the wait for a fill as capital, which it
    would be if placing an order reserved the money. On CSFloat it does not.

    t_sell. Our lot is listed a step under the first survivor, so it stands at
    the front and every buyer in the band reaches it first. What stands in
    front of it is our own: the items bought in the last lock come out of it
    into the same band, one after another, at the rate we buy. That is a
    queue, and its wait is

        t_sell = (own + 1) / (sell_rate - lam)

    with `own` the ones already bought. Buying about as fast as the band sells
    and the wait runs away; faster, and it never ends. Other rungs of ours
    selling into the same band join the same queue - which of them are held
    together is decided later, so the executor adds them (`tied_up`).
    """
    spare = rung.sell_rate - rung.lam
    if spare <= 1e-12:
        rung.t_sell = None
        rung.rank = 0.0
        return False
    rung.t_sell = (rung.own + 1) / spare
    margin = rung.margin_expected if rung.margin_expected is not None \
        else (rung.margin or 0.0)
    rung.rank = margin / (lock_days + rung.t_sell)
    return True


def _jammed(rung: Rung) -> str:
    return (f"полоса не успевает продавать: покупали бы {rung.lam:.2f}/сут, "
            f"а в ней продаётся {rung.sell_rate:.2f}/сут")


# -- the ladder ------------------------------------------------------------

def price_step(price: float) -> float:
    """CSFloat's bid grid, which widens with the price."""
    if price < 5:      return 0.01
    if price < 10:     return 0.05
    if price < 100:    return 0.10
    if price < 500:    return 1.00
    if price < 1000:   return 5.00
    return 10.00


def evaluate(top: float, sales: Sequence[dict], orders: Sequence[dict],
             span: tuple[float, float], lots: int,
             asks: Sequence, params: Params,
             lot_span: tuple[float, float] | None = None,
             own: Sequence[float] = ()) -> Rung:
    """One rung of the ladder: the order [span_low, top], priced.

    `own` is the tops of our orders that filled within the last lock: those
    items are still locked, unsold for certain, and will stand in the same
    sell queue as this rung's. Their top stands in for their float, as it does
    everywhere in the model.

    `lot_span` is the float range the listings sit in, which is NOT the rung's
    range: the rung reaches down to the wear minimum while the lots are banded
    far more narrowly. The queue drains at the rate of ITS OWN band, and
    measuring that rate over the rung's whole range counted sales that will
    never touch those lots.

    `sales` must already be windowed - `ladder` does it once for the whole
    climb, and `pricing.evaluate` does it for a single held order. Everything
    here divides by `window_days`, so a longer history reaching this far would
    be counted at the wrong rate.
    """
    low = span[0]
    rung = Rung(low=low, top=top, lots=lots)

    # What it resells for: the sales that resemble what we will receive.
    near = in_range(sales, max(low, top - params.top_step), top)
    rung.sample = len(near)
    if rung.sample < params.min_sample:
        rung.reason = (f"мало продаж у верха: {rung.sample} "
                       f"при пороге {params.min_sample}")
        return rung
    rung.market = median([sale_price(r) for r in near])

    # What the queue allows, once the lock has run. The rate is the lots'
    # own band, not the rung's reach.
    q_lo, q_hi = lot_span or (max(low, top - params.top_step), top)
    rate = sale_rate(sales, q_lo, q_hi, params.window_days)
    rung.lots_cleared = rate * params.lock_days
    rung.lot_lo, rung.lot_hi = q_lo, q_hi
    rung.sell_rate = rate
    rung.own = sum(1 for f in own if f is not None and q_lo < f <= q_hi + 1e-9)
    step = price_step(rung.market)
    # Only the lots our order would actually queue behind. A depth band is
    # 0.02 wide while the top moves by 0.01, so half of one can be worse items
    # than we would be selling; a buyer who wants our float cannot use those,
    # and letting them set our price cost a band three dollars of exit. A lot
    # whose float was never recorded is kept: unknown counts against us.
    pairs = _pairs(asks)
    if len(pairs) >= lots > 0:
        # The whole band is on hand, so the ones we would not queue behind can
        # be dropped and the count reduced with them. Two tests, not one:
        #
        #   float - a lot worse than our top is a worse item, and the buyer
        #           who wants ours cannot use it;
        #   price - a lot dearer than what ours is worth does not block us at
        #           all. We list under it and the buyer reaches us first.
        #
        # Only the second was missing, and it counted the whole band as if it
        # stood in our way - on a liquid item that is fifty lots where a dozen
        # are actually ahead of us.
        #
        # The median is the reference rather than the exit price, which is not
        # known yet and is derived from this count. It is also the safe end of
        # the two: pricing under it only shortens the queue further.
        ours = [p for p, f in pairs
                if (f is None or f <= top)
                and (rung.market is None or p <= rung.market)]
        rung.lots = len(ours)
    else:
        # Partial: older rows kept only the cheapest ask. Dropping the count
        # to match would read as a queue that clears, which is the optimistic
        # direction and the wrong one - so the recorded count stands and the
        # prices we do have are used as they were before.
        ours = [p for p, _ in pairs]
        rung.lots = lots
    rung.asks = sorted(ours)
    # A band read to the endpoint's page limit holds only its cheapest fifty,
    # and there may be more behind them. That is harmless while the median
    # sits under the dearest lot we did store - everything unseen is dearer
    # still, so none of it would have joined the queue. It stops being
    # harmless the moment the median clears that price: then the lots we never
    # saw could be under it, and the count is a floor rather than a figure.
    dearest = max((p for p, _ in pairs), default=None)
    rung.lots_read = len(pairs)
    rung.lots_capped = bool(
        len(pairs) >= DEPTH_PAGE and dearest is not None
        and rung.market is not None and rung.market > dearest)
    rung.queue_price = queue_price(ours, rung.lots, rung.lots_cleared, step)

    if rung.queue_price is not None and rung.queue_price < rung.market:
        exit_gross, rung.priced_from = rung.queue_price, "очередь"
    else:
        exit_gross, rung.priced_from = rung.market, "история"
    rung.exit_net = exit_gross * (1 - params.fee)

    # Two prices, and the difference between them is the room to fight.
    #
    # The ceiling is the highest bid that still clears the margin floor. We
    # never pay more than that, whoever else is bidding.
    #
    # The bid is the cheapest price that leads: one step over the best rival
    # for the lot we will be handed. Paying more than it takes to lead buys
    # nothing on the sellers already willing to come to us - and what it does
    # buy, reach over the ones who want more, is bought again later and only
    # when someone actually forces it. With nobody to lead, there is nothing
    # to undercut, so the ceiling itself is the bid and the reach is taken.
    step = price_step(rung.exit_net)
    rung.ceiling = snap_down(rung.exit_net / (1 + params.min_margin), step)
    if rung.ceiling <= 0:
        rung.reason = "цена выхода слишком мала"
        return rung

    # What has to be beaten for the item we will actually receive - the one at
    # the top. Deliberately NOT the strongest bid anywhere in the range: a
    # rival scoped to a sliver of it takes a sliver of the flow, not the rung.
    # Judging every rung against the range's best bid rejected all 23 tops of
    # a live item because one order covered 0.15-0.16.
    rung.rival = rival_bid(orders, top)

    if rung.rival > 0:
        lead = snap_up(rung.rival + price_step(rung.rival), price_step(rung.rival))
        if lead > rung.ceiling + 1e-9:
            rung.bid = rung.ceiling
            rung.margin = (rung.exit_net - rung.bid) / rung.bid
            rung.reason = (f"перебить стоит ${lead:.2f}, а маржа позволяет "
                           f"лишь ${rung.ceiling:.2f}")
            return rung
        # One step over the rival, and no further. Paying more than it takes
        # to lead buys nothing from a seller who is coming to us anyway.
        #
        # This used to walk up from the lead to the first price the history
        # said somebody had actually sold at, on the grounds that leading a
        # queue nobody joins fills nothing. That reasoning is sound and the
        # remedy was wrong: it quietly paid four dollars over a rival who
        # could be led for one. The case it guarded against is still caught,
        # one line down - a bid no past sale would have met takes nothing,
        # `fills` comes out zero, and the rung is refused by name rather than
        # bid up to a price that makes the history agree.
        rung.bid = lead
    else:
        rung.bid = rung.ceiling
    rung.margin = (rung.exit_net - rung.bid) / rung.bid

    mine = fills_at(sales, orders, low, top, rung.bid)
    rung.fills = len(mine)
    if not rung.fills:
        cheap = [r for r in in_range(sales, low, top)
                 if (sale_price(r) or 0) <= rung.bid]
        rung.reason = (
            f"конкуренты перебивают: {len(cheap)} дешёвых продаж, все ушли им"
            if cheap else f"по ${rung.bid:.2f} никто не продавал")
        return rung

    rung.lam = rung.fills / params.window_days if params.window_days else 0.0
    price_fills(rung, sales, mine, params)
    if not turnover(rung, params.lock_days):
        rung.reason = _jammed(rung)
        return rung
    rung.take = True
    return rung


def lots_in_band(depth: Sequence[dict], top: float):
    """The listings we would queue behind: the band our top lands IN.

    Bands touch at their edges, and the 0.01 scan puts a top on one often, so
    two can match. The containing band is the lower of them - our item sits at
    its top, not at the bottom of the next. Taking the higher band priced a
    rung off lots it would never compete with and cost it three dollars of
    exit on a live item.

    Returns (count, prices, span). `prices` falls back to the cheapest ask
    alone, which is all that is stored today.
    """
    bands: list[tuple[float, float, tuple[int, list[float]]]] = []
    for row in depth:
        try:
            band_lo = float(row["float_min"])
            band_hi = float(row["float_max"])
        except (KeyError, TypeError, ValueError):
            continue
        count = int(row.get("listings") or 0)
        asks: list[tuple[float, float | None]] = []
        for pair in (row.get("asks") or []):
            try:
                price = float(pair[0])
            except (TypeError, ValueError, IndexError):
                continue
            try:
                f = float(pair[1]) if pair[1] is not None else None
            except (TypeError, ValueError, IndexError):
                f = None
            asks.append((price, f))
        if not asks and row.get("cheapest") is not None:
            # Written before the column existed: the minimum is all there is,
            # and its float is unknown, so it is kept and counted against us.
            asks = [(float(row["cheapest"]), None)]
        bands.append((band_lo, band_hi, (count, sorted(asks))))
    bands.sort()
    for band_lo, band_hi, value in bands:
        if band_lo < top <= band_hi:
            return value + ((band_lo, band_hi),)
    for band_lo, band_hi, value in bands:          # top on the very first edge
        if band_lo <= top <= band_hi:
            return value + ((band_lo, band_hi),)
    return (0, [], None)


def ladder(sales: Sequence[dict], orders: Sequence[dict],
           span: tuple[float, float] | None,
           depth: Sequence[dict] = (),
           params: Params | None = None,
           own: Sequence[float] = ()) -> list[Rung]:
    """Every candidate top, scored - the rejected ones too.

    A rejected rung keeps its reason: "why not this one" is the question the
    table is read for, and dropping them silently makes an over-bid top
    indistinguishable from one nobody looked at.

    Rungs share one flow. A sale both accept goes to the dearer rung, so the
    tops are walked from tight to wide and each claims what it takes; a wider
    rung is then credited only with what the tighter ones left. Crediting each
    with the whole range would count the same sale several times and make the
    lower rungs look better than they are.

    A skin that fell more than `max_drop` over the last week has every rung
    scored and none taken: the numbers are still worth reading, and "why not"
    is answered by the fall. Only opening is refused - an order already
    standing is priced by `evaluate` alone, and the defence decides about it.
    """
    p = params or Params()
    if not span:
        return []
    low, high = span
    # Before the windowing below: the trend compares the last week with the
    # three before it, which reaches past a fortnight's window.
    drift = weekly((r.get("age_days"), sale_float(r), sale_price(r))
                   for r in sales)
    # Windowed once, here, rather than in every rate: the caller loads far
    # more history than the window so a thin band still has a median to read,
    # and dividing that wider count by the narrower window is what overstated
    # every flow in the model.
    sales = within(sales, p.window_days)

    out: list[Rung] = []
    claimed: set[int] = set()
    top = round(low + p.top_step, 4)
    while top <= high + 1e-9:
        lots, prices, lot_span = lots_in_band(depth, top)
        rung = evaluate(top, sales, orders, (low, high), lots, prices, p,
                        lot_span=lot_span, own=own)
        rung.trend = drift.change
        if rung.take:
            mine = [i for i in fills_at(sales, orders, low, top, rung.bid)
                    if i not in claimed]
            if not mine:
                rung.take = False
                rung.reason = "весь поток забрали ступени выше"
                rung.fills = rung.lam = rung.rank = 0
            else:
                claimed |= set(mine)
                rung.fills = len(mine)
                rung.lam = rung.fills / p.window_days if p.window_days else 0.0
                # Valued on the sales this rung actually keeps, not the ones
                # a tighter rung took first.
                price_fills(rung, sales, mine, p)
                turnover(rung, p.lock_days)
        if (rung.take and p.max_drop > 0 and drift.change is not None
                and drift.change <= -p.max_drop):
            rung.take = False
            rung.reason = (f"скин дешевеет: {drift.change * 100:+.1f}% за неделю "
                           f"с поправкой на float, порог −{p.max_drop * 100:.0f}% "
                           f"({drift.recent} продаж против {drift.base})")
        out.append(rung)
        top = round(top + p.top_step, 4)

    out.sort(key=lambda r: (not r.take, -r.rank, r.top))
    return out
