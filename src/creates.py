"""How many orders may still be created today.

CSFloat allows 200 order creations a day per key, the day counted from the
first creation; amends and cancels come out of a separate, far larger count.
A successful creation carries no counter in its reply - only a refusal does -
so the bot counts its own: every order it placed in the last 24 hours.

A rolling day is the cautious reading of a fixed one. CSFloat's window began
at most a day ago, so it holds no more creations than the last 24 hours do:
the count here is never below the real one, and "left" never above it. When
CSFloat has said otherwise - a refusal with its counter - that wins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

DAILY_CREATES = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def status(db, now: datetime | None = None) -> dict:
    """{"limit", "used", "left", "reset"} - reset an ISO time or None."""
    from .pacing import parse_iso

    now = now or _now()
    since = (now - timedelta(days=1)).replace(microsecond=0).isoformat()
    made = [e for e in db.order_events(limit=5000, include_dry=False,
                                       since=since)
            if e["ok"] and e["kind"] == "place"]
    used = len(made)
    left = max(DAILY_CREATES - used, 0)
    reset = None
    if made:
        oldest = min(parse_iso(e["at"]) for e in made if parse_iso(e["at"]))
        reset = (oldest + timedelta(days=1)).replace(microsecond=0).isoformat()

    # What CSFloat itself last said about the main key's create counter.
    try:
        state = json.loads(db.get_setting("main_key_state") or "[]")
    except ValueError:
        state = []
    for row in state:
        if row.get("kind") != "create" or row.get("remaining") is None:
            continue
        at = row.get("reset")
        if at and float(at) > now.timestamp():
            left = min(left, int(row["remaining"]))
            reset = datetime.fromtimestamp(float(at), timezone.utc) \
                .replace(microsecond=0).isoformat()
    return {"limit": DAILY_CREATES, "used": used, "left": left, "reset": reset}


def cap_places(actions: list[dict], left: int) -> tuple[list[dict], int]:
    """Every action but placements, and placements up to `left` in the order
    given (best rank first). Returns (kept, deferred)."""
    kept, places = [], 0
    deferred = 0
    for a in actions:
        if a.get("kind") != "place":
            kept.append(a)
            continue
        if places < left:
            kept.append(a)
            places += 1
        else:
            deferred += 1
    return kept, deferred
