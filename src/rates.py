"""USD -> CNY rate: fetching, storing and sanity-checking it.

Prices are collected and stored in USD; the rate is only a display convenience,
so it lives in `settings` and never touches the sales rows. Anything that reads
it must survive the rate being absent — a missing rate means "show USD", not an
error.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("csfloat.rates")

# A rate outside this range is a parsing mistake, not a market move: USD/CNY has
# stayed between roughly 6 and 8 for two decades. Refusing the value keeps a
# changed API shape from silently multiplying every price on the dashboard.
RATE_MIN, RATE_MAX = 3.0, 15.0

# How often the collector refreshes it. One request a day is nothing against
# the quota, and the rate does not move enough to justify more.
REFRESH_SECONDS = 12 * 3600.0

# Where to read it from. CSFloat serves the rate its own frontend uses; the
# path is configurable because it is an internal endpoint that can move.
DEFAULT_RATE_URL = "https://csfloat.com/api/v1/meta/exchange-rates"

# Keys that could hold the CNY rate. Deliberately all CNY-specific: a generic
# "rate" would match the euro row of a currency list just as happily.
CNY_KEYS = ("cny", "usd_cny", "usdcny", "cny_rate")


def extract_cny_rate(payload: Any) -> float | None:
    """Find a plausible USD->CNY rate anywhere in a JSON payload.

    Written defensively on purpose: the endpoint is undocumented, so rather
    than depending on one shape we walk the structure for a CNY-ish key with a
    believable number under it.
    """
    found = _walk(payload)
    if found is None:
        return None
    # Some APIs quote the inverse (USD per CNY, ~0.14).
    if 1 / RATE_MAX <= found <= 1 / RATE_MIN:
        found = 1.0 / found
    return round(found, 4) if RATE_MIN <= found <= RATE_MAX else None


def _walk(node: Any, key_hint: str = "") -> float | None:
    if isinstance(node, dict):
        # A row like {"code": "CNY", "rate": 7.2}: the currency is a value here,
        # not a key, so look for the number beside it.
        if any(isinstance(v, str) and v.strip().upper() == "CNY"
               for v in node.values()):
            for key, value in node.items():
                if str(key).lower() in ("rate", "value", "price", "amount"):
                    number = _as_number(value)
                    if number is not None:
                        return number
        for key, value in node.items():
            k = str(key).lower()
            if k in CNY_KEYS or "cny" in k:
                number = _as_number(value)
                if number is not None:
                    return number
                deeper = _walk(value, k)
                if deeper is not None:
                    return deeper
        for key, value in node.items():
            deeper = _walk(value, str(key).lower())
            if deeper is not None:
                return deeper
        return None
    if isinstance(node, list):
        for entry in node:
            deeper = _walk(entry, key_hint)
            if deeper is not None:
                return deeper
        return None
    if "cny" in key_hint:
        return _as_number(node)
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def validate_rate(value: Any) -> tuple[float | None, str]:
    """(rate, message) for a rate typed in by hand."""
    number = _as_number(value)
    if number is None:
        return None, "курс должен быть числом, например 7.15"
    if not RATE_MIN <= number <= RATE_MAX:
        return None, f"курс вне разумного диапазона {RATE_MIN}–{RATE_MAX}"
    return round(number, 4), "ok"
