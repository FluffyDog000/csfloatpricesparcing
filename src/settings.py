"""Scoring thresholds and money limits, read the same way everywhere.

The dashboard decides them and the collector acts on them, so they cannot be
read differently by the two: a defence that used its own idea of the ceiling
would withdraw from positions the page still shows as held.

Values out of range are pulled to the nearest sane one rather than rejected.
A band step of 0.5 spans an entire wear and a minimum flow of 3/day passes
nothing; both come back as an empty report that reads as "no opportunities
here" rather than "your threshold did that".
"""
from __future__ import annotations

from typing import Any

from .executor import Limits
from .pricing import Params

PARAM_BOUNDS = {
    "fee": (0.0, 0.20), "min_margin": (0.0, 1.0),
    "window_days": (1.0, 365.0), "band_step": (0.005, 0.23),
    "min_lambda": (0.0, 10.0), "min_wars": (0, 100),
    "max_fill_days": (1.0, 365.0), "min_sample": (1, 1000),
    "bid_tolerance": (0.0, 1.0), "sigma_k": (0.0, 5.0),
}
PARAM_KEYS = (
    ("an_fee", "fee", float), ("an_min_margin", "min_margin", float),
    ("an_window", "window_days", float), ("an_step", "band_step", float),
    ("an_min_lambda", "min_lambda", float), ("an_min_wars", "min_wars", int),
    ("an_max_fill", "max_fill_days", float), ("an_min_sample", "min_sample", int),
    ("an_bid_tol", "bid_tolerance", float), ("an_sigma_k", "sigma_k", float),
)

LIMIT_BOUNDS = {
    "total_capital": (0.0, 1_000_000.0), "per_item_capital": (0.0, 1_000_000.0),
    "max_orders": (0, 1000), "max_orders_per_item": (0, 1000),
    "patience_minutes": (0.0, 525_600.0),
    "balance": (0.0, 1_000_000.0),
}
LIMIT_KEYS = (
    ("an_total_capital", "total_capital", float),
    ("an_per_item_capital", "per_item_capital", float),
    ("an_max_orders", "max_orders", int),
    ("an_max_per_item", "max_orders_per_item", int),
    ("an_patience_min", "patience_minutes", float),
    ("an_balance", "balance", float),
)

# How often the defence looks, and the floor under it. Each pass re-reads the
# book of every item we hold an order on, which costs real quota, so a minute
# here is not a free choice.
DEFEND_BOUNDS = (10.0, 1440.0)
DEFEND_DEFAULT = 60.0


def _fill(db, target: Any, keys, bounds) -> Any:
    for key, attr, cast in keys:
        raw = db.get_setting(key)
        if raw in (None, ""):
            continue
        try:
            value = cast(str(raw).strip().replace(",", "."))
        except (TypeError, ValueError):
            continue
        lo, hi = bounds[attr]
        setattr(target, attr, cast(min(max(value, lo), hi)))
    return target


def params(db) -> Params:
    return _fill(db, Params(), PARAM_KEYS, PARAM_BOUNDS)


def limits(db) -> Limits:
    out = _fill(db, Limits(), LIMIT_KEYS, LIMIT_BOUNDS)
    if db.get_setting("an_patience_min") in (None, ""):
        # Patience used to be set in days. Reading the old key as minutes would
        # turn "wait up to 14 days" into "wait a quarter of an hour" silently,
        # which is a live position bid up to its ceiling before anyone notices.
        old = db.get_setting("an_patience")
        try:
            out.patience_minutes = float(str(old).replace(",", ".")) * 1440.0
        except (TypeError, ValueError):
            pass
    return out


def defend_minutes(db) -> float:
    raw = db.get_setting("an_defend_minutes")
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return DEFEND_DEFAULT
    lo, hi = DEFEND_BOUNDS
    return min(max(value, lo), hi)


def defending(db) -> bool:
    """Automatic defence is off until it is turned on deliberately."""
    return (db.get_setting("an_defend") or "0") == "1"


def dry_run(db) -> bool:
    return (db.get_setting("analysis_dry_run", "1") or "1") != "0"
