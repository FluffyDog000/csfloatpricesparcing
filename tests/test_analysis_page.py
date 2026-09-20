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
    import re
    tags = re.findall(r'<script src="\{\{ asset\(\'([^\']+)\'\)', page)
    assert tags.index("common.js") < tags.index("analysis.js"), \
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


def test_every_button_reports_what_it_is_doing():
    """The page worked in silence: a button that fetches, computes and
    re-renders looks exactly like a broken one while it runs, which is how a
    missing script read as "добавить не работает"."""
    import pathlib

    js = pathlib.Path("static/analysis.js").read_text()

    # Each action is wrapped so the status line names it and the buttons lock.
    for handler in ("an-add", "an-sweep", "an-run", "an-clear", "p-save"):
        block = js.split(f'$("{handler}").onclick')[1][:400]
        assert "action(" in block, f"{handler} runs without saying so"

    assert "window.addEventListener(\"error\"" in js, \
        "a script error must surface, not look like a dead button"
    assert "buttons.forEach" in js, "actions lock the buttons while they run"


def test_an_item_with_no_history_is_explained_not_left_blank():
    """A freshly added item has no sales, so every band is "мало данных" and
    the report looks broken unless the page says why."""
    name = "★ Driver Gloves | Snow Leopard (Field-Tested)"
    c = _app([name])
    c.post("/api/analysis/items", json={"market_hash_name": name})
    it = c.get("/api/analysis").get_json()["items"][0]

    assert it["sales"] == 0 and it["orders"] == 0
    assert len(it["bands"]) == 12, "bands still reported, each with its reason"
    assert all("мало данных" in b["reason"] for b in it["bands"])

    import pathlib
    js = pathlib.Path("static/analysis.js").read_text()
    assert "Продаж в базе нет" in js, "the empty case has its own message"
    assert "Стакан не собран" in js


def test_an_expired_session_is_named_not_left_as_a_bare_401():
    """Restarting the web service mints a new signing key when
    FLASK_SECRET_KEY is unset, so every session dies while the open page still
    looks logged in - and its API calls return "authentication required",
    which explains nothing to the person reading it."""
    import pathlib

    common = pathlib.Path("static/common.js").read_text()
    assert "401" in common and "сессия истекла" in common
    assert "FLASK_SECRET_KEY" in common, "and names the setting that prevents it"

    base = pathlib.Path("templates/base.html").read_text()
    assert "session_persistent" in base, "a missing key is warned about in the UI"


def test_diag_reports_whether_sessions_survive_a_restart():
    c = _app()
    d = c.get("/api/diag").get_json()
    assert "session_persistent" in d and "auth_enabled" in d
    assert "commit" in d and "build" in d


def test_the_page_can_tell_that_its_own_script_is_stale():
    """Three rounds went on a page whose script was a different version than
    the server's, with nothing on screen able to say so. The bootstrap is
    inline, so it arrives with the markup and cannot go stale separately."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    page = c.get("/analysis").get_data(as_text=True)

    assert "ANALYSIS_BUILD" in page, "the page checks what the script announced"
    assert "не выполнился" in page, "and says so when the script never ran"
    assert "кэширует" in page, "naming the likely cause: a caching proxy"

    import pathlib
    js = pathlib.Path("static/analysis.js").read_text()
    assert "window.ANALYSIS_BUILD = BUILD" in js, \
        "the script announces its build before doing any work"


def test_the_table_shows_what_the_cheapest_leading_price_would_have_given():
    """Only the chosen bid was shown, so a price well over the book looked
    arbitrary and could not be checked. Both numbers belong side by side,
    with what the cheap one would have earned."""
    import pathlib

    js = pathlib.Path("static/analysis.js").read_text()
    assert "минимум" in js, "the cheapest leading price is a column"
    assert "entry_monthly" in js and "entry_lam" in js, \
        "and what it would have returned is on hover"

    from src.pricing import Params, plan
    rows = [{"price": p, "float_value": 0.28, "age_days": a} for p, a in
            [(117.0, 3.0), (118.0, 9.0), (119.0, 15.0)]]
    rows += [{"price": p, "float_value": 0.28, "age_days": float(i % 27)}
             for i, p in enumerate([121.0, 122.0] * 3)]
    rows += [{"price": 135.0, "float_value": 0.28, "age_days": float(i % 27)}
             for i in range(14)]
    orders = [{"price": 116.0, "qty": 1, "float_min": 0.27, "float_max": 0.29}]
    band = plan(rows, orders, (0.27, 0.29), params=Params(min_sample=5))[0]

    # The entry is measured even though it fails the filters - why it fails is
    # the answer to "why are we bidding over the book".
    assert band.entry_lam is not None and band.entry_lam < Params().min_lambda
    assert band.entry_t_buy > band.t_buy * 5, "leading cheap means waiting"
    assert band.entry_monthly < band.monthly


def _stocked(name="★ Specialist Gloves | Big Swell (Field-Tested)"):
    """A client with one item that has history and a swept book."""
    import datetime as dt
    import json

    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    now = dt.datetime.now(dt.timezone.utc)
    # A cheap tail the order would actually catch, under a market that sells
    # far higher - otherwise the band is bid past its ceiling and nothing is
    # planned, which is a different case from the one being tested here.
    rows = []
    for i in range(10):
        price = 168.0 + (i % 4)
        rows.append((f"lo{i}", item_id, name, int(price * 100), price, 0.16,
                     (now - dt.timedelta(days=i % 20)).isoformat()))
    for i in range(40):
        rows.append((f"hi{i}", item_id, name, 21000, 210.0, 0.16,
                     (now - dt.timedelta(days=i % 20)).isoformat()))
    db.conn.executemany(
        "INSERT INTO sales (sale_id,item_id,market_hash_name,price_cents,price,"
        "float_value,sold_at,sold_at_estimated,scraped_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        [(a, b, c_, d, e, f, g, g) for a, b, c_, d, e, f, g in rows])
    db.conn.commit()
    db.replace_buy_orders(item_id, [
        {"price": 170.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}])
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})
    return c, name


def test_a_bot_with_no_budget_plans_nothing():
    """The default is zero, so a fresh install cannot spend by accident."""
    c, _ = _stocked()
    plan = c.get("/api/analysis/plan").get_json()
    assert plan["limits"]["total_capital"] == 0.0
    assert plan["actions"] == []
    assert not plan["can_place"], "and the request itself is unconfigured"
    assert "не настроена" in plan["placement"]


def test_a_budget_turns_the_scored_bands_into_placements():
    c, name = _stocked()
    c.post("/api/analysis/params",
           json={"an_total_capital": "1000", "an_max_per_item": "2"})
    plan = c.get("/api/analysis/plan").get_json()

    places = [a for a in plan["actions"] if a["kind"] == "place"]
    assert places, "a funded bot plans the bands that passed"
    assert len(places) <= 2, "the per-item cap is honoured"
    assert all(a["price"] <= a["ceiling"] for a in places)
    assert sum(a["price"] for a in places) <= 1000.0


def test_limits_are_clamped_like_the_thresholds():
    c, _ = _stocked()
    r = c.post("/api/analysis/params",
               json={"an_total_capital": "-5", "an_max_orders": "9999"}).get_json()
    assert r["limits"]["total_capital"] == 0.0
    assert r["limits"]["max_orders"] == 1000
    assert len(r["rejected"]) == 2


def test_an_order_we_hold_is_reconciled_rather_than_duplicated():
    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "1000"})
    plan = c.get("/api/analysis/plan").get_json()
    first = [a for a in plan["actions"] if a["kind"] == "place"][0]

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.upsert_our_order(db.get_item_id(name), first["float_min"],
                        first["float_max"], first["price"], first["ceiling"],
                        state="live", remote_id="abc")
    db.close()

    again = c.get("/api/analysis/plan").get_json()["actions"]
    same = [a for a in again
            if a["float_min"] == first["float_min"]
            and a["float_max"] == first["float_max"]]
    assert len(same) == 1 and same[0]["kind"] == "keep", \
        "an order we already hold is not placed a second time"


def test_the_captured_request_is_saved_only_when_sent():
    """One endpoint came off the site; the rest follow its shape. The page
    offers them, but nothing is stored until it is saved deliberately."""
    c, _ = _stocked()
    got = c.get("/api/analysis/placement").get_json()

    assert not got["configured"], "an empty install has no request to send"
    assert got["confirmed"] == ["cancel_method", "cancel_path",
                                "create_method", "create_path",
                                "update_method", "update_path"], \
        "all three operations were captured from the browser"
    assert got["suggested"]["update_path"] == "/api/v1/buy-orders/{order_id}"

    saved = c.post("/api/analysis/placement",
                   json=got["suggested"]).get_json()
    assert saved["saved"]
    assert c.get("/api/analysis/placement").get_json()["configured"]


def test_a_body_that_would_fail_at_send_time_is_refused_at_save_time():
    """Finding out the template is wrong while holding a live order is the
    expensive moment to find out."""
    c, _ = _stocked()
    bad = c.post("/api/analysis/placement",
                 json={"create_path": "/x", "create_body": '{"p": {pennies}}'})
    assert bad.status_code == 400
    assert "pennies" in bad.get_json()["problems"][0]
    assert not c.get("/api/analysis/placement").get_json()["configured"]


def test_saving_a_new_request_disarms():
    """What was approved was the previous shape, not this one."""
    c, _ = _stocked()
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.set_setting("analysis_armed", "1")
    db.close()

    suggested = c.get("/api/analysis/placement").get_json()["suggested"]
    c.post("/api/analysis/placement", json=suggested)
    assert c.get("/api/analysis/plan").get_json()["armed"] is False


def test_the_plan_says_how_the_capital_would_be_split():
    """Six orders on one item are one bet in six pieces: they fill together
    when that market moves. The plan has to show that, not just the total."""
    c, name = _stocked()
    c.post("/api/analysis/params",
           json={"an_total_capital": "1000", "an_max_per_item": "3"})
    plan = c.get("/api/analysis/plan").get_json()

    assert plan["by_item"], "the split is reported, not only the sum"
    assert plan["concentration"] == 1.0, "one item holding everything is 100%"
    assert plan["planned_total"] == sum(plan["by_item"].values())

    import pathlib
    js = pathlib.Path("static/analysis.js").read_text()
    assert "концентрация" in js or "капитала" in js, \
        "and the page warns rather than leaving it to be noticed"


def test_the_binding_limit_is_visible_in_the_plan():
    c, _ = _stocked()
    c.post("/api/analysis/params",
           json={"an_total_capital": "1000", "an_max_per_item": "1"})
    plan = c.get("/api/analysis/plan").get_json()

    places = [a for a in plan["actions"] if a["kind"] == "place"]
    assert len(places) == 1, "the per-item cap binds"
    assert plan["limits"]["max_orders_per_item"] == 1, \
        "and the page is told which number did it"


def test_applying_requires_arming_and_spends_it():
    """Arming is separate from configuring and from planning, and one arming
    applies one plan: the next one has to be approved on its own."""
    c, _ = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "1000"})
    c.post("/api/analysis/placement",
           json=c.get("/api/analysis/placement").get_json()["suggested"])

    assert c.post("/api/analysis/apply", json={}).status_code == 403

    c.post("/api/analysis/arm", json={"armed": True})
    assert c.get("/api/analysis/plan").get_json()["armed"] is True
    body = c.post("/api/analysis/apply", json={}).get_json()
    assert body["queued"] >= 1 and body["dry_run"] is True

    plan = c.get("/api/analysis/plan").get_json()
    assert plan["armed"] is False, "arming is spent by use"
    assert plan["pending"] is True, "and the collector has the work"


def test_what_is_queued_is_what_was_shown():
    """A plan recomputed at apply time could differ from the one approved,
    and nothing on screen would say so."""
    import json as _json

    c, _ = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "1000"})
    c.post("/api/analysis/placement",
           json=c.get("/api/analysis/placement").get_json()["suggested"])
    shown = [a for a in c.get("/api/analysis/plan").get_json()["actions"]
             if a["kind"] != "keep"]

    c.post("/api/analysis/arm", json={"armed": True})
    c.post("/api/analysis/apply", json={})

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    queued = _json.loads(db.get_setting("analysis_pending_actions"))["actions"]
    db.close()
    assert [(a["float_min"], a["price"]) for a in queued] == \
           [(a["float_min"], a["price"]) for a in shown]


def test_arming_cannot_outlive_a_change_to_the_request():
    c, _ = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "1000"})
    suggested = c.get("/api/analysis/placement").get_json()["suggested"]
    c.post("/api/analysis/placement", json=suggested)
    c.post("/api/analysis/arm", json={"armed": True})

    c.post("/api/analysis/placement", json=dict(suggested, create_path="/other"))
    assert c.get("/api/analysis/plan").get_json()["armed"] is False
    assert c.post("/api/analysis/apply", json={}).status_code == 403


def test_a_real_run_has_to_be_asked_for_twice():
    """Dry run is the default, and the page confirms before a live one."""
    import pathlib

    c, _ = _stocked()
    assert c.get("/api/analysis/plan").get_json()["dry_run"] is True

    r = c.post("/api/analysis/arm",
               json={"armed": True, "dry_run": False}).get_json()
    assert r["dry_run"] is False

    js = pathlib.Path("static/analysis.js").read_text()
    assert "потратит деньги" in js, "a live apply is confirmed, not assumed"


def test_a_saved_body_is_shown_as_it_would_be_sent():
    """Updating the code does not update what was saved, and only the saved
    shape is what goes out. The rendered request is on the page so the two can
    be told apart without spending a request to find out."""
    import json as _json

    c, _ = _stocked()
    stale = {
        "create_method": "POST", "create_path": "/api/v1/buy-orders",
        "create_body": '{"market_hash_name": "{name}", "price": {price_cents}}',
        "update_method": "PATCH", "update_path": "/api/v1/buy-orders/{order_id}",
        "update_body": '{"price": {price_cents}}',
    }
    c.post("/api/analysis/placement", json=stale)
    got = c.get("/api/analysis/placement").get_json()

    assert got["preview"]["создание"]["price"] == 4900, \
        "what would be sent, rendered from what is stored"
    assert any("max_price" in w for w in got["warnings"]), \
        "and the field CSFloat refused is named"

    # Saving the corrected shape clears it.
    c.post("/api/analysis/placement",
           json=c.get("/api/analysis/placement").get_json()["suggested"])
    fixed = c.get("/api/analysis/placement").get_json()
    assert fixed["warnings"] == []
    assert fixed["preview"]["создание"]["max_price"] == 4900


def test_a_body_that_cannot_render_shows_the_reason_not_a_blank():
    c, _ = _stocked()
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.set_setting("placement_spec", json.dumps({
        "create_path": "/x", "create_body": '{"p": {pennies}}'}))
    db.close()

    got = c.get("/api/analysis/placement").get_json()
    assert "ошибка" in got["preview"]["создание"]
    assert "pennies" in got["preview"]["создание"]["ошибка"]


def test_the_calculator_is_gone_entirely():
    """Removed on request: it duplicated the analysis tab's arithmetic without
    knowing about the order book, so two places answered "is this worth it"
    and only one of them had the data."""
    import pathlib

    assert not pathlib.Path("templates/calc.html").exists()
    assert not pathlib.Path("static/calc.js").exists()
    assert "calc_page" not in pathlib.Path("webapp.py").read_text(encoding="utf-8")
    base = pathlib.Path("templates/base.html").read_text(encoding="utf-8")
    assert "Калькулятор" not in base, "a nav link to a route that is gone"
