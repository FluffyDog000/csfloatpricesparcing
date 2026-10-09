"""How much money goes through float-premium sales: not the skin's whole
turnover, only the sales bought for their float.

The float zone is each item's own, read off its sales rather than fixed:

- the ordinary price is the median of the sales in the worse half of the
  item's float range (from its lowest sold float to the top of the wear),
  where float no longer moves the price;
- walking up from the lowest float a hundredth at a time, a hundredth with at
  least `MIN_BUCKET` sales belongs to the zone while its median is at least
  `premium` over the ordinary price; the first such hundredth that is not
  ends the zone. A hundredth too thin to judge neither ends nor extends it;
- with `width` set, the zone is that many hundredths instead, whatever the
  prices do.

Every sale inside the zone is a float sale; its overpay is price minus the
ordinary price. Items with too few ordinary sales are left out of the float
count but still counted in the total.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .orders import wear_range

STEP = 0.01
MIN_BASE = 5            # ordinary sales needed before an item has a price
MIN_BUCKET = 3          # sales in a hundredth before its median is believed
MAX_ZONE = 0.15         # no zone runs further than this from the lowest float
BANDS = ((0, 5), (5, 20), (20, 50), (50, 200), (200, 1000), (1000, None))


def band_of(price: float) -> str:
    for lo, hi in BANDS:
        if hi is None or price < hi:
            return f"${lo}+" if hi is None else f"${lo}–{hi}"
    return "?"


def zone_edge(rows: list[tuple[float, float]], floor: float, base: float,
              premium: float, width: int = 0) -> float:
    """The float the item's zone ends at (exclusive)."""
    if width:
        return floor + width * STEP
    buckets: dict[int, list[float]] = defaultdict(list)
    for p, f in rows:
        k = int((f - floor) / STEP + 1e-9)
        if 0 <= k < MAX_ZONE / STEP:
            buckets[k].append(p)
    edge = floor
    for k in range(int(MAX_ZONE / STEP)):
        prices = buckets.get(k, [])
        if len(prices) < MIN_BUCKET:
            continue
        if st.median(prices) >= base * (1 + premium):
            edge = floor + (k + 1) * STEP
        else:
            break
    return edge


def report(db, days: float = 30.0, premium: float = 0.05, width: int = 0,
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
        floor = floors[item_id]
        span = wear_range(names[item_id])
        top = span[1] if span else max(f for _, f in rows)
        middle = floor + (top - floor) / 2
        ordinary = [p for p, f in rows if f >= middle]
        if len(ordinary) < MIN_BASE:
            continue
        base = st.median(ordinary)
        edge = zone_edge(rows, floor, base, premium, width)
        hits = [p for p, f in rows if f < edge]
        if not hits:
            continue
        usd = sum(hits)
        over = sum(p - base for p in hits)
        items.append({"name": names[item_id], "n": len(hits), "usd": round(usd, 2),
                      "over": round(over, 2), "base": round(base, 2),
                      "pct": round((usd / len(hits) / base - 1) * 100, 1),
                      "floor": round(floor, 4), "edge": round(edge, 4),
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
        "days": days, "premium": premium, "width": width,
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
