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


# A purchase this recent still has its order in the journal (kept 30 days):
# "not the bot's" can be written down for good. Older and unmarked, the
# journal may simply have been cleared - left unmarked rather than called wrong.
SURE_DAYS = 25


def _stored(raw) -> dict | None:
    from .forecast import loads
    return loads(raw) if isinstance(raw, str) else (raw or None)


def remember(db, trades: list[dict], events) -> None:
    """Copy onto each purchase, for good, what the journal and the forecasts
    only keep for a while: whether an order of ours made it, and the forecast
    it was bought on. Earnings a year back still know which deals were the
    bot's, and how they compared with what it expected."""
    from datetime import timedelta
    recent = (datetime.now(timezone.utc) - timedelta(days=SURE_DAYS)).isoformat()
    for t in trades:
        if t.get("role") != pf.BUY:
            continue
        when = _iso(t.get("done_at") or t.get("created_at")) or ""
        mine = t.get("by_bot")
        if mine is None:
            found = pf.by_bot(t, events)
            if found or when >= recent:
                db.mark_trade(t["trade_id"], by_bot=found)
                t["by_bot"] = 1 if found else 0
                mine = t["by_bot"]
        if t.get("forecast") is None and (mine or mine is None):
            fc = db.forecast_for(t.get("market_hash_name"), t.get("float_value"),
                                 t.get("price"), when or None)
            if fc:
                keep = {"exit": fc.get("exit"), "at": fc.get("at"),
                        "float_min": fc.get("float_min"),
                        "float_max": fc.get("float_max"), "data": fc.get("data")}
                db.mark_trade(t["trade_id"], forecast=keep)
                t["forecast"] = keep
                if mine is None:
                    db.mark_trade(t["trade_id"], by_bot=True)
                    t["by_bot"] = 1


def is_bot(trade: dict, events) -> bool:
    """Whether an order of ours made this purchase: as written on the trade,
    or worked out from the journal while it still holds the order."""
    if trade.get("by_bot") is not None:
        return bool(trade["by_bot"])
    return pf.by_bot(trade, events)


def attach_forecast(db, deal: dict, stored=None) -> None:
    """What the bot expected a closed deal to sell for, and how far off it
    was: the forecast in force when the order bought it (see forecast.py) -
    as copied onto the purchase, or looked up while it is still on file."""
    from .forecast import exit_gross
    deal["forecast_exit"] = deal["forecast_error"] = None
    fc = _stored(stored) or db.forecast_for(
        deal["market_hash_name"], deal["float_value"], deal["bought"],
        _iso(deal["bought_at"]))
    if not fc:
        return
    data = fc.get("data") or {}
    exit_ = fc.get("exit") or exit_gross(data)
    if not exit_:
        return
    deal["forecast_exit"] = round(float(exit_), 2)
    deal["forecast_error"] = round((deal["sold"] - exit_) / exit_ * 100.0, 1)
    deal["forecast_sample"] = data.get("sample")
    deal["forecast_from"] = data.get("priced_from")
    deal["forecast_width"] = round(float(fc["float_max"]) - float(fc["float_min"]), 4)
    deal["forecast_queue"] = data.get("queue")


def accuracy(closed: list[dict]) -> dict:
    """How the forecasts fared, overall and by what they rested on. Error is
    (sold - expected) / expected: negative means sold for less than expected."""
    import statistics as st

    def block(rows):
        errs = [r["forecast_error"] for r in rows]
        if not errs:
            return {"count": 0}
        return {"count": len(errs),
                "median": round(st.median(errs), 1),
                "mean": round(sum(errs) / len(errs), 1),
                "mean_abs": round(sum(abs(e) for e in errs) / len(errs), 1),
                "below": round(sum(e < 0 for e in errs) / len(errs) * 100, 0)}

    rows = [d for d in closed if d.get("forecast_error") is not None]
    groups = [
        ("все", rows),
        ("до 15 продаж у верха", [r for r in rows if (r.get("forecast_sample") or 0) < 15]),
        ("15–39 продаж", [r for r in rows if 15 <= (r.get("forecast_sample") or 0) < 40]),
        ("40+ продаж", [r for r in rows if (r.get("forecast_sample") or 0) >= 40]),
        ("цена от истории", [r for r in rows if r.get("forecast_from") == "история"]),
        ("цена от очереди", [r for r in rows if r.get("forecast_from") == "очередь"]),
        ("узкие полосы (до 0.02)", [r for r in rows if (r.get("forecast_width") or 1) <= 0.0201]),
        ("широкие полосы", [r for r in rows if (r.get("forecast_width") or 0) > 0.0201]),
    ]
    return {"groups": [{"group": g, **block(rs)} for g, rs in groups],
            "with_forecast": len(rows),
            "closed": len(closed)}


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
    remember(db, everything, events)
    trades = {t["trade_id"]: t for t in everything}

    # Bought before the date and sold after goes too: the purchase is part of
    # what the date leaves out, and half a deal is no profit to report.
    counted_closed = [d for d in book.closed if counted(d["bought_at"])]
    closed = [d for d in counted_closed if (d["sold_at"] or "") >= since]
    for d in closed:
        buy = trades.get(d["buy_id"]) or {}
        d["by_bot"] = is_bot(buy, events) if buy else pf.by_bot(
            {"market_hash_name": d["market_hash_name"],
             "float_value": d["float_value"], "price": d["bought"]}, events)
        attach_forecast(db, d, buy.get("forecast"))

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
            "by_bot": is_bot(t, events),
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
        stored = _stored(trades.get(h["trade_id"], {}).get("forecast"))
        h["forecast_exit"] = stored.get("exit") if stored else None

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
        "accuracy": accuracy(closed),
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
