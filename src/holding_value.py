"""What a skin we hold will sell for, judged by the queue it will join.

The earnings tab valued every held skin at the median of recent sales in its
hundredth of float. That is what it would fetch if nobody else were selling.
Others are: the lots already listed cheaper than that median, with a float as
good as ours or better, are ahead of us, and the buyer reaches them first.

So, the same way the order pricing reads the queue (`ladder.queue_price`):

  ahead      lots under the median with a float no worse than ours, from the
             newest listings read for the item;
  cleared    how many of them sell before ours can be listed: the hundredth's
             sales a day times the days of lock left (no more than the
             configured queue days - others list too in that time);
  own        our own skins in the same hundredth bought earlier: they unlock
             first and take the front of the same queue;
  price      a step under the first lot left standing, or the median when the
             queue clears - and never more than the median;
  t_sell     days on sale after the unlock: our own ahead plus us, at the
             hundredth's pace.

The median itself is brought to today's level first when the item has enough
sales for a price line (see `recency`): on a falling skin a month-old median
values the stock above what it will fetch.
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .pacing import parse_iso

LOCK_DAYS = 7.0


def _step(price: float) -> float:
    from .ladder import price_step
    return price_step(price)


def _hundredth(f: float) -> float:
    return int(f * 100 + 1e-9) / 100.0


def _today(sales: list[dict], window: float) -> tuple[list[dict], bool]:
    from .recency import to_today
    rows, info = to_today(sales, window)
    return rows, bool(info.applied)


def _lots(depth: Sequence[dict]) -> tuple[list[tuple[float, float | None]], str | None]:
    """Every lot of the newest listing reading, and when it was read."""
    lots, newest = [], None
    for band in depth or ():
        at = band.get("fetched_at")
        if at and (newest is None or str(at) > newest):
            newest = str(at)
        for pair in band.get("asks") or ():
            try:
                price = float(pair[0])
                f = float(pair[1]) if len(pair) > 1 and pair[1] is not None else None
            except (TypeError, ValueError, IndexError):
                continue
            lots.append((price, f))
    return lots, newest


def value_one(row: dict, sales: list[dict], lots, book_at: str | None,
              own_ahead: int, fee: float, window: float, queue_days: float,
              now: datetime | None = None) -> dict:
    """The queue-aware valuation of one held skin. `sales` carry age_days."""
    from . import profit as pf

    now = now or datetime.now(timezone.utc)
    f = row.get("float_value")
    out: dict = {"own_ahead": own_ahead, "book_at": book_at}
    adjusted, applied = _today(sales, window)
    median, basis = pf.estimate(adjusted, f)
    if median is not None and applied:
        basis += ", к сегодняшним ценам"
    out.update(median=round(median, 2) if median is not None else None,
               basis=basis)

    bought_at = parse_iso(row.get("bought_at"))
    unlock = (bought_at + timedelta(days=LOCK_DAYS)) if bought_at else now
    left = max((unlock - now).total_seconds() / 86400.0, 0.0)
    out["unlock_days"] = round(left, 1)

    rate = None
    if f is not None:
        lo = _hundredth(float(f))
        near = [s for s in sales if s.get("float_value") is not None
                and lo <= float(s["float_value"]) < lo + 0.01]
        if near and window > 0:
            rate = len(near) / window
    out["sell_rate"] = round(rate, 3) if rate is not None else None

    if median is None:
        out.update(estimate=None, queue_price=None, ahead=None, cleared=None,
                   t_sell=None, advice=None, est_profit=None, est_pct=None,
                   queue_note="мало продаж для оценки")
        return out

    if not lots:
        out.update(estimate=out["median"], queue_price=None, ahead=None,
                   cleared=None, advice=out["median"],
                   queue_note="листинги не читались — по медиане")
    else:
        ahead = sorted(p for p, lf in lots
                       if p <= median and (lf is None or f is None or lf <= float(f) + 1e-9))
        cleared = (rate or 0.0) * min(left, queue_days)
        survivors = ahead[int(cleared):]
        queue_price = (survivors[0] - _step(survivors[0])) if survivors else None
        exit_ = min(median, queue_price) if queue_price is not None else median
        out.update(estimate=round(exit_, 2),
                   queue_price=round(queue_price, 2) if queue_price is not None else None,
                   ahead=len(ahead), cleared=round(cleared, 1), advice=round(exit_, 2),
                   queue_note=(f"впереди {len(ahead)} дешевле медианы, до разблокировки "
                               f"уйдёт ~{cleared:.0f}"
                               + (f", свои впереди: {own_ahead}" if own_ahead else "")
                               + ("" if survivors else " — очередь рассосётся")))
    out["t_sell"] = (round((own_ahead + 1) / rate, 1) if rate else None)
    bought = float(row.get("bought") or 0)
    est = out["estimate"]
    out["est_profit"] = round(est * (1 - fee) - bought, 2)
    out["est_pct"] = round((est * (1 - fee) - bought) / bought * 100, 1) if bought else None
    return out


def value_all(db, rows: list[dict], fee: float, window: float,
              queue_days: float, now: datetime | None = None) -> None:
    """Fill in the queue-aware valuation of every held row, in place."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=window)).isoformat()
    cache: dict[str, tuple] = {}
    for row in rows:
        name = row.get("market_hash_name") or ""
        if name not in cache:
            item_id = db.get_item_id(name) if name else None
            sales, lots, at = [], [], None
            if item_id is not None:
                for s in db.query_sales(int(item_id), since_iso=since):
                    when = parse_iso(s.get("sold_at"))
                    if when is None:
                        continue
                    sales.append({"price": s.get("price"),
                                  "float_value": s.get("float_value"),
                                  "age_days": (now - when).total_seconds() / 86400.0})
                try:
                    lots, at = _lots(db.listing_depth(int(item_id)))
                except Exception:  # noqa: BLE001 - an older DB has no listings
                    lots, at = [], None
            cache[name] = (sales, lots, at)
        sales, lots, at = cache[name]
        f = row.get("float_value")
        own = 0
        if f is not None:
            lo = _hundredth(float(f))
            own = sum(1 for o in rows
                      if o is not row and o.get("market_hash_name") == name
                      and o.get("float_value") is not None
                      and lo <= float(o["float_value"]) < lo + 0.01
                      and (o.get("bought_at") or "") < (row.get("bought_at") or ""))
        row.update(value_one(row, sales, lots, at, own, fee, window, queue_days, now))
        row["tracked"] = bool(sales)
