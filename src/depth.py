"""The sell side: how many lots are on sale in a float band, and how long
they have been sitting there.

Scoring a buy order needs both halves of the cycle - how long until it fills,
and how long until what it bought is sold on. The second was estimated as
1 / (sales per day in the band), which quietly assumes you are the only seller:
it put a $300 pair of gloves back on the market in seven hours. You are not
the only seller, you are behind everyone already listed cheaper, and the live
listings say by how much.

Unlike the order book this uses the documented endpoint:

    GET /api/v1/listings?market_hash_name=...&min_float=..&max_float=..

which authenticates with the API key rather than the browser session, and so
draws on a different budget than the cookie-and-residential-IP order sweep.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from urllib.parse import quote

from .orders import LISTINGS_PATH, _to_float, first, price_to_dollars

log = logging.getLogger("csfloat.depth")

# One request covers one band, so the step is the same as the book profile's:
# a finer grid buys detail nobody acts on at the price of the daily quota.
DEPTH_STEP = 0.02
DEPTH_PAGE = 50                 # the endpoint's documented maximum

CREATED_PATHS = ("created_at", "listed_at")
TYPE_PATHS = ("type",)
MIN_OFFER_PATHS = ("min_offer_price",)
FLOAT_PATHS = ("item.float_value", "float_value", "item.float")
ID_PATHS = ("id", "listing_id")


def depth_url(base_url: str, name: str, lo: float, hi: float,
              limit: int = DEPTH_PAGE) -> str:
    """Live buy_now lots inside one float band, cheapest first.

    min_float/max_float are documented query parameters, so the band is cut
    server-side: without them the same answer costs several pages of the whole
    item, and on a liquid item the band may not appear in them at all.
    """
    return (f"{base_url}{LISTINGS_PATH}"
            f"?market_hash_name={quote(name, safe='')}"
            f"&min_float={lo:g}&max_float={hi:g}"
            f"&type=buy_now&sort_by=lowest_price&limit={limit}")


def extract_depth(payload: Any) -> list[dict[str, Any]]:
    """[{id, float, price, created_at, type, min_offer_price}] from /listings."""
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        for key in ("data", "listings", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        listing_id = first(row, *ID_PATHS)
        if listing_id in (None, ""):
            continue
        out.append({
            "id": str(listing_id),
            "float": _to_float(first(row, *FLOAT_PATHS)),
            "price": price_to_dollars(first(row, "price")),
            "created_at": first(row, *CREATED_PATHS),
            "type": first(row, *TYPE_PATHS) or "buy_now",
            # Published on every listing: the lowest offer the seller will
            # entertain. A lot reachable below its ask needs no order at all.
            "min_offer_price": price_to_dollars(first(row, *MIN_OFFER_PATHS)),
        })
    return out


def _age_days(created_at: Any, now: dt.datetime) -> float | None:
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        when = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400)


def depth_profile(rows: list[dict[str, Any]],
                  span: tuple[float, float] | None,
                  step: float = DEPTH_STEP,
                  now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Per band: the queue you join when you list, and how slow it moves.

    `listings` is that queue - everyone already asking less than you, since the
    band is sorted by price. `oldest_days` is the direct measurement the sales
    history cannot give: a lot still unsold after a fortnight says what the
    turnover rate only implies.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not span:
        floats = [r["float"] for r in rows if r.get("float") is not None]
        if not floats:
            return []
        span = (min(floats), max(floats))
    lo, hi = span
    if not step > 0 or not hi > lo:
        return []

    profile: list[dict[str, Any]] = []
    a = lo
    while a < hi - 1e-9:
        b = round(min(a + step, hi), 4)
        a = round(a, 4)
        here = [r for r in rows
                if r.get("float") is not None and a <= r["float"] < b
                and r.get("price") is not None]
        ages = [d for d in (_age_days(r.get("created_at"), now) for r in here)
                if d is not None]
        offerable = [r for r in here
                     if r.get("min_offer_price") is not None
                     and r["min_offer_price"] < r["price"]]
        profile.append({
            "float_min": a,
            "float_max": b,
            "listings": len(here),
            "cheapest": min((r["price"] for r in here), default=None),
            "median_age_days": _median(ages),
            "oldest_days": max(ages, default=None),
            # How much of the band is reachable without an order at all.
            "offerable": len(offerable),
            "best_offer": min((r["min_offer_price"] for r in offerable),
                              default=None),
        })
        a = b
    return profile


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
