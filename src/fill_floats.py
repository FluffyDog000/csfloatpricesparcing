"""Where inside an order's float range the bot's purchases actually landed.

The ceiling is priced at the range's top - the worst float the order accepts -
on the argument that sellers keep good floats for the market and sell the
worst into orders. Whether that holds for this bot's orders is a question the
trades can answer: each purchase by one of our orders has a float, and the
order it filled has a range. Position 0% is the range's bottom, 100% its top.
"""
from __future__ import annotations

import statistics as st
from typing import Iterable, Sequence


def _ranges(events: Sequence[dict], orders: Sequence[dict], names: dict) -> dict:
    """Ranges and prices an item was ordered at: from the journal (every
    price an order held), and from our own orders (their last price)."""
    out: dict[str, list[tuple[float, float, float]]] = {}
    for e in events:
        if not e.get("ok") or e.get("dry") or e.get("kind") not in ("place", "raise", "lower"):
            continue
        if e.get("price") is None or e.get("float_min") is None:
            continue
        out.setdefault(e["market_hash_name"], []).append(
            (float(e["float_min"]), float(e["float_max"]), float(e["price"])))
    for o in orders:
        name = names.get(o.get("item_id"))
        if name and o.get("price") is not None:
            out.setdefault(name, []).append(
                (float(o["float_min"]), float(o["float_max"]), float(o["price"])))
    return out


def match(buys: Iterable[dict], events: Sequence[dict], orders: Sequence[dict],
          names: dict, step: float = 0.01) -> list[dict]:
    """Each purchase that one of our orders made, with that order's range and
    where the float sits in it. The narrowest matching range wins: two orders
    of ours at one price on nested ranges, and the narrower one is the order
    whose turn it was."""
    ranges = _ranges(events, orders, names)
    out = []
    for b in buys:
        f, price = b.get("float_value"), b.get("price")
        if f is None or price is None:
            continue
        hits = [(lo, hi) for lo, hi, p in ranges.get(b.get("market_hash_name"), ())
                if abs(p - float(price)) < 0.011 and lo <= float(f) <= hi]
        if not hits:
            continue
        lo, hi = min(set(hits), key=lambda r: r[1] - r[0])
        width = hi - lo
        pos = (float(f) - lo) / width if width > 0 else 1.0
        out.append({"item": b.get("market_hash_name"), "float": float(f),
                    "price": float(price), "lo": lo, "hi": hi,
                    "position": min(max(pos, 0.0), 1.0),
                    "top_hundredth": float(f) > hi - step + 1e-9,
                    "at": b.get("done_at") or b.get("created_at")})
    return out


def summary(rows: Sequence[dict]) -> dict:
    """The distribution of positions, overall and by how wide the range was."""
    def block(rs):
        if not rs:
            return {"count": 0}
        pos = [r["position"] for r in rs]
        deciles = [0] * 10
        for p in pos:
            deciles[min(int(p * 10), 9)] += 1
        return {"count": len(rs),
                "median": round(st.median(pos) * 100, 1),
                "mean": round(sum(pos) / len(pos) * 100, 1),
                "top_hundredth": round(sum(r["top_hundredth"] for r in rs) / len(rs) * 100, 1),
                "upper_half": round(sum(p >= 0.5 for p in pos) / len(pos) * 100, 1),
                "deciles": deciles}
    wide = [r for r in rows if r["hi"] - r["lo"] > 0.0201]
    narrow = [r for r in rows if r["hi"] - r["lo"] <= 0.0201]
    return {"all": block(rows), "wide": block(wide), "narrow": block(narrow)}


def report(db) -> tuple[list[dict], dict, int]:
    buys = [t for t in db.all_trades() if t.get("role") == "buy"]
    events = db.order_events(limit=200000, include_dry=False)
    orders = db.our_orders(live_only=False)
    names = {o["item_id"]: db.item_name(o["item_id"]) for o in orders}
    rows = match(buys, events, orders, names)
    return rows, summary(rows), len(buys)
