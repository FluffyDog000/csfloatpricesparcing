"""Does the order sweep reach both ends of an item's float range?

A page of /listings comes back in CSFloat's own order, so on a liquid item all
fifty lots can sit in the middle of the range. Orders scoped to an end that no
lot represents are then invisible: on ★ Driver Gloves | Brocade Flowers
(Field-Tested) the swept book topped out at $169 while the site showed $244
bids scoped to float 0.15-0.17.
"""


def test_wear_range_is_read_from_the_item_name():
    from src.orders import wear_range

    assert wear_range("★ Driver Gloves | Brocade Flowers (Field-Tested)") == (0.15, 0.38)
    assert wear_range("AK-47 | Redline (Minimal Wear)") == (0.07, 0.15)
    assert wear_range("Gloves") is None, "no wear named = nothing to measure against"


def test_a_plan_stuck_in_the_middle_reports_both_ends_missing():
    from src.orders import coverage_gaps, plan_bands

    ft = (0.15, 0.38)
    middle = [{"id": f"L{i}", "float": 0.22 + i * 0.01} for i in range(3)]
    assert coverage_gaps(plan_bands(middle), ft) == ("low", "high")

    low_only = [{"id": f"L{i}", "float": 0.15 + i * 0.01} for i in range(3)]
    assert coverage_gaps(plan_bands(low_only), ft) == ("high",)

    spanning = [{"id": "a", "float": 0.151}, {"id": "b", "float": 0.372}]
    assert coverage_gaps(plan_bands(spanning), ft) == ()

    # Without a known wear there is nothing to measure against, so no extra
    # request is spent guessing.
    assert coverage_gaps(plan_bands(middle), None) == ()


def test_listings_from_several_pages_are_deduplicated():
    from src.orders import merge_listings

    a = [{"id": "L1", "float": 0.15}, {"id": "L2", "float": 0.30}]
    b = [{"id": "L2", "float": 0.30}, {"id": "L3", "float": 0.37}]
    assert sorted(r["id"] for r in merge_listings(a, b)) == ["L1", "L2", "L3"]


def test_the_sweep_asks_for_the_float_end_its_first_page_missed():
    """The regression: a mid-range page must not be where the sweep stops."""
    import os
    import tempfile
    import logging

    logging.disable(logging.WARNING)
    from src.config import load_config
    from src.db import Database
    from src.csfloat_client import CSFloatClient
    from src.collector import Collector

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Driver Gloves | Brocade Flowers (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    middle = {"data": [{"id": f"M{i}", "item": {"float_value": 0.22 + i * 0.01}}
                       for i in range(3)]}
    lowest = {"data": [{"id": "LOW", "item": {"float_value": 0.152}}]}
    highest = {"data": [{"id": "HIGH", "item": {"float_value": 0.376}}]}
    asked = []

    def fake(url, headers=None):
        asked.append(url)
        if "/buy-orders" in url:
            listing = url.split("/listings/")[1].split("/")[0]
            return {"data": [{"price": 24400 if listing == "LOW" else 16900,
                              "qty": 1}]}
        if "sort_by=lowest_float" in url:
            return lowest
        if "sort_by=highest_float" in url:
            return highest
        return middle

    col.client.fetch_json = fake
    result = col.sweep_buy_orders(name, item_id)

    assert any("sort_by=lowest_float" in u for u in asked), \
        "the missing low-float end has to be asked for by name"
    swept = [u.split("/listings/")[1].split("/")[0]
             for u in asked if "/buy-orders" in u]
    assert "LOW" in swept and "HIGH" in swept
    assert result["bands"] == 5, "three middle bands plus both ends"
    assert result["span"] == "0.15-0.37", "the panel has to say what was covered"
    assert max(o["price"] for o in db.buy_orders(item_id)) == 244.0, \
        "the top of the book is only visible from a low-float lot"
    db.close()


def test_a_sweep_leaves_from_one_address():
    """CSFloat counts how many IPs an account speaks from, and a sweep is a
    burst: thirteen requests hopping across routes read as thirteen addresses
    in ninety seconds, which is what quarantined the whole rotating set."""
    import logging

    logging.disable(logging.WARNING)
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://a:1", "http://b:1", "http://c:1"],
                     use_direct=False)
    pinned = pool.pin()
    assert pinned is not None
    assert [pool.pick().key for _ in range(6)] == [pinned.key] * 6

    # Unpinned it does not go back to alternating either: routes are drained
    # one at a time, so the account still speaks from one address.
    pool.unpin()
    assert len({pool.pick().key for _ in range(20)}) == 1

    # A pin must never wedge the pool: once that route is rate-limited the
    # next request goes somewhere else instead of into the same wall.
    pinned = pool.pin()
    pool.record_429(pinned, 600.0)
    assert pool.pick().key != pinned.key
    pool.unpin()


def test_routes_are_drained_one_at_a_time():
    """The spread is what CSFloat counts. A route keeps serving until its
    quota is down to the reserve; only then does the next address open."""
    import time as clock

    from src.proxies import ProxyPool

    pool = ProxyPool(["http://a:1", "http://b:1", "http://c:1"],
                     use_direct=False, reserve=15)
    soon = int(clock.time()) + 3600
    pool.routes["http://a:1"].remaining, pool.routes["http://a:1"].reset = 300, soon
    pool.routes["http://b:1"].remaining, pool.routes["http://b:1"].reset = 40, soon
    # http://c:1 stays unknown: an address nobody has used yet.

    assert {pool.pick().key for _ in range(10)} == {"http://b:1"}, \
        "the route closest to its ceiling is spent first"

    # Down to the reserve it drops out, and the next one takes over — still
    # one address at a time, and the untouched one stays in reserve.
    pool.routes["http://b:1"].remaining = 15
    assert {pool.pick().key for _ in range(10)} == {"http://a:1"}

    pool.routes["http://a:1"].remaining = 15
    assert pool.pick().key == "http://c:1", "only now is a fresh address opened"


def test_unopened_routes_count_towards_the_budget():
    """Draining leaves most of the pool deliberately unopened, and summing only
    what CSFloat has spoken about reported forty fresh proxies as nothing: the
    dashboard read "0 доступно сейчас" beside twenty thousand requests held in
    reserve, and the pacing maths throttled to fit that phantom budget."""
    import time as clock

    from src.proxies import ASSUMED_IP_LIMIT, ProxyPool

    pool = ProxyPool([f"http://p{i}:1" for i in range(40)], use_direct=False)
    assert pool.total_remaining() == 40 * ASSUMED_IP_LIMIT
    assert pool.usable_remaining() == 40 * ASSUMED_IP_LIMIT
    assert pool.total_limit() == 40 * ASSUMED_IP_LIMIT

    # Once CSFloat has spoken about a route, its own number is what counts.
    route = pool.routes["http://p0:1"]
    route.limit, route.remaining = 500, 120
    route.reset = int(clock.time()) + 3600
    assert pool.total_remaining() == 39 * ASSUMED_IP_LIMIT + 120

    # The routes table still shows an unopened address as unopened, though —
    # there the honest answer is "no number yet", not an assumption.
    fresh = next(r for r in pool.snapshot() if r["key"] == "http://p1:1")
    assert fresh["remaining"] is None


def test_a_route_refused_for_orders_is_faulted_not_fatal():
    """"Disable your VPN" is about the exit address, not the account. Aborting
    the sweep on it left half a book on screen — five bands read, the rest
    dropped — and the same address was handed out again on the next press."""
    import os
    import tempfile
    import logging

    logging.disable(logging.WARNING)
    from src.config import load_config
    from src.db import Database
    from src.csfloat_client import CSFloatClient, VpnBlocked
    from src.collector import Collector

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    item_id = db.add_item("Gloves")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    col.client.pool.replace(["http://a:1", "http://b:1"], use_direct=False)

    listings = {"data": [{"id": f"L{i}", "item": {"float_value": 0.20 + i * 0.01}}
                         for i in range(4)]}
    seen = []

    def fake(url, headers=None):
        if "/buy-orders" not in url:
            return listings
        seen.append(url)
        if len(seen) == 1:                      # the first band is refused
            raise VpnBlocked("Disable your VPN", None)
        return {"data": [{"price": 16900, "qty": 1}]}

    col.client.fetch_json = fake
    result = col.sweep_buy_orders("Gloves", item_id)

    assert len(seen) == 4, "the sweep must try every band, not stop at the first"
    assert result["bands"] == 3, "three bands answered"
    assert result["orders"] == 1, "and what they returned was stored"
    assert "недоступны в принципе" not in (result["error"] or "")


def test_a_sweep_starts_on_a_proxy_with_budget_to_finish():
    """Draining hands out the most spent address, and the server's own IP is
    the most spent of all — so every sweep began there and drew a 429 on its
    first request, over and over. A sweep needs a proxied address with enough
    budget to reach the last band."""
    import time as clock

    from src.proxies import ProxyPool

    pool = ProxyPool(["http://thin:1", "http://deep:1"], use_direct=True)
    soon = int(clock.time()) + 3600
    pool.routes["direct"].remaining, pool.routes["direct"].reset = 40, soon
    pool.routes["http://thin:1"].remaining = 20
    pool.routes["http://thin:1"].reset = soon
    pool.routes["http://deep:1"].remaining = 400
    pool.routes["http://deep:1"].reset = soon

    assert pool.pin(for_orders=True).key == "http://deep:1"
    pool.unpin()

    # Sales history still drains, server IP and all: one request always fits.
    assert pool.pick().key == "http://thin:1"

    # With nothing else left, a thin route is still better than no sweep, and
    # the drain order decides which of them it is.
    pool.routes["http://deep:1"].remaining = 25
    assert pool.pin(for_orders=True).key == "http://thin:1"
    pool.unpin()


def test_a_blocked_route_still_serves_sales_but_not_orders():
    from src.proxies import ProxyPool

    pool = ProxyPool(["http://a:1", "http://b:1"], use_direct=False)
    bad = pool.routes["http://a:1"]
    pool.mark_vpn_blocked(bad)

    assert pool.pin(for_orders=True).key == "http://b:1"
    pool.unpin()
    # Sales history does not care about the refusal, so the route stays usable.
    assert pool.has_order_route() is True
    assert any(pool.pick().key == "http://a:1" for _ in range(20))


def test_a_wearless_item_costs_no_extra_lookup():
    """The probe is paid for out of the same quota as everything else, so it
    only fires where a gap can actually be proven."""
    import os
    import tempfile
    import logging

    logging.disable(logging.WARNING)
    from src.config import load_config
    from src.db import Database
    from src.csfloat_client import CSFloatClient
    from src.collector import Collector

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    item_id = db.add_item("Gloves")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    listings = {"data": [{"id": f"L{i}", "item": {"float_value": 0.22 + i * 0.01}}
                         for i in range(2)]}
    asked = []

    def fake(url, headers=None):
        asked.append(url)
        if "/buy-orders" in url:
            return {"data": [{"price": 16900, "qty": 1}]}
        return listings

    col.client.fetch_json = fake
    result = col.sweep_buy_orders("Gloves", item_id)

    assert not any("sort_by" in u for u in asked)
    assert result["requests"] == 3, "one listings page, two bands"
    db.close()


def test_a_sweep_where_nothing_answered_says_so():
    """Silence read as an empty book. When every band failed, the panel showed
    no orders and no error, which looks like "nobody is bidding" rather than
    "the sweep never got an answer" — and those call for opposite actions."""
    import logging
    import os
    import tempfile

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    item_id = db.add_item("Gloves")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    listings = {"data": [{"id": f"L{i}", "item": {"float_value": 0.20 + i * 0.01}}
                         for i in range(3)]}

    def fake(url, headers=None):
        if "/buy-orders" in url:
            raise RuntimeError("connection reset")
        return listings

    col.client.fetch_json = fake
    result = col.sweep_buy_orders("Gloves", item_id)

    assert result["bands"] == 0 and result["failed_bands"] == 3
    assert result["error"], "a sweep that read nothing must not report silence"
    assert "не ответила" in result["error"]


def test_the_book_is_read_on_the_api_key_without_the_cookie():
    """Tested against the live endpoint: an API key alone returns the book.
    The 403 that made this look cookie-only said "Disable your VPN to view buy
    orders" - the exit address was refused, not the key.

    Reads are nearly all of the traffic, and replaying a browser session
    across a dozen addresses is what drew "too many requests from too many
    IPs"."""
    import logging
    import os
    import tempfile

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    cfg.http.api_key = "test-key"
    db = Database(cfg.db_path)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    headers = col._book_headers()
    assert headers["Authorization"] == "test-key"
    assert headers["Cookie"] is None, \
        "None removes the session's cookie rather than merging it in"

    # Without a key nothing changes: the session is used exactly as before.
    cfg.http.api_key = None
    assert col._book_headers() is None
    db.close()


def test_every_book_request_carries_those_headers():
    """Two call sites read the book; one left behind would keep sending the
    cookie and the change would be half-made."""
    import pathlib

    source = pathlib.Path("src/collector.py").read_text(encoding="utf-8")
    calls = [line for line in source.splitlines()
             if "ORDERS_PATH.format" in line]
    assert len(calls) == 2, f"call sites moved: {calls}"
    assert source.count("headers=self._book_headers()") == 2
