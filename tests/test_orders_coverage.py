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

    # Unpinned, the pool spreads the load across routes again.
    pool.unpin()
    assert len({pool.pick().key for _ in range(20)}) > 1

    # A pin must never wedge the pool: once that route is rate-limited the
    # next request goes somewhere else instead of into the same wall.
    pinned = pool.pin()
    pool.record_429(pinned, 600.0)
    assert pool.pick().key != pinned.key
    pool.unpin()


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
