"""What goes over the wire, by kind of request, hour by hour.

The load page counted only the sales polls - their rows in poll_log carry the
response size. The book sweeps, the defence re-reading every held item's book
and the account's own requests never wrote a row, so a proxy billed by the
gigabyte was paying for traffic the page did not show. Every response is
counted here instead, in memory, and the collector writes the counts out.

Kinds:
  history   sales history polls (anonymous, through the proxy pool)
  book      buy orders of a lot - what the sweeps and the defence read
  listings  lots for sale, by band
  ring      anything else on the analysis keys
  account   the main key: placing, amending, cancelling, balance, trades
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

KINDS = ("history", "book", "listings", "ring", "account")
LABELS = {"history": "история продаж", "book": "ордера в стаканах",
          "listings": "листинги", "ring": "прочее (ключи анализа)",
          "account": "свой аккаунт (главный ключ)"}


def hour_of(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0).isoformat()


class Traffic:
    """Requests and bytes per (hour, kind), waiting to be written."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], list[int]] = {}

    def add(self, kind: str, nbytes: int | None, now: datetime | None = None) -> None:
        key = (hour_of(now), kind if kind in KINDS else "ring")
        with self._lock:
            row = self._pending.setdefault(key, [0, 0])
            row[0] += 1
            row[1] += int(nbytes or 0)

    def drain(self) -> list[tuple[str, str, int, int]]:
        with self._lock:
            out = [(h, k, r, b) for (h, k), (r, b) in self._pending.items()]
            self._pending = {}
        return out
