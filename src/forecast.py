"""What the bot expected to sell a purchase for, written down when it bought.

Whether the pricing is right is a question only the sales can answer: the exit
price an order was placed on, against what the item actually went for. The
listings and the logs behind a forecast are cleared after a few days, so the
forecast keeps its own copy of what it rested on - the median, the queue and
the lots in it with their floats - for as long as the purchase takes to sell.

Written for every order of ours: when it is placed or amended, and on each
defence pass (at most every few hours unless something moved), so the one in
force when an order fills is on file whichever way the fill is noticed.
"""
from __future__ import annotations

import json
from typing import Any

# Fields of a priced band worth keeping. Settings are added by the caller.
FIELDS = ("market", "market_plain", "market_then", "queue_price", "priced_from",
          "exit_net", "exit_expected", "ceiling", "bid", "margin",
          "margin_expected", "lam", "sell_rate", "t_sell", "sample", "window",
          "shift", "queue", "lots_cleared", "trend")

# A new row when the price moved, the exit moved by more than this, or the
# last one is this old; otherwise the pass only confirms what is on file.
EXIT_MOVE = 0.005
REFRESH_HOURS = 6.0


def _num(v):
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return round(v, 4)
    return v


def snapshot(band, params=None) -> dict[str, Any]:
    """The forecast a priced band amounts to, as plain JSON-able values."""
    out = {k: _num(getattr(band, k, None)) for k in FIELDS}
    out["queue_lots"] = [[_num(p), _num(f)] for p, f in
                         (getattr(band, "queue_lots", None) or [])][:60]
    if params is not None:
        out["settings"] = settings_of(params)
    return out


def settings_of(params) -> dict[str, Any]:
    """The pricing settings a forecast was made under."""
    return {k: _num(getattr(params, k, None)) for k in
            ("fee", "min_margin", "window_days", "min_sample",
             "adaptive", "careful", "queue_days")}


def exit_gross(data: dict | None) -> float | None:
    """The sale price the forecast expected, before CSFloat's cut: the lower
    of the median and the queue, as the ceiling was priced from."""
    if not data:
        return None
    net = data.get("exit_net")
    fee = ((data.get("settings") or {}).get("fee"))
    if net is not None and fee is not None and fee < 1:
        return round(float(net) / (1 - float(fee)), 2)
    vals = [v for v in (data.get("market"), data.get("queue_price")) if v is not None]
    return round(min(vals), 2) if vals else None


def loads(raw) -> dict | None:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
