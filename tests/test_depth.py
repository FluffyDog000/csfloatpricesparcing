"""Is the sell half of the cycle measured, or assumed?

Time-to-resell was 1 / (sales per day in the band), which assumes you are the
only seller: it put a $300 pair of gloves back on the market in seven hours and
inflated the modelled return threefold. The listings endpoint says how long the
queue is and how long its lots have sat there, and it is documented - min_float
and max_float cut the band server-side.
"""
import datetime as dt


def test_a_band_is_asked_for_by_float_not_filtered_afterwards():
    from src.depth import depth_url

    url = depth_url("https://csfloat.com", "★ Gloves | Fade (Field-Tested)",
                    0.31, 0.33)
    assert "min_float=0.31" in url and "max_float=0.33" in url, \
        "the band is cut server-side or it costs pages of the whole item"
    assert "type=buy_now" in url, "auctions do not queue the way listings do"
    assert "sort_by=lowest_price" in url
    assert "%E2%98%85" in url, "the star must survive the query string"


def test_a_listing_carries_its_age_and_its_offer_floor():
    from src.depth import extract_depth

    rows = extract_depth({"data": [
        {"id": "292312870132253796", "price": 8900, "type": "buy_now",
         "created_at": "2021-03-17T15:06:59.155367Z",
         "min_offer_price": 7565, "item": {"float_value": 0.26253828}},
    ]})
    assert rows[0]["price"] == 89.0, "cents, like everywhere else"
    assert rows[0]["min_offer_price"] == 75.65
    assert rows[0]["created_at"].startswith("2021-03-17")


def test_the_queue_and_its_age_are_what_the_band_reports():
    from src.depth import depth_profile

    now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
    rows = [
        {"id": "a", "float": 0.315, "price": 115.0, "type": "buy_now",
         "created_at": "2026-09-05T00:00:00Z", "min_offer_price": 100.0},
        {"id": "b", "float": 0.320, "price": 120.0, "type": "buy_now",
         "created_at": "2026-09-17T00:00:00Z", "min_offer_price": None},
        {"id": "c", "float": 0.350, "price": 111.0, "type": "buy_now",
         "created_at": "2026-09-18T00:00:00Z", "min_offer_price": None},
    ]
    bands = {b["float_min"]: b for b in depth_profile(rows, (0.31, 0.35), 0.02,
                                                      now=now)}

    assert bands[0.31]["listings"] == 2, "both lots in 0.31-0.33"
    assert bands[0.31]["cheapest"] == 115.0, \
        "the exit price is the cheapest ask, not the median of past sales"
    assert bands[0.31]["oldest_days"] == 14.0, \
        "a fortnight unsold is the measurement 1/lambda cannot give"
    assert bands[0.31]["median_age_days"] == 8.0

    # A lot reachable below its ask needs no buy order, no queue and no
    # outbidding war, so the band has to say how many there are.
    assert bands[0.31]["offerable"] == 1
    assert bands[0.31]["best_offer"] == 100.0

    assert bands[0.33]["listings"] == 0, "an empty band is still a data point"
    assert bands[0.33]["cheapest"] is None


def test_a_listing_without_a_usable_date_does_not_poison_the_band():
    from src.depth import depth_profile

    rows = [{"id": "a", "float": 0.16, "price": 10.0, "created_at": None,
             "min_offer_price": None},
            {"id": "b", "float": 0.16, "price": 12.0, "created_at": "рано",
             "min_offer_price": None}]
    band = depth_profile(rows, (0.15, 0.17), 0.02)[0]
    assert band["listings"] == 2
    assert band["median_age_days"] is None and band["oldest_days"] is None


def test_a_sweep_stores_a_profile_per_band():
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    asked = []

    def fake(url, headers=None):
        asked.append(url)
        return {"data": [{"id": f"L{len(asked)}", "price": 16000,
                          "type": "buy_now", "min_offer_price": 15000,
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.16}}]}

    col.client.fetch_json = fake
    result = col.sweep_listing_depth(name, item_id)

    assert result["span"] == "0.15-0.38"
    assert result["bands"] == 12, "0.15 to 0.38 in steps of 0.02"
    assert all("min_float" in u for u in asked), "every band asked by float"

    rows = db.listing_depth(item_id)
    assert len(rows) == 12
    assert rows[0]["listings"] >= 1 and rows[0]["cheapest"] == 160.0
    assert len({r["fetched_at"] for r in rows}) == 1, "one sweep, one timestamp"
    db.close()


def test_a_wearless_item_is_not_swept_at_all():
    """The bands come from the wear, so without one there is nothing to ask
    for — and a guessed 0-1 range would spend fifty requests on nothing."""
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
    item_id = db.add_item("Sticker | Fnatic")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    called = []
    col.client.fetch_json = lambda url, headers=None: called.append(url)

    result = col.sweep_listing_depth("Sticker | Fnatic", item_id)
    assert called == [], "no wear, no requests spent"
    assert result["error"] and result["bands"] == 0
    db.close()


def _ready():
    """A collector with one wearable item and no network."""
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    # A sweep re-reads stale sales first; here that read answers at once
    # instead of going to the network.
    col.client.fetch_latest_sales = lambda n: []
    return col, db, name, item_id


def test_both_sides_are_swept_together():
    """The sell-side sweep existed, was tested, and nothing ever called it -
    so every item in the report read "листинги не собраны", the exit price
    fell back to the sales median, and every ceiling came out too high.

    A trade has two halves. Reading one of them is not a sweep."""
    col, db, name, item_id = _ready()
    called = []
    col.sweep_buy_orders = lambda n, i: called.append(("orders", n, i))
    col.sweep_listing_depth = lambda n, i: called.append(("depth", n, i))

    col.sweep_both_sides(name, item_id)
    # Both, in whichever order the staler half asks for - which side leads is
    # its own question, tested beside this one.
    assert sorted(c[0] for c in called) == ["depth", "orders"]
    assert all(c[1:] == (name, item_id) for c in called)


def test_one_half_failing_does_not_cost_the_other():
    """Different budgets - the cookie and the residential route for the book,
    the API key for the listings - so a limit on one says nothing about the
    other."""
    col, db, name, item_id = _ready()
    done = []
    col.sweep_buy_orders = lambda n, i: done.append("orders") or {"orders": 1}

    def boom(n, i):
        raise RuntimeError("HTTP 429")

    col.sweep_listing_depth = boom
    out = col.sweep_both_sides(name, item_id)
    assert done == ["orders"]
    assert "429" in out["depth"]["error"]


def test_the_collector_loop_asks_for_both():
    """The regression that started this: the call site, not the method.

    The loop now hands its items to the parallel sweeper, so the guard
    follows: whichever of the two runs them, neither may sweep the book alone.
    """
    import pathlib

    loop = pathlib.Path("run_collector.py").read_text(encoding="utf-8")
    runner = pathlib.Path("src/parallel.py").read_text(encoding="utf-8")
    assert "sweep_items(" in loop
    assert "sweep_both_sides" in runner
    for source in (loop, runner):
        assert "sweep_buy_orders(" not in source, \
            "sweeping one side alone is what left every ceiling too high"


def _held(db, item_id, ranges):
    for lo, hi in ranges:
        db.upsert_our_order(item_id, lo, hi, 100.0, 120.0, state="live",
                            remote_id=f"r{lo}")
    return db.our_orders(item_id)


def test_asks_are_re_read_for_the_ranges_we_hold():
    """The exit price is the cheapest ask, and it came from a sweep nobody ran
    except by hand - so the defence could run for days against asks from
    another week, holding an order above a ceiling that had moved under it."""
    col, db, name, item_id = _ready()
    rows = _held(db, item_id, [(0.15, 0.16), (0.20, 0.21)])
    asked = []

    def answer(url, headers=None):
        asked.append(url)
        return {"data": [{"id": "L1", "price": 16000, "type": "buy_now",
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.155}}]}

    col.client.fetch_json = answer
    out = col.refresh_held_asks(name, item_id, rows, max_age_minutes=60)

    assert out["asked"] == 2, "one request per range held, no more"
    assert "min_float=0.15&max_float=0.16" in asked[0]
    assert "min_float=0.2&max_float=0.21" in asked[1]

    stored = {(r["float_min"], r["float_max"]): r["cheapest"]
              for r in db.listing_depth(item_id)}
    assert stored[(0.15, 0.16)] == 160.0
    db.close()


def test_a_reading_younger_than_the_interval_is_not_asked_for_again():
    """A fast defence must not ask the same question every few minutes."""
    col, db, name, item_id = _ready()
    rows = _held(db, item_id, [(0.15, 0.16)])
    col.client.fetch_json = lambda url, headers=None: {
        "data": [{"id": "L1", "price": 16000, "type": "buy_now",
                  "created_at": "2026-09-10T00:00:00Z",
                  "item": {"float_value": 0.155}}]}

    first = col.refresh_held_asks(name, item_id, rows, max_age_minutes=60)
    again = col.refresh_held_asks(name, item_id, rows, max_age_minutes=60)
    assert first["asked"] == 1 and again["asked"] == 0
    assert again["skipped"] == 1
    db.close()


def test_one_range_failing_does_not_cost_the_others():
    import requests

    col, db, name, item_id = _ready()
    rows = _held(db, item_id, [(0.15, 0.16), (0.20, 0.21)])

    def flaky(url, headers=None):
        if "0.15" in url:
            raise requests.HTTPError("HTTP 429")
        return {"data": [{"id": "L2", "price": 14000, "type": "buy_now",
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.205}}]}

    col.client.fetch_json = flaky
    out = col.refresh_held_asks(name, item_id, rows, max_age_minutes=60)
    assert out["errors"] == 1 and out["asked"] == 1
    assert [r["float_min"] for r in db.listing_depth(item_id)] == [0.2]
    db.close()


def test_a_narrow_reading_does_not_hide_the_grid_sweep():
    """Refreshing two held bands writes rows with a new timestamp. Keyed on
    the latest timestamp alone, every other band of the last full sweep would
    vanish behind them."""
    col, db, name, item_id = _ready()
    db.record_listing_depth(item_id, [
        {"float_min": 0.15, "float_max": 0.17, "listings": 3, "cheapest": 200.0,
         "median_age_days": 1.0, "oldest_days": 2.0, "offerable": 0,
         "best_offer": None},
        {"float_min": 0.30, "float_max": 0.32, "listings": 2, "cheapest": 150.0,
         "median_age_days": 1.0, "oldest_days": 2.0, "offerable": 0,
         "best_offer": None}])

    rows = _held(db, item_id, [(0.15, 0.16)])
    col.client.fetch_json = lambda url, headers=None: {
        "data": [{"id": "L1", "price": 19000, "type": "buy_now",
                  "created_at": "2026-09-10T00:00:00Z",
                  "item": {"float_value": 0.155}}]}
    col.refresh_held_asks(name, item_id, rows, max_age_minutes=0)

    bands = {(r["float_min"], r["float_max"]) for r in db.listing_depth(item_id)}
    assert (0.30, 0.32) in bands, "the far band of the full sweep survives"
    assert (0.15, 0.16) in bands, "and the narrow reading is there too"


def test_a_sweep_cut_short_records_only_the_bands_it_read():
    """The profile is built across the whole wear range, so recording it
    wholesale after a rate limit wrote "no lots" over every band still to
    come - newer than the real reading, and read in its place. A band with no
    lots is not a neutral record: it tells the pricing there is no queue.
    """
    import logging
    import os
    import tempfile

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient, RateLimited
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    calls = []

    def two_then_refused(url, headers=None):
        calls.append(url)
        if len(calls) > 2:
            raise RateLimited("429")
        return {"data": [{"id": f"L{len(calls)}", "price": 16000,
                          "type": "buy_now", "min_offer_price": None,
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.16 + (len(calls) - 1) * 0.02}}]}

    col.client.fetch_json = two_then_refused
    result = col.sweep_listing_depth(name, item_id)

    assert result["rate_limited"] and result["bands"] == 2
    stored = db.listing_depth(item_id)
    assert len(stored) == 2, "only what answered was written"
    assert [(b["float_min"], b["float_max"]) for b in stored] == \
        [(0.15, 0.17), (0.17, 0.19)], stored
    assert all(b["listings"] > 0 for b in stored), stored
    assert result["stopped_at"] == stored[-1]["float_max"], \
        "and it says where to resume"
    db.close()


def test_a_resumed_sweep_starts_at_the_float_it_stopped_on():
    """The bands below are already stored; reading them again spends the quota
    that stopped the sweep in the first place."""
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    asked = []
    col.client.fetch_json = lambda url, headers=None: (
        asked.append(url), {"data": []})[1]
    result = col.sweep_listing_depth(name, item_id, start=0.30)

    assert result["started_at"] == 0.30
    assert all("min_float=0.3" in u or "min_float=0.3" not in u for u in asked)
    assert not any("min_float=0.15" in u for u in asked), \
        "the stored bands were not read again"
    db.close()


def test_the_listings_sweep_leaves_from_one_address():
    """A dozen requests inside a minute is a burst, and letting the pool hop
    between addresses inside it is what CSFloat's "too many requests from too
    many IPs" check counts. The order sweep has been pinned since that
    complaint first appeared; this half never was, so every listings sweep
    went on showing the account a fresh address per band."""
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    pinned = []
    real_pin, real_unpin = col.client.pool.pin, col.client.pool.unpin
    col.client.pool.pin = lambda *a, **k: (pinned.append("pin"), real_pin(*a, **k))[1]
    col.client.pool.unpin = lambda: (pinned.append("unpin"), real_unpin())[1]
    col.client.fetch_json = lambda url, headers=None: {"data": []}

    col.sweep_listing_depth(name, item_id)
    assert pinned and pinned[0] == "pin", "pinned before the first band"
    assert pinned[-1] == "unpin", "and released afterwards"
    db.close()


def test_the_pin_is_released_even_when_a_band_blows_up():
    """A pin nobody releases wedges every later poll onto one address."""
    import logging
    import os
    import tempfile

    import pytest

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    def explode(url, headers=None):
        raise KeyboardInterrupt("something the sweep does not catch")

    col.client.fetch_json = explode
    with pytest.raises(KeyboardInterrupt):
        col.sweep_listing_depth(name, item_id)
    assert col.client.pool._pinned is None
    db.close()


def _both_sides_collector():
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    col.client.fetch_latest_sales = lambda n: []    # no network in a test
    order = []
    col.sweep_buy_orders = lambda n, i: (order.append("book"), {"bands": 1})[1]
    col.sweep_listing_depth = lambda n, i: (order.append("listings"),
                                            {"bands": 1})[1]
    return col, db, name, item_id, order


def test_the_half_that_is_missing_is_read_first():
    """The two halves run one after the other on one client and one account,
    so whichever goes second pays for what the first spent and takes the
    limit. With the book hard-coded first, the listings starved every time -
    which is why the books are current across the database and the listings
    are not."""
    col, db, name, item_id, order = _both_sides_collector()
    db.replace_buy_orders(item_id, [
        {"price": 50.0, "qty": 1, "float_min": 0.15, "float_max": 0.19}])

    col.sweep_both_sides(name, item_id)
    assert order == ["listings", "book"], "the side never read goes first"
    db.close()


def test_the_book_goes_first_when_it_is_the_staler_half():
    col, db, name, item_id, order = _both_sides_collector()
    from src.depth import depth_profile

    db.record_listing_depth(item_id, depth_profile(
        [{"id": "L1", "price": 52.0, "float": 0.165, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.38)))
    # No book at all: that is the half the pricing is guessing about.
    col.sweep_both_sides(name, item_id)
    assert order == ["book", "listings"]
    db.close()


def test_neither_half_is_lost_when_the_other_fails():
    col, db, name, item_id, order = _both_sides_collector()
    col.sweep_listing_depth = lambda n, i: (order.append("listings"),
                                            _raise())[1]

    def _raise():
        raise RuntimeError("limit")

    out = col.sweep_both_sides(name, item_id)
    assert "book" in order
    assert out["orders"] is not None
    assert "limit" in (out["depth"] or {}).get("error", "")
    db.close()


def test_a_dropped_connection_costs_a_retry_not_a_band():
    """A proxy that closes the connection without answering is not a refusal:
    nothing was counted against the quota and the address is not blocked. The
    band was simply lost, leaving a gap nothing later fills - one of nineteen
    on a live sweep, and one of twelve on the one beside it."""
    import logging
    import os
    import tempfile

    import requests

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    calls = []

    def flaky(url, headers=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("Remote end closed connection")
        return {"data": [{"id": "L1", "price": 16000, "type": "buy_now",
                          "min_offer_price": None,
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.16}}]}

    col.client.fetch_json = flaky
    result = col.sweep_listing_depth(name, item_id)
    assert result["failed_bands"] == 0, "the drop was retried, not counted"
    assert result["bands"] == 12, "every band of the wear range answered"
    db.close()


def test_a_refusal_is_not_retried():
    """A 429 or a credential refusal answers the same however often it is
    asked, and asking again is what draws the complaint."""
    import logging

    import pytest

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient, RateLimited
    from src.db import Database
    import os
    import tempfile

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    calls = []

    def refused(url, headers=None):
        calls.append(url)
        raise RateLimited("429")

    col.client.fetch_json = refused
    with pytest.raises(RateLimited):
        col._fetch_band("https://csfloat.com/x")
    assert len(calls) == 1
    db.close()


def test_a_retry_does_not_re_read_the_bands_it_just_finished():
    """A sweep stopped by a limit gets pressed again, and re-reading what it
    had just got through spends the quota that ran out in the first place."""
    import logging
    import os
    import tempfile

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database
    from src.depth import depth_profile

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    # The first attempt got through the two lowest bands a moment ago.
    db.record_listing_depth(item_id, depth_profile(
        [{"id": "L1", "price": 52.0, "float": 0.16, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None},
         {"id": "L2", "price": 53.0, "float": 0.18, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.19)))

    asked = []
    col.client.fetch_json = lambda url, headers=None: (
        asked.append(url), {"data": []})[1]
    result = col.sweep_listing_depth(name, item_id)

    assert result["reused"] == 2, "the two stored bands were not asked for again"
    assert not any("min_float=0.15" in u for u in asked)
    assert not any("min_float=0.17" in u for u in asked)
    assert any("min_float=0.19" in u for u in asked), "the rest was read"
    db.close()


def test_a_band_read_long_ago_is_read_again():
    """Not a freshness policy: a deliberate refresh past the window has to
    mean it, or "обойти" stops refreshing anything."""
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
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    db.conn.execute(
        "INSERT INTO listing_depth (item_id, fetched_at, float_min, float_max,"
        " listings, cheapest, asks) VALUES (?,?,?,?,?,?,?)",
        (item_id, "2026-09-01T00:00:00+00:00", 0.15, 0.17, 2, 52.0,
         '[[52.0, 0.16]]'))
    db.conn.commit()
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    assert col._recent_bands(item_id) == set()
    db.close()


def test_stale_sales_are_read_again_before_the_books():
    """The ceiling is priced off recent sales, and a quiet item's next
    scheduled poll can be days away. One request first, then the books."""
    col, db, name, item_id = _ready()
    order = []
    col.client.fetch_latest_sales = lambda n: order.append("sales") or []
    col.sweep_buy_orders = lambda n, i: order.append("orders")
    col.sweep_listing_depth = lambda n, i: order.append("depth")
    col.sweep_both_sides(name, item_id)
    assert order[0] == "sales" and sorted(order[1:]) == ["depth", "orders"]


def test_fresh_sales_are_not_read_twice():
    col, db, name, item_id = _ready()
    db.log_poll(item_id=item_id, market_hash_name=name, fetched_count=40,
                new_count=3, overlap_count=37, status="ok")
    asked = []
    col.client.fetch_latest_sales = lambda n: asked.append(n) or []
    col.sweep_buy_orders = lambda n, i: None
    col.sweep_listing_depth = lambda n, i: None
    col.sweep_both_sides(name, item_id)
    assert asked == [], "polled a minute ago - the books are what is old"
