"""Buy orders: reading CSFloat's undocumented order book for an item.

Two requests are needed, because the endpoint is keyed by listing, not by item:

    GET /api/v1/listings?market_hash_name=...&limit=1   -> a listing id
    GET /api/v1/listings/{id}/buy-orders?limit=10       -> the orders

Only the top of the book is kept: the dashboard shows the best bids, and a
deeper book would cost more requests for information that changes by the minute.
Orders are stored as a snapshot per item, replaced on each fetch — this is a
"what is the market bidding now" view, not a history.
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("csfloat.orders")

LISTINGS_PATH = "/api/v1/listings"
ORDERS_PATH = "/api/v1/listings/{listing_id}/buy-orders"
DEFAULT_LIMIT = 10

# A listing's orders are the ones that match ITS float, so the book of an item
# is only visible by asking several listings spread across the float range.
# Bands are derived from the listings that actually exist rather than from a
# fixed grid: an empty band would cost a request and return nothing.
BAND_STEP = 0.01
MAX_BANDS = 25              # ceiling on requests for one sweep
LISTINGS_PAGE = 50          # listings to pull in the single lookup request

LISTING_FLOAT_PATHS = ("item.float_value", "float_value", "item.float")
LISTING_ID_PATHS = ("id", "listing_id")

# Candidate paths per field: the endpoint is undocumented, so read defensively
# rather than depend on one shape (same approach as the sales parser).
PRICE_PATHS = ("price", "market_price", "value", "amount")
QTY_PATHS = ("qty", "quantity", "count", "num", "amount_left")
FLOAT_MIN_PATHS = ("expression.float_value.min", "expression.min_float",
                   "min_float", "float_min", "float_value.min")
FLOAT_MAX_PATHS = ("expression.float_value.max", "expression.max_float",
                   "max_float", "float_max", "float_value.max")
SEED_PATHS = ("expression.paint_seed", "paint_seed", "seed")


def records(payload: Any) -> list[dict]:
    """The order list, whether it arrives bare or wrapped."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "orders", "buy_orders", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def first(record: dict, *paths: str) -> Any:
    """Value at the first dotted path that resolves."""
    for path in paths:
        node: Any = record
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None:
            return node
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def price_to_dollars(value: Any) -> float | None:
    """CSFloat quotes money in integer USD cents — the same convention the
    sales parser uses.

    An earlier magnitude guess ("under 1000 must already be dollars") turned a
    $3 order into $300 and sat it beside genuine $246 bids, which is exactly
    the kind of number a trader acts on. Only a fractional value is read as
    dollars, since cents are always whole."""
    number = _to_float(value)
    if number is None:
        return None
    if number != int(number):        # 123.45 is already dollars
        return round(number, 2)
    return round(number / 100.0, 2)


def parse_orders(payload: Any) -> list[dict]:
    """Normalize the response into rows ready for storage."""
    out = []
    for record in records(payload):
        price = price_to_dollars(first(record, *PRICE_PATHS))
        if price is None:
            continue
        out.append({
            "price": price,
            "qty": _to_int(first(record, *QTY_PATHS)) or 1,
            "float_min": _to_float(first(record, *FLOAT_MIN_PATHS)),
            "float_max": _to_float(first(record, *FLOAT_MAX_PATHS)),
            "paint_seed": _to_int(first(record, *SEED_PATHS)),
        })
    out.sort(key=lambda r: r["price"], reverse=True)
    return out


def extract_listings(payload: Any) -> list[dict]:
    """[{id, float}] from a /listings response, floats where known."""
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        for key in ("data", "listings", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        listing_id = first(row, *LISTING_ID_PATHS)
        if listing_id in (None, ""):
            continue
        out.append({"id": str(listing_id),
                    "float": _to_float(first(row, *LISTING_FLOAT_PATHS))})
    return out


def plan_bands(listings: list[dict], step: float = BAND_STEP,
               max_bands: int = MAX_BANDS) -> list[dict]:
    """Pick one listing per float band — the sweep's request plan.

    One listing per band is enough: every order whose range covers that band
    shows up on any listing inside it. Listings with an unknown float still get
    queried once, since they may be the only way into their part of the range.
    """
    by_band: dict[int, dict] = {}
    unknown: list[dict] = []
    for listing in listings:
        value = listing.get("float")
        if value is None:
            unknown.append(listing)
            continue
        # Round before flooring: 0.17 is stored as 0.16999…, so a plain
        # int(value / step) drops it into the band below and the listing is
        # lost to whichever neighbour shares that band.
        band = math.floor(round(value / step, 6))
        # Keep the lowest float in each band: low-float orders are the narrow,
        # high-value ones, so they are the ones worth not missing.
        if band not in by_band or value < by_band[band]["float"]:
            by_band[band] = listing

    plan = [dict(listing, band=round(band * step, 6))
            for band, listing in sorted(by_band.items())]
    if not plan and unknown:
        plan = [dict(unknown[0], band=None)]
    return plan[:max_bands]


def order_key(order: dict) -> tuple:
    """Identity of an order for de-duplication across bands.

    The same order surfaces on every listing its range covers, so a sweep sees
    it many times. Without an id from the API, price plus filters identifies it:
    two bids that agree on all of those are indistinguishable anyway."""
    return (order.get("id") or "", order["price"], order.get("float_min"),
            order.get("float_max"), order.get("paint_seed"))


def merge_orders(batches: list[list[dict]]) -> list[dict]:
    """Combine the per-band results into one book, best bid first."""
    seen: dict[tuple, dict] = {}
    for batch in batches:
        for order in batch:
            key = order_key(order)
            existing = seen.get(key)
            if existing is None:
                seen[key] = dict(order)
            else:
                # Same order seen from another band; keep the larger quantity
                # rather than adding, which would count it twice.
                existing["qty"] = max(existing.get("qty") or 1,
                                      order.get("qty") or 1)
    out = list(seen.values())
    out.sort(key=lambda o: o["price"], reverse=True)
    return out


def extract_listing_id(payload: Any) -> str | None:
    """First listing id from a /listings response."""
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else None
    if not rows:
        return None
    for row in rows:
        if isinstance(row, dict):
            listing_id = row.get("id") or row.get("listing_id")
            if listing_id not in (None, ""):
                return str(listing_id)
    return None
