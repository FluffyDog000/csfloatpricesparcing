"""The global brake on polling, and a timestamp helper.

How often each item is polled is decided per item in schedule.py. This keeps
what applies to all of them at once: the multiplier a 429 sets, and how it
eases off again.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Global pace multiplier (AIMD): grows on a 429, decays after a clean stretch.
PACE_UP_FACTOR = 1.5
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
