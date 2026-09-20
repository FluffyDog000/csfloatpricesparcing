"""The filter that runs before a single request is spent.

Scoring an item needs its order book, and reading one costs roughly six
requests through the cookie and the residential route - the expensive, rate
limited path that once drew "too many requests from too many IPs". Three
hundred items is well over an hour of continuous asking.

So the first filter has to be the free one. Everything here is worked out from
sales history already in the database: the price, how often the thing trades,
whether it still trades at all, how widely the price swings, and whether there
is any gap between what it sells for and what one could pay. An item that
fails these would have failed after the requests too.

The defaults are deliberately wide. A screen that silently throws items away
before anyone has chosen its thresholds is worse than no screen: it looks like
the market said no.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass
class Screen:
    """Thresholds for the free pass. Zero means "do not check this"."""
    min_price: float = 0.0        # median sale price, dollars
    max_price: float = 0.0
    min_flow: float = 0.0         # sales per day over the window
    max_quiet_days: float = 0.0   # since the last sale
    max_spread: float = 0.0       # IQR / median
    min_gap: float = 0.0          # headroom between the exit and the cheap end
    min_sales: int = 0            # rows of history, any age

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Verdict:
    passed: bool
    reason: str = ""
    median: float | None = None
    flow: float | None = None
    quiet_days: float | None = None
    spread: float | None = None
    gap: float | None = None
    sales: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def profile(sales: Sequence[dict], window_days: float = 28.0,
            fee: float = 0.02) -> Verdict:
    """What the history says about an item, before any thresholds are applied.

    Measured, not judged: the same numbers are shown beside an item whether it
    passes or not, because "why was this dropped" is answered by the value, not
    by the word "dropped".
    """
    prices = [float(s["price"]) for s in sales
              if s.get("price") is not None]
    out = Verdict(True, sales=len(prices))
    if not prices:
        return Verdict(False, "нет истории продаж", sales=0)

    out.median = st.median(prices)

    ages = [s["age_days"] for s in sales if s.get("age_days") is not None]
    if ages:
        out.quiet_days = min(ages)
        recent = [p for p, s in zip(prices, sales)
                  if s.get("age_days") is not None
                  and s["age_days"] <= window_days]
        out.flow = len(recent) / window_days if window_days > 0 else None

    if len(prices) >= 4:
        ordered = sorted(prices)
        n = len(ordered)
        q1 = ordered[n // 4]
        q3 = ordered[(3 * n) // 4 - (1 if n % 4 == 0 else 0)]
        out.spread = (q3 - q1) / out.median if out.median else None

    # Is there room for a trade at all? The tenth percentile stands in for
    # "what the cheap end of the market goes for": if selling at the median,
    # less the fee, does not clear even that, no bid can be both low enough to
    # fill and high enough to profit - and no request would have said otherwise.
    if len(prices) >= 5:
        cheap = sorted(prices)[max(0, len(prices) // 10)]
        if cheap > 0:
            out.gap = (out.median * (1.0 - fee)) / cheap - 1.0
    return out


def screen(verdict: Verdict, limits: Screen) -> Verdict:
    """Apply the thresholds to a measured profile."""
    v = verdict
    checks = [
        (limits.min_sales and v.sales < limits.min_sales,
         lambda: f"продаж {v.sales} < {limits.min_sales}"),
        (limits.min_price and (v.median or 0) < limits.min_price,
         lambda: f"медиана ${v.median:.2f} дешевле ${limits.min_price:.2f}"),
        (limits.max_price and (v.median or 0) > limits.max_price,
         lambda: f"медиана ${v.median:.2f} дороже ${limits.max_price:.2f}"),
        (limits.min_flow and (v.flow or 0.0) < limits.min_flow,
         lambda: f"поток {v.flow or 0:.2f}/сут < {limits.min_flow:.2f}"),
        (limits.max_quiet_days and v.quiet_days is not None
         and v.quiet_days > limits.max_quiet_days,
         lambda: f"последняя продажа {v.quiet_days:.0f} дн назад"),
        (limits.max_spread and v.spread is not None
         and v.spread > limits.max_spread,
         lambda: f"разброс {v.spread:.2f} > {limits.max_spread:.2f}"),
        (limits.min_gap and v.gap is not None and v.gap < limits.min_gap,
         lambda: f"зазор {v.gap * 100:.1f}% < {limits.min_gap * 100:.1f}%"),
    ]
    for failed, why in checks:
        if failed:
            return Verdict(False, why(), v.median, v.flow, v.quiet_days,
                           v.spread, v.gap, v.sales)
    return v


def look(sales: Sequence[dict], limits: Screen, window_days: float = 28.0,
         fee: float = 0.02) -> Verdict:
    """Measure, then judge."""
    measured = profile(sales, window_days, fee)
    if not measured.passed:
        return measured
    return screen(measured, limits)
