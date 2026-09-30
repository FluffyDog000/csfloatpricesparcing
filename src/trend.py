"""Whether the skin as a whole is falling, with the float taken out.

The risk nothing else in the model covers is the market moving while the item
sits out its seven-day lock. The margin floor is the only defence against a
wrong exit, and it is the same whatever the skin is doing - so a skin that lost
five percent last week spends the whole floor before our item can even be
listed.

Two choices decide whether the number means anything.

Per skin, not per rung. The rung's own window holds the eight-odd sales at its
top; split into "before" and "now" that is four against four, and a median of
four moves further on noise than the market moves in a week.

Float-adjusted, not a raw median. Two sales of the same skin differ in price by
half on float alone, so a week that happened to trade worn examples reads as a
fall that never happened. Each sale is therefore divided by the median of its
own hundredth of float over the whole period, and the trend is how that ratio
moved: the pool of sales is the whole skin's, and the mix of floats in it no
longer matters.

What this cannot see is the float premium itself shifting - low floats gaining
while the skin stands still. That needs more history per hundredth than there
is today.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass
from typing import Iterable

# The lock is seven days, so the question is what the last seven did. Compared
# against the three weeks before them: long enough to be a level, short enough
# to be the same market.
RECENT_DAYS = 7.0
BASE_DAYS = 21.0
BUCKET = 0.01
# A hundredth with fewer sales than this has no median worth dividing by: one
# sale divided by itself is 1.0 whatever the market did.
MIN_BUCKET = 3
# Sales needed on each side before the comparison is a number.
MIN_SIDE = 5


@dataclass
class Trend:
    change: float | None      # -0.07 = the skin is 7% cheaper than it was
    recent: int = 0           # sales behind the "now" side
    base: int = 0             # and behind the "before" side

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def weekly(sales: Iterable[tuple], recent_days: float = RECENT_DAYS,
           base_days: float = BASE_DAYS, bucket: float = BUCKET) -> Trend:
    """How the skin's float-adjusted price moved over the last `recent_days`.

    `sales` is (age_days, float, price) per sale; rows missing any of the three
    are skipped. `change` is None when either side is too thin to say.
    """
    horizon = recent_days + base_days
    rows = []
    for age, f, price in sales:
        if age is None or f is None or price is None:
            continue
        try:
            age, f, price = float(age), float(f), float(price)
        except (TypeError, ValueError):
            continue
        if 0 <= age <= horizon and price > 0:
            rows.append((age, int(f / bucket + 1e-9), price))

    by_bucket: dict[int, list[float]] = {}
    for _, key, price in rows:
        by_bucket.setdefault(key, []).append(price)
    level = {key: st.median(prices) for key, prices in by_bucket.items()
             if len(prices) >= MIN_BUCKET}

    recent, base = [], []
    for age, key, price in rows:
        if key not in level:
            continue
        (recent if age <= recent_days else base).append(price / level[key])

    out = Trend(None, len(recent), len(base))
    if len(recent) < MIN_SIDE or len(base) < MIN_SIDE:
        return out
    out.change = st.median(recent) / st.median(base) - 1.0
    return out
