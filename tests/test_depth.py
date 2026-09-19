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
