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
from typing import Any

log = logging.getLogger("csfloat.orders")

LISTINGS_PATH = "/api/v1/listings"
ORDERS_PATH = "/api/v1/listings/{listing_id}/buy-orders"
DEFAULT_LIMIT = 10

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
    """CSFloat quotes money in cents. A bare 303 could be either, so treat a
    value that looks like cents as cents and leave small numbers alone."""
    number = _to_float(value)
    if number is None:
        return None
    return round(number / 100.0, 2) if number >= 1000 else round(number, 2)


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
