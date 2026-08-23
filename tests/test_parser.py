"""Tests for parsing & aggregation (pure functions, no network)."""
from __future__ import annotations

from datetime import datetime, timezone

from src import parser
from src.report import (
    aggregate_buckets,
    aggregate_seeds,
    date_bounds_iso,
    period_to_since_iso,
)


NOW = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)

# A sample payload in CSFloat's expected shape (nested "item", cents, ISO time).
SAMPLE = [
    {
        "id": "sale-1",
        "price": 123456,  # cents => $1234.56
        "sold_at": "2024-01-02T11:00:00Z",
        "item": {
            "float_value": 0.1543,
            "paint_seed": 13,
            "paint_index": 10024,
            "market_hash_name": "★ Specialist Gloves | Lt. Commander (Field-Tested)",
            "stickers": [{"name": "Sticker A", "slot": 0}],
        },
    },
    {
        # No id -> deterministic hash id. Price as dollars float.
        "price": 999.5,
        "sold_at": 1704193200,  # unix epoch seconds -> 2024-01-02T11:00:00Z
        "item": {"float_value": 0.1611, "paint_seed": 42},
    },
]


def test_parse_nested_shape():
    sales = parser.parse_sales(
        SAMPLE, item_id=1, fallback_name="fallback",
        scraped_at_iso="2024-01-02T12:00:00+00:00", now=NOW,
    )
    assert len(sales) == 2

    s0 = sales[0]
    assert s0.sale_id == "sale-1"
    assert s0.price_cents == 123456
    assert s0.price == 1234.56
    assert s0.float_value == 0.1543
    assert s0.paint_seed == 13
    assert s0.paint_index == 10024
    assert s0.sold_at == "2024-01-02T11:00:00+00:00"
    assert s0.stickers and s0.stickers[0]["name"] == "Sticker A"
    assert s0.sold_at_estimated is False

    s1 = sales[1]
    # Dollars float -> cents
    assert s1.price_cents == 99950
    assert s1.price == 999.5
    # Deterministic hash id when API gives none
    assert s1.sale_id.startswith("h:")


def test_deterministic_id_is_stable():
    a = parser.make_sale_id("X", 100, 0.15, 13, "2024-01-01T00:00:00+00:00")
    b = parser.make_sale_id("X", 100, 0.15, 13, "2024-01-01T00:00:00+00:00")
    c = parser.make_sale_id("X", 200, 0.15, 13, "2024-01-01T00:00:00+00:00")
    assert a == b
    assert a != c


def test_relative_time_parsing():
    dt = parser.parse_relative_time("54 minutes ago", now=NOW)
    assert dt == datetime(2024, 1, 2, 11, 6, 0, tzinfo=timezone.utc)
    assert parser.parse_relative_time("just now", now=NOW) == NOW
    assert parser.parse_relative_time("an hour ago", now=NOW) == datetime(
        2024, 1, 2, 11, 0, 0, tzinfo=timezone.utc
    )
    assert parser.parse_relative_time("garbage") is None


def test_relative_fallback_flags_estimated():
    payload = [{"price": 5000, "sold_ago": "10 minutes ago",
                "item": {"float_value": 0.2, "paint_seed": 1}}]
    sales = parser.parse_sales(
        payload, item_id=1, fallback_name="f",
        scraped_at_iso="2024-01-02T12:00:00+00:00", now=NOW,
    )
    assert sales[0].sold_at_estimated is True
    assert sales[0].sold_at == "2024-01-02T11:50:00+00:00"


def test_wrapped_payload():
    wrapped = {"sales": SAMPLE}
    assert len(parser.extract_records(wrapped)) == 2


def test_aggregate_buckets_and_seeds():
    rows = [
        {"float_value": 0.151, "price": 100.0, "paint_seed": 13},
        {"float_value": 0.158, "price": 120.0, "paint_seed": 13},
        {"float_value": 0.162, "price": 200.0, "paint_seed": 42},
    ]
    buckets = aggregate_buckets(rows, 0.01)
    # 0.151 & 0.158 both fall in 0.1500-0.1600
    b0 = next(b for b in buckets if b["bucket"].startswith("0.1500"))
    assert b0["count"] == 2
    assert b0["avg_price"] == 110.0
    assert b0["min_price"] == 100.0
    assert b0["max_price"] == 120.0

    seeds = aggregate_seeds(rows)
    seed13 = next(s for s in seeds if s["paint_seed"] == 13)
    assert seed13["count"] == 2


def test_count_is_sales_in_period():
    # count == number of sales in each group (= sales in the selected period).
    rows = [{"float_value": 0.151, "price": 100.0, "paint_seed": 1} for _ in range(10)]
    rows += [{"float_value": 0.161, "price": 100.0, "paint_seed": 2} for _ in range(4)]
    b = {x["bucket"][:6]: x["count"] for x in aggregate_buckets(rows, 0.01)}
    assert b["0.1500"] == 10
    assert b["0.1600"] == 4
    s = {x["paint_seed"]: x["count"] for x in aggregate_seeds(rows)}
    assert s == {1: 10, 2: 4}


def test_date_bounds_iso():
    since, until = date_bounds_iso("2026-08-01", "2026-08-07")
    assert since == "2026-08-01T00:00:00+00:00"
    assert until == "2026-08-07T23:59:59+00:00"
    assert date_bounds_iso(None, None) == (None, None)
    s2, u2 = date_bounds_iso("2026-08-01", None)
    assert s2 is not None and u2 is None


def test_period_parsing():
    assert period_to_since_iso("all") is None
    assert period_to_since_iso("7d") is not None
    try:
        period_to_since_iso("banana")
        assert False, "should have raised"
    except ValueError:
        pass


# --- adaptive pacing --------------------------------------------------------

def test_adaptive_minutes_scales_with_sale_rate():
    from datetime import datetime, timedelta, timezone
    from src.pacing import adaptive_minutes

    now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    first = (now - timedelta(days=10)).isoformat()

    # 240 sales/day -> 10 sales accumulate in ~1 hour
    fast = adaptive_minutes(2400, first, floor_minutes=15, ceiling_minutes=120, now=now)
    # 2 sales/day -> would take days, so it lands on the ceiling
    slow = adaptive_minutes(20, first, floor_minutes=15, ceiling_minutes=120, now=now)
    assert fast is not None and slow is not None
    assert fast < slow, "faster items must be polled more often"
    assert fast >= 15 and slow <= 120, "must respect floor/ceiling"

    # An extremely hot item is still clamped to the floor.
    assert adaptive_minutes(100000, first, floor_minutes=15,
                            ceiling_minutes=120, now=now) == 15


def test_adaptive_minutes_needs_history():
    from src.pacing import adaptive_minutes
    assert adaptive_minutes(2, "2026-08-01T00:00:00+00:00", floor_minutes=15) is None
    assert adaptive_minutes(50, None, floor_minutes=15) is None
