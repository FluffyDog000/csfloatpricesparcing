"""The analysis tab: add items, sweep their books, see what would be bid.

The web process must not talk to CSFloat - the collector owns the proxies and
the rate limits - so pressing "sweep" queues the work and the page waits for
it, the same contract the per-item button already uses.
"""
import json
import os
import tempfile


def _app(tmp_items=()):
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    import importlib

    import webapp
    importlib.reload(webapp)
    webapp.app.config["TESTING"] = True
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    for name in tmp_items:
        db.add_item(name)
    db.close()
    return webapp.app.test_client()


def test_the_tab_renders_and_starts_empty():
    c = _app()
    assert c.get("/analysis").status_code == 200
    body = c.get("/api/analysis").get_json()
    assert body["items"] == []
    assert body["params"]["fee"] > 0, "the thresholds are shown, not hidden"


def test_an_untracked_item_is_refused_rather_than_silently_added():
    """Adding a name we have no sales for would render an empty panel with no
    explanation; saying so at the point of typing is the honest moment."""
    c = _app()
    r = c.post("/api/analysis/items",
               json={"market_hash_name": "★ Nothing | Here (Field-Tested)"})
    assert r.status_code == 404
    assert c.get("/api/analysis").get_json()["items"] == []


def test_items_are_added_listed_and_removed():
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    assert c.post("/api/analysis/items",
                  json={"market_hash_name": name}).get_json()["items"] == [name]
    # Adding twice does not duplicate.
    assert c.post("/api/analysis/items",
                  json={"market_hash_name": name}).get_json()["items"] == [name]
    assert c.get("/api/analysis").get_json()["items"][0]["item"] == name
    assert c.post("/api/analysis/items",
                  json={"market_hash_name": name,
                        "action": "remove"}).get_json()["items"] == []


def test_the_sweep_button_queues_work_instead_of_fetching():
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    c = _app([name])
    c.post("/api/analysis/items", json={"market_hash_name": name})
    body = c.post("/api/analysis/sweep", json={}).get_json()
    assert body["queued"] == [name]

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    pending = [r["market_hash_name"] for r in db.pending_order_requests()]
    assert name in pending, "the collector picks the sweep up, not the web app"
    db.close()


def test_a_band_report_names_the_bid_and_the_reasons():
    import datetime as dt

    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    # The low band trades either side of the book, so a bid above the top of
    # it reaches real sellers; the high band is bid past what it resells for.
    for i in range(12):
        for price in (170.0 + i * 2, 215.0 + i * 2):
            rows.append((f"lo{i}-{price}", item_id, name, int(price * 100), price,
                         0.16, (now - dt.timedelta(days=i % 21)).isoformat()))
    for i in range(24):
        rows.append((f"hi{i}", item_id, name, 10000, 100.0 + (i % 6), 0.36,
                     (now - dt.timedelta(days=i % 21)).isoformat()))
    db.conn.executemany(
        "INSERT INTO sales (sale_id,item_id,market_hash_name,price_cents,price,"
        "float_value,sold_at,sold_at_estimated,scraped_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        [(a, b, c_, d, e, f, g, g) for a, b, c_, d, e, f, g in rows])
    db.conn.commit()
    db.replace_buy_orders(item_id, [
        {"price": 175.0, "qty": 1, "float_min": 0.15, "float_max": 0.17},
        {"price": 150.0, "qty": 1, "float_min": 0.35, "float_max": 0.38},
    ])
    db.close()

    c.post("/api/analysis/items", json={"market_hash_name": name})
    it = c.get("/api/analysis").get_json()["items"][0]
    bands = {round(b["float_min"], 2): b for b in it["bands"]}

    low = bands[0.15]
    assert low["take"] and low["bid"] > low["top"], "we have to be first to fill"
    assert low["bid"] <= low["ceiling"] and low["wars"] >= 2
    assert it["capital"] >= low["bid"]

    high = bands[0.35]
    assert not high["take"], "bid at $150 against a $100 market is a loss"
    assert "выше потолка" in high["reason"]

    # Every band comes back, so "why not this one" is answerable from the page.
    assert len(it["bands"]) == 12


def test_thresholds_round_trip_and_change_the_verdict():
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    r = c.post("/api/analysis/params", json={"an_min_margin": "0.25"})
    assert r.get_json()["params"]["min_margin"] == 0.25
    assert c.get("/api/analysis").get_json()["params"]["min_margin"] == 0.25


def test_the_page_loads_the_shared_helpers_it_calls():
    """It shipped without common.js and every button died on "getJSON is not
    defined" — the page renders fine, so nothing else catches this."""
    import pathlib

    page = pathlib.Path("templates/analysis.html").read_text()
    script = pathlib.Path("static/analysis.js").read_text()
    for helper in ("getJSON", "postJSON"):
        if helper in script:
            assert "common.js" in page, f"{helper} lives in common.js"
    assert page.index("common.js") < page.index("analysis.js"), \
        "helpers have to be defined before the page script runs"


def test_a_threshold_out_of_range_is_pulled_back_and_reported():
    """A band step of 0.5 spans a whole wear and a flow of 3/day passes
    nothing; either returns an empty report that reads as "no opportunities"
    rather than "your threshold did that"."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    r = c.post("/api/analysis/params",
               json={"an_step": "0.5", "an_min_lambda": "3"}).get_json()

    assert r["params"]["band_step"] == 0.23, "clamped to the widest wear"
    assert r["params"]["min_lambda"] == 3.0, "3/day is steep but not absurd"
    assert any("an_step" in m for m in r["rejected"]), "and the page is told"


def test_a_threshold_that_is_not_a_number_is_refused_not_stored():
    """"0.15-038" typed into the band-step box: it reads like a float range,
    which is exactly how the label was misread."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    before = c.get("/api/analysis").get_json()["params"]["band_step"]
    r = c.post("/api/analysis/params", json={"an_step": "0.15-038"}).get_json()

    assert r["params"]["band_step"] == before, "the old value survives"
    assert any("не число" in m for m in r["rejected"])


def test_a_comma_decimal_is_accepted():
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    r = c.post("/api/analysis/params", json={"an_step": "0,03"}).get_json()
    assert r["params"]["band_step"] == 0.03
