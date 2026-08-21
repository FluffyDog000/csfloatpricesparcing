"""Plain data holders shared across modules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Sale:
    """A single normalized sale record ready for storage."""

    sale_id: str
    item_id: int
    market_hash_name: str
    price_cents: int | None
    price: float | None
    float_value: float | None
    paint_seed: int | None
    paint_index: int | None
    sold_at: str | None            # ISO-8601 UTC
    sold_at_estimated: bool
    stickers: list[dict[str, Any]] | None
    raw_json: str | None
    scraped_at: str                # ISO-8601 UTC
