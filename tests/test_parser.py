"""Tests for parsing & aggregation (pure functions, no network)."""
from __future__ import annotations

import time
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


def test_validate_proxy_accepts_supported_urls_and_rejects_junk():
    from src.proxies import validate_proxy

    for good in ("http://1.2.3.4:8080", "https://host.example:3128",
                 "socks5://user:pass@host:1080", "socks5h://host:1080"):
        ok, why = validate_proxy(good)
        assert ok, f"{good} should be valid ({why})"

    for bad in ("ftp://host:21",         # unsupported scheme
                "http://:8080",          # no host
                "http://host",           # no port
                "http://host:99999"):    # port out of range
        ok, why = validate_proxy(bad)
        assert not ok and why, f"{bad} should be rejected"


def test_parse_proxy_list_splits_and_trims():
    from src.proxies import parse_proxy_list

    raw = " http://a:1 \n\nsocks5://b:2 ; http://c:3 , \n"
    assert parse_proxy_list(raw) == ["http://a:1", "socks5://b:2", "http://c:3"]
    assert parse_proxy_list("") == []
    assert parse_proxy_list(None) == []


def test_pool_replace_keeps_quota_of_surviving_routes():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://a:1", "http://b:2"], use_direct=False)
    pool.routes["http://a:1"].remaining = 300
    pool.routes["http://a:1"].limit = 500

    changed = pool.replace(["http://a:1", "http://c:3"], use_direct=False)
    assert changed
    assert sorted(pool.routes) == ["http://a:1", "http://c:3"]
    assert pool.routes["http://a:1"].remaining == 300, "surviving route keeps its quota"
    assert pool.routes["http://c:3"].remaining is None

    assert not pool.replace(["http://a:1", "http://c:3"], use_direct=False), \
        "replacing with the same list is a no-op"


def test_pool_never_exposes_proxy_credentials():
    import json
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://user:sekret@1.2.3.4:8080"], use_direct=False)
    dumped = pool.to_json()
    assert "sekret" not in dumped and "user" not in dumped
    assert json.loads(dumped)[0]["key"].startswith("http://1.2.3.4:8080#")


def test_sticky_sessions_on_one_gateway_stay_separate_routes():
    from src.proxies import ProxyPool

    # A provider's sticky sessions differ only by the login (…-sid-N-…); they
    # are distinct exit IPs and must not collapse into one route.
    lines = [f"gate.example.com:8888:acct-sid-{i}-ttl-30:pw" for i in (1, 2, 3)]
    pool = ProxyPool(lines, use_direct=False)
    assert len(pool.routes) == 3

    dumped = pool.to_json()
    assert "acct" not in dumped and "pw@" not in dumped, "no credentials leak"

    # Spending one session's quota must not touch the others.
    keys = sorted(pool.routes)
    now = int(time.time())
    pool.routes[keys[0]].remaining, pool.routes[keys[0]].reset = 0, now + 3600
    assert {pool.pick().key for _ in range(6)} <= set(keys[1:])


def test_rotating_marker_and_seller_formats_are_parsed():
    from src.proxies import split_proxy_flags, validate_proxy

    assert split_proxy_flags("http://gate:7000 #rotating") == ("http://gate:7000", True)
    assert split_proxy_flags("http://gate:7000 #rot") == ("http://gate:7000", True)
    assert split_proxy_flags("http://gate:7000") == ("http://gate:7000", False)

    # The format proxy sellers hand out, with and without a scheme.
    assert split_proxy_flags("1.2.3.4:8080:user:pass") == \
        ("http://user:pass@1.2.3.4:8080", False)
    assert split_proxy_flags("user:pass@1.2.3.4:8080") == \
        ("http://user:pass@1.2.3.4:8080", False)
    assert validate_proxy("1.2.3.4:8080:user:pass")[0]

    # A marker on its own is not a proxy.
    assert not validate_proxy("#rotating")[0]


def test_rotating_route_uses_a_local_budget_not_response_headers():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://gate:7000 #rotating"], use_direct=False,
                     rotating_limit=3)
    route = pool.routes["http://gate:7000"]
    assert route.rotating

    # A rotating endpoint reports a fresh quota from every exit IP; that must
    # not be read as "we still have 499 requests left".
    for _ in range(3):
        picked = pool.pick()
        assert picked is not None
        pool.record_headers(picked, 500, 499, int(time.time()) + 3600)

    assert route.window_used == 3
    assert route.effective_remaining() == 0
    assert pool.pick() is None, "local budget spent -> route must stop"
    assert pool.wait_seconds() > 0


def test_rotating_budget_survives_a_restart():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://gate:7000 #rotating"], use_direct=False,
                     rotating_limit=10)
    for _ in range(4):
        pool.pick()
    saved = pool.usage_snapshot()

    fresh = ProxyPool(["http://gate:7000 #rotating"], use_direct=False,
                      rotating_limit=10)
    fresh.restore_usage(saved)
    assert fresh.routes["http://gate:7000"].effective_remaining() == 6, \
        "a restart must not hand back an already-spent budget"


def test_park_rotating_leaves_fixed_routes_working():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://gate:7000 #rotating", "http://fixed:8080"],
                     use_direct=False)
    assert pool.park_rotating(3600) == 1
    keys = {pool.pick().key for _ in range(5)}
    assert keys == {"http://fixed:8080"}, \
        "only the rotating route is parked after an account-level IP complaint"


def test_client_detects_account_level_ip_complaint():
    from src.csfloat_client import CSFloatClient

    class FakeResp:
        text = '{"error": "You have been making too many requests from too many IPs,"}'

    client = CSFloatClient.__new__(CSFloatClient)
    assert client._account_ip_complaint(FakeResp())

    class Ordinary:
        text = '{"error": "rate limit exceeded"}'

    assert not client._account_ip_complaint(Ordinary())


def test_rotating_route_is_overflow_only():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://gate:7000 #rotating", "http://fixed:8080"],
                     use_direct=True, rotating_limit=300)

    used = {pool.pick().key for _ in range(8)}
    assert "http://gate:7000" not in used, \
        "a rotating route must not be used while fixed routes have quota"

    now = int(time.time())
    for key in ("direct", "http://fixed:8080"):
        pool.routes[key].remaining, pool.routes[key].reset = 0, now + 3600

    assert {pool.pick().key for _ in range(4)} == {"http://gate:7000"}, \
        "once fixed routes are spent the rotating one takes over"


def test_backoff_signals_do_not_compound():
    from src.collector import Collector

    col = Collector.__new__(Collector)
    col.pace_multiplier = lambda: 8.0          # maxed out by past 429s
    col.cached_budget_factor = lambda: 8.0     # quota spent for this window

    # Multiplying these gives x64, which turns a 2h interval into five days.
    assert col.stretch_factor() == 8.0

    col.cached_budget_factor = lambda: 20.0
    assert col.stretch_factor() == 20.0, "the tighter signal wins"

    col.pace_multiplier = lambda: 1.0
    col.cached_budget_factor = lambda: 1.0
    assert col.stretch_factor() == 1.0


def test_pace_recovery_runs_on_the_clock_not_on_polls():
    """A x8 backoff must unwind even while polling is nearly stopped — the
    multiplier is what makes polls rare, so tying recovery to them deadlocks."""
    from datetime import datetime, timedelta, timezone
    from src.collector import Collector
    from src.pacing import PACE_RECOVER_SECONDS, PACE_UP_FACTOR

    store: dict[str, str] = {}

    class FakeDB:
        def get_setting(self, key, default=None):
            return store.get(key, default)

        def set_setting(self, key, value):
            store[key] = value

    col = Collector.__new__(Collector)
    col.db = FakeDB()

    def hours_ago(h):
        return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()

    store["pace_multiplier"] = "8.0"
    store["pace_last_429"] = hours_ago(3)      # quiet for three hours

    col.maybe_speed_up()
    eased = float(store["pace_multiplier"])
    assert eased == round(8.0 / PACE_UP_FACTOR, 3), "one step down per clean hour"

    # A second call inside the same hour must not double-step.
    col.maybe_speed_up()
    assert float(store["pace_multiplier"]) == eased

    # ... but the next clean hour eases it again.
    store["pace_last_step"] = hours_ago(PACE_RECOVER_SECONDS / 3600 + 0.1)
    col.maybe_speed_up()
    assert float(store["pace_multiplier"]) < eased

    # A fresh 429 blocks recovery entirely.
    store["pace_multiplier"] = "8.0"
    store["pace_last_429"] = hours_ago(0.1)
    col.maybe_speed_up()
    assert float(store["pace_multiplier"]) == 8.0


def test_pace_recovery_is_symmetric_with_the_backoff():
    from src.pacing import PACE_MAX, PACE_UP_FACTOR

    # Six clean hours should undo a backoff that took six 429s to build.
    mult = 1.0
    for _ in range(6):
        mult = min(mult * PACE_UP_FACTOR, PACE_MAX)
    assert mult == PACE_MAX

    hours = 0
    while mult > 1.0 and hours < 50:
        mult = max(mult / PACE_UP_FACTOR, 1.0)
        hours += 1
    assert hours <= 6, f"x{PACE_MAX} should unwind within hours, took {hours}"
