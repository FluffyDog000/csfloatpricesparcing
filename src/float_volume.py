"""How much money goes through float-premium sales: not the skin's whole
turnover, only the sales bought for their float.

A sale counts as a float sale when both hold:

- its float sits in the item's best hundredths - the first `best` hundredths
  above the lowest float the item has ever sold at (a skin capped at 0.06
  starts there, not at the wear's 0.00);
- it went for at least `premium` over the item's ordinary price - the median
  of the same window's sales outside those hundredths.

Its overpay is price minus that ordinary price: what the float itself cost
the buyer. Items with too few ordinary sales have no ordinary price and are
left out of the float count, but still counted in the total.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone

STEP = 0.01
MIN_BASE = 5            # ordinary sales needed before an item has a price
BANDS = ((0, 5), (5, 20), (20, 50), (50, 200), (200, 1000), (1000, None))


def band_of(price: float) -> str:
    for lo, hi in BANDS:
        if hi is None or price < hi:
            return f"${lo}+" if hi is None else f"${lo}–{hi}"
    return "?"


def report(db, days: float = 30.0, best: int = 2, premium: float = 0.10,
           now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    floors = {r[0]: r[1] for r in db.conn.execute(
        "SELECT item_id, MIN(float_value) FROM sales "
        "WHERE float_value IS NOT NULL GROUP BY item_id")}
    by_item: dict[int, list[tuple[float, float]]] = defaultdict(list)
    names: dict[int, str] = {}
    total_n, total_usd = 0, 0.0
    for item_id, name, price, f in db.conn.execute(
            "SELECT s.item_id, i.market_hash_name, s.price, s.float_value "
            "FROM sales s JOIN items i ON i.id = s.item_id "
            "WHERE s.sold_at >= ? AND s.price > 0", (since,)):
        total_n += 1
        total_usd += price
        if f is not None:
            by_item[item_id].append((float(price), float(f)))
            names[item_id] = name

    items, bands = [], defaultdict(lambda: {"n": 0, "usd": 0.0, "over": 0.0})
    for item_id, rows in by_item.items():
        edge = floors[item_id] + best * STEP
        ordinary = [p for p, f in rows if f >= edge]
        if len(ordinary) < MIN_BASE:
            continue
        base = st.median(ordinary)
        hits = [p for p, f in rows if f < edge and p >= base * (1 + premium)]
        if not hits:
            continue
        usd = sum(hits)
        over = sum(p - base for p in hits)
        items.append({"name": names[item_id], "n": len(hits), "usd": round(usd, 2),
                      "over": round(over, 2), "base": round(base, 2),
                      "pct": round((usd / len(hits) / base - 1) * 100, 1),
                      "sales": len(rows)})
        b = bands[band_of(base)]
        b["n"] += len(hits)
        b["usd"] += usd
        b["over"] += over

    items.sort(key=lambda r: r["usd"], reverse=True)
    float_n = sum(r["n"] for r in items)
    float_usd = sum(r["usd"] for r in items)
    order = [band_of(lo) for lo, _ in BANDS]
    return {
        "days": days, "best": best, "premium": premium,
        "total": {"n": total_n, "usd": round(total_usd, 2)},
        "float": {"n": float_n, "usd": round(float_usd, 2),
                  "over": round(sum(r["over"] for r in items), 2),
                  "items": len(items),
                  "share": round(float_usd / total_usd * 100, 1) if total_usd else 0.0,
                  "per_day": round(float_usd / days, 2) if days else 0.0},
        "bands": [{"band": k, "n": bands[k]["n"], "usd": round(bands[k]["usd"], 2),
                   "over": round(bands[k]["over"], 2)} for k in order if k in bands],
        "items": items,
    }
