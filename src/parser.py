"""Parse the raw JSON returned by CSFloat's "Latest Sales" endpoint into
normalized Sale records.

IMPORTANT — field mapping is defensive on purpose.
The "Latest Sales" endpoint is undocumented. This parser searches a list of
*candidate* key paths for each field, so it tolerates small differences in the
real response shape. The collector also dumps the first raw response per item
to CSFLOAT_RAW_DUMP_DIR — send that dump over and the exact paths can be
pinned. Based on CSFloat's observed structure the expected shape is roughly:

    [
      {
        "id": "<sale id>",                 # may be absent
        "price": 12345,                     # USD cents (integer)
        "sold_at": "2024-01-02T03:04:05Z", # absolute timestamp
        "item": {
          "float_value": 0.1543,
          "paint_seed": 13,
          "paint_index": 10024,
          "market_hash_name": "...",
          "stickers": [ {...}, ... ]
        }
      },
      ...
    ]

If your real response differs, edit the candidate lists below (or send the
dump and they will be adjusted for you).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Sale


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _dig(obj: Any, path: str) -> Any:
    """Follow a dotted path like "item.float_value" through nested dicts.
    Returns None if any segment is missing."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _first(obj: Any, paths: list[str]) -> Any:
    """Return the first non-None value found among candidate dotted paths."""
    for p in paths:
        val = _dig(obj, p)
        if val is not None:
            return val
    return None


ICON_PATHS = ["item.icon_url", "icon_url"]


def extract_icon_hash(payload: Any) -> str | None:
    """Pull the Steam economy icon hash from the sales response (it lives on
    each sale's item). Lets us show item images WITHOUT the rate-limited
    official listings API — the sales endpoint already carries it."""
    for rec in extract_records(payload):
        if isinstance(rec, dict):
            icon = _first(rec, ICON_PATHS)
            if icon:
                return str(icon)
    return None


# Candidate key paths for each field, most-likely first.
PRICE_PATHS = ["price", "sale_price", "sold_price", "item.price"]
FLOAT_PATHS = [
    "item.float_value",
    "float_value",
    "item.floatvalue",
    "wear",
    "item.wear",
]
SEED_PATHS = ["item.paint_seed", "paint_seed", "item.paintseed", "seed"]
PAINT_INDEX_PATHS = ["item.paint_index", "paint_index", "item.paintindex"]
NAME_PATHS = ["item.market_hash_name", "market_hash_name", "name"]
STICKER_PATHS = ["item.stickers", "stickers"]
ID_PATHS = ["id", "sale_id", "_id", "listing_id"]
# Absolute sale time (ISO string or unix epoch).
SOLD_AT_PATHS = ["sold_at", "sold_at_timestamp", "time", "timestamp", "created_at"]
# Relative sale time like "54 minutes ago" if that is all the API gives.
SOLD_REL_PATHS = ["sold_ago", "time_ago", "relative_time", "age"]


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _price_to_cents(val: Any) -> int | None:
    """CSFloat prices are integer USD cents. If we ever get a float that looks
    like dollars (e.g. 123.45), convert it. Heuristic but safe for both."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # A float with a fractional part is almost certainly dollars.
        if val != int(val):
            return int(round(val * 100))
        return int(val)
    # String
    s = str(val).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        if "." in s:
            return int(round(float(s) * 100))
        return int(s)
    except ValueError:
        return None


_REL_RE = re.compile(
    r"(?P<num>\d+)\s*(?P<unit>second|minute|hour|day|week|month|year)s?\s*ago",
    re.IGNORECASE,
)
_REL_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,   # ~30d, approximate
    "year": 31536000,   # ~365d, approximate
}


def parse_relative_time(text: str, now: datetime | None = None) -> datetime | None:
    """Parse "54 minutes ago" -> absolute UTC datetime, relative to `now`.
    Also handles "just now"/"a minute ago" loosely."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.strip().lower()
    if "just now" in t or t in ("now", "moments ago"):
        return now
    # "a minute ago" / "an hour ago" -> treat as 1 unit
    t = re.sub(r"\b(a|an)\b", "1", t)
    m = _REL_RE.search(t)
    if not m:
        return None
    num = int(m.group("num"))
    unit = m.group("unit").lower()
    return now - timedelta(seconds=num * _REL_UNIT_SECONDS[unit])


def parse_absolute_time(val: Any) -> datetime | None:
    """Parse an ISO-8601 string or unix epoch (s or ms) into UTC datetime."""
    if val is None:
        return None
    # Numeric epoch
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        num = float(val)
        # Milliseconds if implausibly large for seconds.
        if num > 1e12:
            num /= 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO string
    s = str(val).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------

def extract_records(payload: Any) -> list[dict[str, Any]]:
    """The response may be a bare list, or wrapped in a dict under a common
    key. Return the list of sale records."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("sales", "data", "results", "history", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def make_sale_id(
    market_hash_name: str,
    price_cents: int | None,
    float_value: float | None,
    paint_seed: int | None,
    sold_at: str | None,
) -> str:
    """Deterministic fallback id when the API gives none. Stable across polls
    so the same sale dedupes correctly."""
    basis = "|".join(
        [
            market_hash_name or "",
            str(price_cents if price_cents is not None else ""),
            f"{float_value:.10f}" if float_value is not None else "",
            str(paint_seed if paint_seed is not None else ""),
            sold_at or "",
        ]
    )
    return "h:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def parse_sale(
    record: dict[str, Any],
    *,
    item_id: int,
    fallback_name: str,
    scraped_at_iso: str,
    now: datetime | None = None,
) -> Sale:
    """Normalize one raw record into a Sale."""
    now = now or datetime.now(timezone.utc)

    name = _first(record, NAME_PATHS) or fallback_name
    price_cents = _price_to_cents(_first(record, PRICE_PATHS))
    price = round(price_cents / 100.0, 2) if price_cents is not None else None
    float_value = _to_float(_first(record, FLOAT_PATHS))
    paint_seed = _to_int(_first(record, SEED_PATHS))
    paint_index = _to_int(_first(record, PAINT_INDEX_PATHS))

    # Sale time: prefer an absolute timestamp; fall back to relative text.
    estimated = False
    dt = parse_absolute_time(_first(record, SOLD_AT_PATHS))
    if dt is None:
        rel = _first(record, SOLD_REL_PATHS)
        if isinstance(rel, str):
            dt = parse_relative_time(rel, now=now)
            estimated = dt is not None
    sold_at_iso = _iso(dt)

    stickers = _first(record, STICKER_PATHS)
    if not isinstance(stickers, list):
        stickers = None

    api_id = _first(record, ID_PATHS)
    sale_id = (
        str(api_id)
        if api_id not in (None, "")
        else make_sale_id(name, price_cents, float_value, paint_seed, sold_at_iso)
    )

    return Sale(
        sale_id=sale_id,
        item_id=item_id,
        market_hash_name=name,
        price_cents=price_cents,
        price=price,
        float_value=float_value,
        paint_seed=paint_seed,
        paint_index=paint_index,
        sold_at=sold_at_iso,
        sold_at_estimated=estimated,
        stickers=stickers,
        raw_json=json.dumps(record, ensure_ascii=False),
        scraped_at=scraped_at_iso,
    )


def parse_sales(
    payload: Any,
    *,
    item_id: int,
    fallback_name: str,
    scraped_at_iso: str,
    now: datetime | None = None,
) -> list[Sale]:
    records = extract_records(payload)
    out: list[Sale] = []
    for rec in records:
        if isinstance(rec, dict):
            out.append(
                parse_sale(
                    rec,
                    item_id=item_id,
                    fallback_name=fallback_name,
                    scraped_at_iso=scraped_at_iso,
                    now=now,
                )
            )
    return out
