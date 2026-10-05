"""The brake: too much bought in one day takes every order down.

Steam bots carry the same rule as "the balance fell by N% in a day". On CSFloat
the balance falls for exactly one reason we control - orders filling - so the
rule is kept and measured where it happens: the fills the account sync has
seen in the last twenty-four hours, against the balance.

What it is for. A normal day buys a small, steady share: with the money spent
the way the plan spends it, a dollar comes back after the lock and the sale,
so a day's fills are roughly a ninth of what is tied up. A day that buys a
third of the balance is not that. Either sellers are dumping into our orders
because the skin is collapsing faster than the history knows, or a ceiling is
wrong and every seller has noticed. Both are cases for stopping first and
looking second.

Once it trips it stays tripped. Every order the bot placed is taken down, the
plan is disarmed and anything approved but not yet sent is dropped, and
nothing can be applied until someone resets it by hand. Orders placed by hand
on the site are the owner's, and are left alone.

The balance is not read from the account - the bot is told it on the settings
page. Without it the budget stands in, which is the money we meant to commit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .holdings import bought_within

TRIPPED_KEY = "guard_tripped"
WINDOW_DAYS = 1.0


@dataclass
class Reading:
    spent: float            # fills seen in the last day, at our order price
    fills: int
    reference: float        # the balance, or the budget when it is not known
    limit: float            # spent at or above this trips it; 0 = off

    @property
    def tripped(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit - 1e-9

    def reason(self) -> str:
        return (f"за сутки исполнилось {self.fills} ордер(ов) на "
                f"${self.spent:.2f} — это {self.spent / self.reference * 100:.0f}% "
                f"от ${self.reference:.2f}, порог "
                f"{self.limit / self.reference * 100:.0f}%")

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["tripped"] = self.tripped
        return out


def read(rows: Iterable[dict], limits, now: datetime | None = None) -> Reading:
    """Where the day stands. `rows` is `our_orders(live_only=False)`."""
    bought = bought_within(rows, WINDOW_DAYS, now)
    spent = 0.0
    for row in bought:
        try:
            spent += float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
    reference = _reference(limits, spent)
    share = limits.guard_share
    limit = reference * share if reference > 0 and share > 0 else 0.0
    return Reading(round(spent, 2), len(bought), reference, limit)


def _reference(limits, spent: float) -> float:
    """What the day's fills are measured against. A balance read off the
    account is already short of today's purchases, and measuring them against
    what they left would tighten the brake with every fill: the day began
    with what is there now plus what was spent."""
    if limits.balance > 0:
        return limits.balance + (spent if getattr(limits, "balance_live", False)
                                 else 0.0)
    return limits.total_capital


def read_trades(trades: Iterable[dict], limits,
                now: datetime | None = None) -> Reading:
    """Where the day stands, by what the account actually bought.

    An order missing from the account's list was counted as bought, because a
    fill and a removal look the same there. A list read halfway made 46
    standing orders "bought" at once and pulled the brake on a day with one
    real fill. The trades say what was bought, at what price, and when.
    """
    from datetime import timedelta, timezone

    from .pacing import parse_iso
    from .profit import BUY, FAILED

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    spent, fills = 0.0, 0
    for t in trades:
        if t.get("role") != BUY or t.get("state") in FAILED:
            continue
        at = parse_iso(t.get("created_at")) or parse_iso(t.get("done_at"))
        if at is None or at < cutoff:
            continue
        try:
            spent += float(t.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        fills += 1
    reference = _reference(limits, spent)
    share = limits.guard_share
    limit = reference * share if reference > 0 and share > 0 else 0.0
    return Reading(round(spent, 2), fills, reference, limit)


def trades_readable(db) -> bool:
    """Whether the account's trades are being read: a path was found and the
    last reading came back without an error."""
    if not db.get_setting("trades_path"):
        return False
    try:
        last = json.loads(db.get_setting("trades_sync_result") or "null")
    except ValueError:
        return False
    return bool(last) and not last.get("error")


def current(db, limits) -> Reading:
    """The day's reading from the trades when they can be read, and from the
    orders gone from the account's list only when they cannot."""
    if trades_readable(db):
        return read_trades(db.all_trades(), limits)
    return read(db.our_orders(live_only=False), limits)


def tripped(db) -> dict | None:
    """What tripped it and when, or None while it stands open."""
    raw = db.get_setting(TRIPPED_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Unreadable is still tripped: the safe reading of a brake whose
        # state cannot be read is "on".
        return {"at": None, "reason": "состояние защиты не читается"}


def trip(db, reading: Reading, at: str) -> dict:
    state = {"at": at, "reason": reading.reason(), **reading.as_dict()}
    db.set_setting(TRIPPED_KEY, json.dumps(state, ensure_ascii=False))
    # Disarmed, and whatever was approved but not yet sent goes with it: it
    # was approved before this happened.
    db.set_setting("analysis_armed", "0")
    db.set_setting("analysis_pending_actions", "")
    return state


def reset(db) -> None:
    db.set_setting(TRIPPED_KEY, "")
