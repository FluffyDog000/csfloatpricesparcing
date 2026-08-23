"""Adaptive poll pacing: how often each item really needs to be polled.

CSFloat only exposes the last 40 sales, so an item must be polled before 40 new
sales pile up — but polling a skin that sells twice a day every 15 minutes just
burns requests (and trips the rate limit). These helpers turn an item's observed
sale rate into a sensible interval.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Aim for this many new sales between polls: a quarter of the 40-sale window,
# so nothing is missed even if the item suddenly speeds up.
TARGET_SALES_PER_POLL = 10.0
RATE_WINDOW_DAYS = 14        # history used to estimate the sale rate
MIN_SALES_FOR_RATE = 5       # below this we can't judge; use the plain interval
ADAPTIVE_MAX_MINUTES = 120.0  # never stretch an item beyond this (freshness)

# Global pace multiplier (AIMD): grows on a 429, decays after a clean stretch.
PACE_UP_FACTOR = 1.5
PACE_DOWN_FACTOR = 0.9
PACE_MAX = 8.0
PACE_RECOVER_SECONDS = 3600.0   # one clean hour before easing back up


def parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def window_start(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=RATE_WINDOW_DAYS)).replace(microsecond=0).isoformat()


def adaptive_minutes(
    count: int,
    first_sold: object,
    floor_minutes: float,
    ceiling_minutes: float = ADAPTIVE_MAX_MINUTES,
    target_sales: float = TARGET_SALES_PER_POLL,
    now: datetime | None = None,
) -> float | None:
    """Minutes between polls so ~target_sales accumulate each time.

    Returns None when the item has too little history to judge — the caller
    should fall back to the plain configured interval."""
    now = now or datetime.now(timezone.utc)
    if count < MIN_SALES_FOR_RATE:
        return None
    t0 = parse_iso(first_sold)
    if t0 is None:
        return None
    hours = (now - t0).total_seconds() / 3600.0
    if hours <= 0:
        return None
    per_hour = count / hours
    if per_hour <= 0:
        return None
    minutes = (target_sales / per_hour) * 60.0
    return min(max(minutes, floor_minutes), ceiling_minutes)
