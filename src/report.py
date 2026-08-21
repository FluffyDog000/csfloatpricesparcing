"""Aggregation & reporting: exact per-sale views, float-bucket aggregation,
and paint-seed slices. Used by the report.py CLI entrypoint.
"""
from __future__ import annotations

import csv as csvmod
import re
import statistics
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any

from .db import Database


_PERIOD_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)
_PERIOD_SECONDS = {"h": 3600, "d": 86400, "w": 604800}


def period_to_since_iso(period: str | None) -> str | None:
    """Convert "7d"/"24h"/"2w" to an ISO cutoff timestamp. None => no limit."""
    if not period or period.lower() in ("all", "0"):
        return None
    m = _PERIOD_RE.match(period)
    if not m:
        raise ValueError(f"Bad period '{period}'. Use forms like 24h, 7d, 2w, all.")
    seconds = int(m.group(1)) * _PERIOD_SECONDS[m.group(2).lower()]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return cutoff.replace(microsecond=0).isoformat()


def _prices(rows: list[dict[str, Any]]) -> list[float]:
    return [r["price"] for r in rows if r["price"] is not None]


def bucket_label(float_value: float, size: float) -> str:
    lo = (int(float_value / size)) * size
    hi = lo + size
    return f"{lo:.4f}-{hi:.4f}"


def bucket_key(float_value: float, size: float) -> float:
    return (int(float_value / size)) * size


def aggregate_buckets(
    rows: list[dict[str, Any]], bucket_size: float
) -> list[dict[str, Any]]:
    """Group sales by float bucket; compute count/avg/median/min/max price."""
    groups: dict[float, list[dict[str, Any]]] = {}
    for r in rows:
        fv = r["float_value"]
        if fv is None or r["price"] is None:
            continue
        groups.setdefault(bucket_key(fv, bucket_size), []).append(r)

    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        grp = groups[key]
        prices = _prices(grp)
        out.append(
            {
                "bucket": f"{key:.4f}-{key + bucket_size:.4f}",
                "count": len(grp),
                "avg_price": round(statistics.mean(prices), 2) if prices else None,
                "median_price": round(statistics.median(prices), 2) if prices else None,
                "min_price": round(min(prices), 2) if prices else None,
                "max_price": round(max(prices), 2) if prices else None,
            }
        )
    return out


def aggregate_seeds(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by paint_seed; count/avg/min/max price."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["paint_seed"], []).append(r)
    out: list[dict[str, Any]] = []
    for seed in sorted(groups, key=lambda s: (s is None, s)):
        grp = groups[seed]
        prices = _prices(grp)
        out.append(
            {
                "paint_seed": seed if seed is not None else "(none)",
                "count": len(grp),
                "avg_price": round(statistics.mean(prices), 2) if prices else None,
                "min_price": round(min(prices), 2) if prices else None,
                "max_price": round(max(prices), 2) if prices else None,
            }
        )
    return out


def exact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flattened per-sale view for CSV / detailed table."""
    return [
        {
            "sold_at": r["sold_at"],
            "price": r["price"],
            "float_value": r["float_value"],
            "paint_seed": r["paint_seed"],
            "paint_index": r["paint_index"],
            "sold_at_estimated": bool(r["sold_at_estimated"]),
            "sale_id": r["sale_id"],
        }
        for r in rows
    ]


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = StringIO()
    writer = csvmod.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def build_report(
    db: Database,
    market_hash_name: str,
    period: str | None,
    bucket_size: float,
    paint_seed: int | None = None,
) -> dict[str, Any]:
    """Assemble the full report structure for one item."""
    item_id = db.get_item_id(market_hash_name)
    if item_id is None:
        raise KeyError(f"Item not tracked: {market_hash_name}")
    since = period_to_since_iso(period)
    rows = db.query_sales(item_id, since_iso=since, paint_seed=paint_seed)
    prices = _prices(rows)
    return {
        "item": market_hash_name,
        "period": period or "all",
        "since": since,
        "paint_seed_filter": paint_seed,
        "total_sales": len(rows),
        "overall": {
            "avg_price": round(statistics.mean(prices), 2) if prices else None,
            "median_price": round(statistics.median(prices), 2) if prices else None,
            "min_price": round(min(prices), 2) if prices else None,
            "max_price": round(max(prices), 2) if prices else None,
        },
        "buckets": aggregate_buckets(rows, bucket_size),
        "seeds": aggregate_seeds(rows),
        "rows": rows,
    }
