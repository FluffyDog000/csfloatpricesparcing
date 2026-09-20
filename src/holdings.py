"""Reconciling what we think we hold against what the account actually holds.

`our_orders` is the bot's own bookkeeping: what it placed, at what price, and
what it meant by it. That is not the same thing as the truth. An order can
leave CSFloat without the bot doing anything - cancelled by hand from the
site, or filled - and nothing in our table would change. The dashboard then
reports four standing orders that do not exist, the defence tends positions
that are gone, and the capital limit reserves money that is free.

So the site is the authority and this works out the difference. Matching is by
CSFloat's own id where we have one, and by the float band otherwise, because
an order placed by hand has no id of ours to match against.

What cannot be told apart is deliberate: an order that is no longer listed was
either cancelled or filled, and the list does not say which. Calling it "gone"
and saying so is better than guessing at the one that sounds better.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

GONE = "gone"          # ours, not on the site any more
FILLED = "filled"      # the site says it bought something
REPRICED = "repriced"  # standing at a price we did not write down
ADOPTED = "adopted"    # on the site, never ours
MATCHED = "matched"    # agrees


@dataclass
class Change:
    kind: str
    detail: str
    ours: dict[str, Any] | None = None
    theirs: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        if self.theirs and self.theirs.get("market_hash_name"):
            return str(self.theirs["market_hash_name"])
        return str((self.ours or {}).get("market_hash_name") or "")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "name": self.name,
                "ours": self.ours, "theirs": self.theirs}


def _band(row: Any) -> tuple[float | None, float | None]:
    def num(value):
        try:
            return None if value is None else round(float(value), 4)
        except (TypeError, ValueError):
            return None
    return num(row.get("float_min")), num(row.get("float_max"))


def reconcile_holdings(ours: Sequence[dict], theirs: Sequence[dict],
                       price_tolerance: float = 0.005) -> list[Change]:
    """One pass over both sides. Order of the result is ours, then theirs."""
    by_remote: dict[str, dict] = {}
    by_band: dict[tuple, dict] = {}
    for row in theirs:
        rid = str(row.get("remote_id") or "")
        if rid:
            by_remote[rid] = row
        key = (str(row.get("market_hash_name") or ""),) + _band(row)
        by_band.setdefault(key, row)

    used: set[int] = set()
    changes: list[Change] = []

    for row in ours:
        rid = str(row.get("remote_id") or "")
        match = by_remote.get(rid) if rid else None
        if match is None:
            # No id of ours to match on - an order we placed before ids were
            # recorded, or one whose reply never came back.
            key = (str(row.get("market_hash_name") or ""),) + _band(row)
            match = by_band.get(key)
        if match is None:
            changes.append(Change(
                GONE, "нет на сайте — снят вручную или исполнен", ours=row))
            continue
        used.add(id(match))

        if int(match.get("bought") or 0) > 0:
            changes.append(Change(
                FILLED, f"куплено {int(match['bought'])} шт.",
                ours=row, theirs=match))
            continue

        site_price = match.get("price")
        mine = row.get("price")
        if (site_price is not None and mine is not None
                and abs(float(site_price) - float(mine)) > price_tolerance):
            changes.append(Change(
                REPRICED,
                f"на сайте ${float(site_price):.2f}, у нас ${float(mine):.2f}",
                ours=row, theirs=match))
            continue

        changes.append(Change(MATCHED, "совпадает", ours=row, theirs=match))

    for row in theirs:
        if id(row) in used:
            continue
        changes.append(Change(
            ADOPTED, "стоит на сайте, у нас не числится", theirs=row))
    return changes


def strip_own(book: Sequence[dict], ours: Sequence[dict]) -> list[dict]:
    """The order book with our own orders taken out of it.

    The book read off a listing is the public one, and our orders are in it.
    Left there, every count of "who is ahead of us" includes us: an order
    alone in its band reads as having one rival at exactly its own price, the
    wait to fill doubles, and the defence answers an outbid that never
    happened.

    Orders carry no id in the book, so ours are found by price and bounds -
    one entry removed per order held, never more. A real rival standing at
    exactly our price and exactly our range would be dropped instead of ours,
    which undercounts by one; counting ourselves overcounts by one every time.
    """
    def key(row, price_field="price"):
        def num(v, default):
            try:
                return round(float(v), 4)
            except (TypeError, ValueError):
                return default
        return (round(float(row[price_field]), 2),
                num(row.get("float_min"), 0.0), num(row.get("float_max"), 1.0))

    wanted: dict[tuple, int] = {}
    for row in ours:
        try:
            wanted[key(row)] = wanted.get(key(row), 0) + 1
        except (KeyError, TypeError, ValueError):
            continue

    out = []
    for row in book:
        try:
            k = key(row)
        except (KeyError, TypeError, ValueError):
            out.append(row)
            continue
        if wanted.get(k):
            wanted[k] -= 1
            # One order of ours, one entry removed - a qty above one leaves
            # the rest of that entry standing, because the rest is not ours.
            if int(row.get("qty") or 1) > 1:
                row = dict(row, qty=int(row["qty"]) - 1)
                out.append(row)
            continue
        out.append(row)
    return out


def summary(changes: Iterable[Change]) -> dict[str, int]:
    out = {GONE: 0, FILLED: 0, REPRICED: 0, ADOPTED: 0, MATCHED: 0}
    for change in changes:
        out[change.kind] = out.get(change.kind, 0) + 1
    return out
