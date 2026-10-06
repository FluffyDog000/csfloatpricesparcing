"""The earnings report: closed deals, what is held, what it is worth.

Worked out from the account's trades. Shared by the earnings page and the
Telegram digest, so the two can never report different profits.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import profit as pf
from .pacing import parse_iso

SINCE_KEY = "profit_since"
DAYS_KEY = "profit_estimate_days"
EXCLUDED_KEY = "profit_excluded"
DAYS_BOUNDS = (1, 180)
# How often the listings of what is held are read again, hours; 0 = never.
HOLD_SWEEP_KEY = "profit_hold_sweep_hours"
HOLD_SWEEP_BOUNDS = (0, 48)
HOLD_SWEEP_DEFAULT = 4


def settings(db) -> dict:
    """What the earnings tab counts: from which date, which trades the owner
    took out by hand, and how many days of sales value what is still held."""
    since = (db.get_setting(SINCE_KEY) or "").strip()
    try:
        days = int(float(db.get_setting(DAYS_KEY) or 30))
    except (TypeError, ValueError):
        days = 30
    days = min(max(days, DAYS_BOUNDS[0]), DAYS_BOUNDS[1])
    try:
        excluded = {str(x) for x in json.loads(
            db.get_setting(EXCLUDED_KEY) or "[]")}
    except ValueError:
        excluded = set()
    try:
        hours = float(db.get_setting(HOLD_SWEEP_KEY) or HOLD_SWEEP_DEFAULT)
    except (TypeError, ValueError):
        hours = HOLD_SWEEP_DEFAULT
    hours = min(max(hours, HOLD_SWEEP_BOUNDS[0]), HOLD_SWEEP_BOUNDS[1])
    return {"since": since, "estimate_days": days, "excluded": excluded,
            "hold_sweep_hours": hours}


def _iso(when: str | None) -> str | None:
    """A trade's time in the form the forecasts are stamped with."""
    at = parse_iso(when) if when else None
    return at.replace(microsecond=0).isoformat() if at else None


def build(db, fee: float, days: float = 0.0) -> dict:
    """Everything the earnings page shows, for sales within `days` (0 = all).

    Each sale is paired with the purchase of the same skin - same name, float
    and pattern - and the profit is what the sale brought after CSFloat's cut
    less what the purchase cost. What was bought and not sold yet is valued at
    the median of recent sales in its own hundredth of float."""
    since = pf.since_iso(days) if days > 0 else ""
    conf = settings(db)
    start = conf["since"]

    def counted(when: str | None) -> bool:
        """On or after the date the owner counts from. Compared by date:
        trades arrive as "...Z" and "+00:00" alike, and a day is the unit."""
        return not start or (when or "")[:10] >= start

    everything = db.all_trades()
    # Taken out before pairing: a purchase left in would still claim the
    # sale of the same skin, and the sale would vanish with it.
    book = pf.pair([t for t in everything
                    if t["trade_id"] not in conf["excluded"]], fee)
    events = [e for e in db.order_events(limit=50000, include_dry=False)
              if e["ok"] and e["kind"] in ("place", "raise", "lower")]

    # Bought before the date and sold after goes too: the purchase is part of
    # what the date leaves out, and half a deal is no profit to report.
    counted_closed = [d for d in book.closed if counted(d["bought_at"])]
    closed = [d for d in counted_closed if (d["sold_at"] or "") >= since]
    for d in closed:
        d["by_bot"] = pf.by_bot({"market_hash_name": d["market_hash_name"],
                                 "float_value": d["float_value"],
                                 "price": d["bought"]}, events)

    holding = []
    # A purchase still in its trade is money already spent on a skin that is
    # on its way: it belongs with what is held, marked, rather than at the
    # foot of the page. Every fill of a new bot spends its first week there.
    coming = [t for t in book.pending if t.get("role") == pf.BUY]
    coming_ids = {t["trade_id"] for t in coming}
    for t in list(book.holding) + coming:
        if not counted(t.get("done_at") or t.get("created_at")):
            continue
        bought_at = t.get("done_at") or t.get("created_at")
        when = parse_iso(bought_at)
        holding.append({
            "market_hash_name": t["market_hash_name"] or "",
            "float_value": t["float_value"],
            "paint_seed": t["paint_seed"], "bought": float(t["price"] or 0),
            "bought_at": bought_at,
            "days": (round((datetime.now(timezone.utc) - when).total_seconds()
                           / 86400.0, 1) if when else None),
            "by_bot": pf.by_bot(t, events),
            "trade_id": t["trade_id"],
            "pending": t["trade_id"] in coming_ids,
            "state": t.get("state"),
        })
    # Valued by the queue each will join, not the median alone (holding_value).
    from .holding_value import value_all
    from .settings import params as read_params
    value_all(db, holding, fee, float(conf["estimate_days"]),
              float(read_params(db).queue_days))
    for h in holding:
        if h.get("median") is not None:
            h["basis"] = f"{h['basis']} за {conf['estimate_days']} дн"
        fc = db.forecast_for(h["market_hash_name"], h["float_value"], h["bought"],
                             _iso(h["bought_at"])) if h.get("by_bot") else None
        h["forecast_exit"] = fc["exit"] if fc else None

    # Newest first, whichever list a purchase came from: running trades were
    # appended after the finished ones and read upside down.
    holding.sort(key=lambda h: h["bought_at"] or "", reverse=True)
    valued = [h for h in holding if h["estimate"] is not None]
    valued_spent = sum(h["bought"] for h in valued)
    valued_profit = sum(h["est_profit"] for h in valued)
    return {
        "fee": fee,
        "days": days,
        "totals": pf.totals(closed),
        "all_time": pf.totals(counted_closed),
        "closed": closed[:1000],
        "holding": holding,
        "holding_totals": {
            "count": len(holding),
            "spent": round(sum(h["bought"] for h in holding), 2),
            "estimate": round(sum(h["estimate"] for h in valued), 2),
            "est_profit": round(valued_profit, 2),
            "est_pct": (round(valued_profit / valued_spent * 100.0, 1)
                        if valued_spent else None),
            "unvalued": len(holding) - len(valued),
            "pending": sum(1 for h in holding if h["pending"]),
        },
        "unmatched": [dict(t) for t in book.unmatched
                      if ((t.get("done_at") or t.get("created_at") or "") >= since)
                      and counted(t.get("done_at") or t.get("created_at"))],
        "pending": [dict(t) for t in book.pending
                    if t["trade_id"] not in coming_ids],
        "trades": len(everything),
        "settings": {"since": start, "estimate_days": conf["estimate_days"],
                     "hold_sweep_hours": conf["hold_sweep_hours"]},
        "excluded": [{"trade_id": t["trade_id"], "role": t.get("role"),
                      "market_hash_name": t.get("market_hash_name"),
                      "float_value": t.get("float_value"),
                      "price": t.get("price"),
                      "at": t.get("done_at") or t.get("created_at")}
                     for t in everything if t["trade_id"] in conf["excluded"]],
    }
