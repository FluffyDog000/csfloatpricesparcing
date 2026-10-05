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
from .screen import Screen

# The settings the model reads, and no others. The band width, the
# minimum flow, the fill deadline, the outbid reserve, the sigma multiplier and
# the borrowing reach all described machinery that no longer exists; their
# stored values are simply ignored rather than silently steering nothing.
PARAM_BOUNDS = {
    "fee": (0.0, 0.20), "min_margin": (0.0, 1.0),
    "window_days": (1.0, 365.0), "min_sample": (1, 1000),
    "max_drop": (0.0, 1.0),
    "adaptive": (0, 1),
    "careful": (0.0, 1.0),
}
PARAM_KEYS = (
    ("an_fee", "fee", float), ("an_min_margin", "min_margin", float),
    ("an_window", "window_days", float),
    ("an_min_sample", "min_sample", int),
    ("an_max_drop", "max_drop", float),
    ("an_adaptive", "adaptive", int),
    ("an_careful", "careful", float),
)

LIMIT_BOUNDS = {
    "total_capital": (0.0, 1_000_000.0), "per_item_capital": (0.0, 1_000_000.0),
    "max_orders": (0, 1000), "max_orders_per_item": (0, 1000),
    "patience_minutes": (0.0, 525_600.0),
    "balance": (0.0, 1_000_000.0),
    "guard_share": (0.0, 1.0),
    "surge_z": (0.0, 3.0),
    "full_allowance": (0, 1),
    "max_quantity": (1, 50),
    "order_days": (0.5, 30.0),
    "leverage": (1.0, 10.0),
}
LIMIT_KEYS = (
    ("an_total_capital", "total_capital", float),
    ("an_per_item_capital", "per_item_capital", float),
    ("an_max_orders", "max_orders", int),
    ("an_max_per_item", "max_orders_per_item", int),
    ("an_patience_min", "patience_minutes", float),
    ("an_balance", "balance", float),
    ("an_guard_share", "guard_share", float),
    ("an_surge_z", "surge_z", float),
    ("an_full_allowance", "full_allowance", int),
    ("an_max_quantity", "max_quantity", int),
    ("an_order_days", "order_days", float),
    ("an_leverage", "leverage", float),
)

# The balance read off the account (`Collector.read_balance`), in dollars,
# and when. Used in place of the typed one while it is this fresh: older, and
# the bot has not been able to look - the typed figure is the better guess.
BALANCE_KEY = "account_balance"
BALANCE_AT_KEY = "account_balance_at"
BALANCE_FRESH_HOURS = 6.0


def live_balance(db) -> tuple[float | None, str | None]:
    """(dollars, when read) of the account's balance, when it is fresh."""
    from datetime import datetime, timedelta, timezone

    from .pacing import parse_iso

    raw, at = db.get_setting(BALANCE_KEY), db.get_setting(BALANCE_AT_KEY)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, None
    when = parse_iso(at)
    if when is None or datetime.now(timezone.utc) - when > timedelta(
            hours=BALANCE_FRESH_HOURS):
        return None, at
    return value, at

# The free pass over sales history, before any request is spent. Wide by
# default on purpose: a screen that drops items before anyone has chosen its
# thresholds reads as the market saying no.
SCREEN_BOUNDS = {
    "min_price": (0.0, 100_000.0), "max_price": (0.0, 100_000.0),
    "min_flow": (0.0, 100.0), "max_quiet_days": (0.0, 3650.0),
    "min_sales": (0, 100_000),
}
SCREEN_KEYS = (
    ("scr_min_price", "min_price", float),
    ("scr_max_price", "max_price", float),
    ("scr_min_flow", "min_flow", float),
    ("scr_quiet", "max_quiet_days", float),
    ("scr_min_sales", "min_sales", int),
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
    out.balance_typed = out.balance
    live, _ = live_balance(db)
    if live is not None:
        out.balance = live
        out.balance_live = True
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


def screen_limits(db) -> Screen:
    return _fill(db, Screen(), SCREEN_KEYS, SCREEN_BOUNDS)


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
