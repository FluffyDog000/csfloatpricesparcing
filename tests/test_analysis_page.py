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
    c = _many_items([name])       # with history, so the screen lets it through
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
    # Floats sit inside the hundredth each order stops at, not on its edge: a
    # sale sitting exactly on 0.16 belongs to the order that stops there, and
    # that order claims it before the wider one is even asked.
    for i in range(12):
        for price in (170.0 + i * 2, 215.0 + i * 2):
            rows.append((f"lo{i}-{price}", item_id, name, int(price * 100), price,
                         0.165 + (i % 5) * 0.001,
                         (now - dt.timedelta(days=i % 21)).isoformat()))
    for i in range(24):
        rows.append((f"hi{i}", item_id, name, 10000, 100.0 + (i % 6),
                     0.355 + (i % 5) * 0.001,
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
    # Every order runs from the wear minimum and is named by where it stops,
    # so the top is what tells them apart.
    rungs = {round(b["float_max"], 2): b for b in it["bands"]}
    assert all(b["float_min"] == 0.15 for b in it["bands"])

    low = rungs[0.17]
    assert low["take"] and low["bid"] > low["top"], "we have to be first to fill"
    assert low["bid"] <= low["ceiling"], "the ceiling is break-even, above the bid"
    assert it["capital"] >= low["bid"]

    high = rungs[0.36]
    assert not high["take"], "bid at $150 against a $100 market is a loss"
    assert high["reason"], "and it says why"

    # Every rung comes back, so "why not this one" is answerable from the page.
    assert len(it["bands"]) == 23, "0.16 through 0.38 at a hundredth a step"


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
    """A history window of three years is not a window; silently honouring it
    returns a report that reads as "no opportunities" rather than "your
    threshold did that"."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    r = c.post("/api/analysis/params",
               json={"an_window": "999", "an_min_margin": "0.2"}).get_json()

    assert r["params"]["window_days"] == 365.0, "clamped to a year"
    assert r["params"]["min_margin"] == 0.2, "20% is steep but not absurd"
    assert any("an_window" in m for m in r["rejected"]), "and the page is told"


def test_a_threshold_that_is_not_a_number_is_refused_not_stored():
    """"0.15-038" typed into a numeric box: it reads like a float range, which
    is exactly how one of these labels was once misread."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    before = c.get("/api/analysis").get_json()["params"]["window_days"]
    r = c.post("/api/analysis/params", json={"an_window": "0.15-038"}).get_json()

    assert r["params"]["window_days"] == before, "the old value survives"
    assert any("не число" in m for m in r["rejected"])


def test_a_comma_decimal_is_accepted():
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    r = c.post("/api/analysis/params", json={"an_min_margin": "0,07"}).get_json()
    assert r["params"]["min_margin"] == 0.07


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
    """A freshly added item has no sales. Twelve rows of "мало данных: 0
    продаж" is not an explanation - the screen says it once, before the bands
    are computed at all."""
    name = "★ Driver Gloves | Snow Leopard (Field-Tested)"
    c = _app([name])
    c.post("/api/analysis/items", json={"market_hash_name": name})
    it = c.get("/api/analysis").get_json()["items"][0]

    assert it["sales"] == 0 and it["orders"] == 0
    assert it["bands"] == []
    assert "нет истории продаж" in it["screened_out"]

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
           json={"an_total_capital": "2000", "an_max_per_item": "2"})
    plan = c.get("/api/analysis/plan").get_json()

    places = [a for a in plan["actions"] if a["kind"] == "place"]
    assert places, "a funded bot plans the bands that passed"
    assert len(places) <= 2, "the per-item cap is honoured"
    assert all(a["price"] <= a["ceiling"] for a in places)
    assert sum(a["price"] for a in places) <= 2000.0


def test_limits_are_clamped_like_the_thresholds():
    c, _ = _stocked()
    r = c.post("/api/analysis/params",
               json={"an_total_capital": "-5", "an_max_orders": "9999"}).get_json()
    assert r["limits"]["total_capital"] == 0.0
    assert r["limits"]["max_orders"] == 1000
    assert len(r["rejected"]) == 2


def test_an_order_we_hold_is_reconciled_rather_than_duplicated():
    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
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
                                "create_body", "create_method", "create_path",
                                "update_body", "update_method",
                                "update_path"], \
        "all three operations, bodies included, came off the browser"
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
           json={"an_total_capital": "2000", "an_max_per_item": "3"})
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
           json={"an_total_capital": "2000", "an_max_per_item": "1"})
    plan = c.get("/api/analysis/plan").get_json()

    places = [a for a in plan["actions"] if a["kind"] == "place"]
    assert len(places) == 1, "the per-item cap binds"
    assert plan["limits"]["max_orders_per_item"] == 1, \
        "and the page is told which number did it"


def test_applying_requires_arming_and_spends_it():
    """Arming is separate from configuring and from planning, and one arming
    applies one plan: the next one has to be approved on its own."""
    c, _ = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
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
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
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
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
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


def test_the_picker_replaces_the_whole_list_in_one_call():
    """A dialog of tick boxes has one answer at the end. Sending it as a
    stream of adds and removes would leave the list half changed if one of
    them failed - and half a list still gets orders placed against it."""
    c = _app(["A (FT)", "B (FT)", "C (FT)"])
    c.post("/api/analysis/items", json={"market_hash_name": "A (FT)"})

    got = c.post("/api/analysis/items",
                 json={"action": "set", "names": ["B (FT)", "C (FT)"]})
    assert got.status_code == 200
    assert got.get_json()["items"] == ["B (FT)", "C (FT)"], "A was unticked"


def test_the_picker_reports_names_it_does_not_track_rather_than_dropping_them():
    c = _app(["A (FT)"])
    got = c.post("/api/analysis/items",
                 json={"action": "set", "names": ["A (FT)", "ghost"]})
    body = got.get_json()
    assert body["items"] == ["A (FT)"]
    assert body["unknown"] == ["ghost"], "silently shorter is how a list lies"


def test_the_picker_keeps_the_order_ticked_and_drops_repeats():
    c = _app(["A (FT)", "B (FT)"])
    got = c.post("/api/analysis/items",
                 json={"action": "set",
                       "names": ["B (FT)", "A (FT)", "B (FT)", "  "]})
    assert got.get_json()["items"] == ["B (FT)", "A (FT)"]


def test_the_journal_tab_renders_and_reports_nothing_having_happened():
    c = _app()
    assert c.get("/journal").status_code == 200
    body = c.get("/api/analysis/journal").get_json()
    assert body["events"] == [] and body["held"] == 0


def test_the_journal_serves_newest_first_and_filters():
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    db.record_order_event(name=name, kind="place", ok=True, dry=False,
                          source="plan", item_id=item_id, price=150.0,
                          float_min=0.15, float_max=0.17, reason="проба")
    db.record_order_event(name=name, kind="raise", ok=True, dry=False,
                          source="defence", item_id=item_id, price=151.0,
                          was=150.0, float_min=0.15, float_max=0.17,
                          reason="перебили")
    db.record_order_event(name="B (FT)", kind="place", ok=False, dry=True,
                          source="plan", price=10.0, reason="проба")
    db.close()

    body = c.get("/api/analysis/journal").get_json()
    # Newest first: three events written in one second tie on the timestamp,
    # so the id breaks the tie rather than leaving the order to chance.
    assert [e["id"] for e in body["events"]] == sorted(
        (e["id"] for e in body["events"]), reverse=True)
    assert body["events"][0]["market_hash_name"] == "B (FT)"
    assert len(body["events"]) == 3
    assert set(body["items"]) == {name, "B (FT)"}

    only_real = c.get("/api/analysis/journal?dry=0").get_json()["events"]
    assert len(only_real) == 2, "a rehearsal is not a position"

    one = c.get(f"/api/analysis/journal?item={name}").get_json()["events"]
    assert len(one) == 2 and all(e["market_hash_name"] == name for e in one)

    recent = c.get("/api/analysis/journal?hours=1").get_json()
    assert len(recent["events"]) == 3 and recent["since"]
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.conn.execute("UPDATE order_events SET at = '2020-01-01T00:00:00+00:00' "
                    "WHERE market_hash_name = 'B (FT)'")
    db.conn.commit()
    db.close()
    recent = c.get("/api/analysis/journal?hours=24").get_json()["events"]
    assert {e["market_hash_name"] for e in recent} == {name}, \
        "a period leaves out what happened before it"


def test_the_journal_never_confuses_a_rehearsal_with_a_placement():
    """A dry run logged like a live one reads as money committed."""
    c = _app(["A (FT)"])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.record_order_event(name="A (FT)", kind="place", ok=True, dry=True,
                          source="plan", price=10.0, reason="проба")
    db.close()
    ev = c.get("/api/analysis/journal").get_json()["events"][0]
    assert ev["dry"] is True and ev["ok"] is True


def _many_items(names, sales_per_item=60, same_price=False):
    """Items with enough history for the scorer to have an opinion."""
    import datetime as dt

    c = _app(names)
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    now = dt.datetime.now(dt.timezone.utc)
    for idx, name in enumerate(names):
        item_id = db.get_item_id(name)
        rows = []
        # `same_price` puts every item on one price tag. Making later ones
        # dearer stands in for "better" in most of these tests, but the
        # allowance is spread by return per DOLLAR, so a cheap item with the
        # same profit beats a dear one - and a ranking test built that way
        # would be measuring the price tag. There, the competition below is
        # the only thing separating them.
        base = 150.0 if same_price else 100.0 + idx * 50
        for i in range(sales_per_item):
            # Spread around the base, so a bid under the median still catches
            # some of the flow - otherwise every band fails on "no flow" and
            # the test passes without ever ranking anything.
            price = round(base * (0.80 + (i % 10) / 25.0), 2)
            rows.append((f"{idx}-{i}", item_id, name, int(price * 100), price,
                         0.36, (now - dt.timedelta(days=i % 14)).isoformat()))
        # Not OR IGNORE: scraped_at is NOT NULL with no default, and a silent
        # skip here leaves every band reading "мало данных" while the test
        # passes for the wrong reason.
        db.conn.executemany(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, "
            "price_cents, price, float_value, sold_at, sold_at_estimated, "
            "scraped_at) VALUES (?,?,?,?,?,?,?,0,?)",
            [r + (r[-1],) for r in rows])
        # The competition thins out down the list, so later items really are
        # the better trade rather than merely the later one. Without this the
        # returns tie and "the best won" proves only the sort order.
        db.replace_buy_orders(item_id, [
            {"price": round(base * (0.86 - idx * 0.07), 2), "qty": 1,
             "float_min": 0.35, "float_max": 0.38}])
        db.conn.commit()
        c.post("/api/analysis/items", json={"market_hash_name": name})
    db.close()
    return c


def test_the_budget_is_spread_by_return_not_by_the_order_items_were_added():
    """The bug three hundred items exposes. At twenty orders and three per
    item, the first seven names took everything and the rest were scored for
    nothing.

    Each item's numbers are tied to its NAME, not to where it sits in the
    list, so the list can be reversed and the same item must still win. The
    shared fixture cannot show this: it derives an item's worth from its
    index, which is its position, so reversing changes the data too.
    """
    import datetime as dt

    # (name, rival bid). Same sales for all three, so what separates them is
    # the competition alone - and the allowance is spread per dollar, which
    # a price gradient would drown out.
    # Rivals all sit under the margin ceiling, so what separates the items is
    # how much of the cheap tail each bid reaches - not who gets refused.
    market = [("A Weak | Rivals (Field-Tested)", 88.0),
              ("B Some | Rivals (Field-Tested)", 91.0),
              ("C Heavy | Rivals (Field-Tested)", 94.0)]
    rivals = dict(market)
    names = [n for n, _ in market]

    winners = []
    for order in (names, list(reversed(names))):
        c = _app(order)
        import webapp
        db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
        now = dt.datetime.now(dt.timezone.utc)
        for name in order:
            item_id = db.get_item_id(name)
            rows = [(f"{name}-{i}", item_id, name, int((88.0 + i % 32) * 100),
                     88.0 + i % 32, 0.365 + (i % 5) * 0.001,
                     (now - dt.timedelta(days=i % 14)).isoformat())
                    for i in range(60)]
            db.conn.executemany(
                "INSERT INTO sales (sale_id, item_id, market_hash_name, "
                "price_cents, price, float_value, sold_at, sold_at_estimated, "
                "scraped_at) VALUES (?,?,?,?,?,?,?,0,?)",
                [r + (r[-1],) for r in rows])
            db.replace_buy_orders(item_id, [
                {"price": rivals[name], "qty": 1,
                 "float_min": 0.35, "float_max": 0.38}])
            db.conn.commit()
            c.post("/api/analysis/items", json={"market_hash_name": name})
        db.close()

        assert c.post("/api/analysis/params",
                      json={"an_total_capital": "2000", "an_max_orders": "1",
                            "an_max_per_item": "1"}).status_code == 200

        scored = c.get("/api/analysis").get_json()["items"]
        best = {i["item"]: max([b["rank"] for b in i["bands"] if b["take"]]
                               or [0.0])
                for i in scored}
        assert len(set(best.values())) > 1, "the items must differ to rank them"

        places = [a for a in c.get("/api/analysis/plan").get_json()["actions"]
                  if a["kind"] == "place"]
        assert len(places) == 1, places
        won = places[0]["item"]
        assert best[won] == max(best.values()), \
            f"{won} won on {best[won]:.4f} while {max(best.values()):.4f} existed"
        winners.append(won)

    assert winners[0] == winners[1], \
        f"the winner moved with the list order: {winners}"


def test_the_sweep_skips_what_the_history_already_rules_out():
    """One book costs about six requests through the cookie and the
    residential route. Three hundred items is over an hour of asking on the
    path that once drew "too many requests from too many IPs" - so an item the
    free pass already rejected must not buy a place in that queue."""
    good = "A Good | One (Field-Tested)"
    c = _many_items([good])
    c.post("/api/analysis/items",
           json={"market_hash_name": "★ Nothing | Known (Field-Tested)"})

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.add_item("★ Nothing | Known (Field-Tested)")
    db.close()
    c.post("/api/analysis/items",
           json={"market_hash_name": "★ Nothing | Known (Field-Tested)"})

    body = c.post("/api/analysis/sweep", json={}).get_json()
    assert body["queued"] == [good]
    assert [s["item"] for s in body["skipped"]] == \
        ["★ Nothing | Known (Field-Tested)"]
    assert "нет истории продаж" in body["skipped"][0]["reason"]
    assert "запросов" in body["note"], "the saving is worth saying out loud"


def test_a_price_ceiling_keeps_expensive_items_out_of_the_queue():
    """"Не берёт предмет от определённой суммы" - and it costs nothing to
    decide, so it happens before the sweep rather than after it."""
    c = _many_items(["A Cheap | One (Field-Tested)",
                     "B Middling | Two (Field-Tested)",
                     "C Rich | Three (Field-Tested)"])
    # The helper prices them at roughly 100, 150 and 200.
    assert c.post("/api/analysis/params",
                  json={"scr_max_price": "160"}).status_code == 200

    body = c.post("/api/analysis/sweep", json={}).get_json()
    assert "C Rich | Three (Field-Tested)" not in body["queued"]
    assert len(body["queued"]) == 2
    assert "дороже" in body["skipped"][0]["reason"]


def test_every_settings_group_is_actually_saved():
    """The save endpoint loops over the groups it knows about. A group left
    out is a field the page shows, accepts, echoes back - and never stores."""
    from src.settings import (LIMIT_BOUNDS, LIMIT_KEYS, PARAM_BOUNDS,
                              PARAM_KEYS, SCREEN_BOUNDS, SCREEN_KEYS)

    c = _app()
    sent = {}
    for keys, bounds in ((PARAM_KEYS, PARAM_BOUNDS), (LIMIT_KEYS, LIMIT_BOUNDS),
                         (SCREEN_KEYS, SCREEN_BOUNDS)):
        for key, attr, cast in keys:
            lo, hi = bounds[attr]
            sent[key] = str(cast(lo + (min(hi, lo + 10.0) - lo) / 2))
    assert c.post("/api/analysis/params", json=sent).get_json()["rejected"] == []

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    missing = [k for k in sent if db.get_setting(k) in (None, "")]
    db.close()
    assert missing == [], f"accepted and dropped: {missing}"




def test_an_amend_body_without_float_bounds_is_called_out():
    """The captured PATCH carries the order's whole shape. If CSFloat reads it
    as a replacement, amending a price with a body that omits the bounds
    clears the float filter - and an order buying 0.15-0.17 starts buying
    anything. A spec saved before this was learned still has that body."""
    import json as _json

    c = _app()
    c.post("/api/analysis/placement",
           json={"create_path": "/api/v1/buy-orders",
                 "create_body": '{"max_price": {price_cents}}',
                 "update_path": "/api/v1/buy-orders/{order_id}",
                 "update_body": '{"max_price": {price_cents}}',
                 "cancel_path": "/api/v1/buy-orders/{order_id}"})

    warnings = c.get("/api/analysis/placement").get_json()["warnings"]
    assert any("границы float" in w for w in warnings), warnings
    assert any("скупать любой износ" in w for w in warnings)


def test_the_suggested_amend_body_raises_no_such_warning():
    import json as _json

    from src.placement import SUGGESTED

    c = _app()
    c.post("/api/analysis/placement", json=SUGGESTED.as_dict())
    warnings = c.get("/api/analysis/placement").get_json()["warnings"]
    assert not any("границы float" in w for w in warnings), warnings


def test_the_plan_is_listed_best_first_across_every_item():
    """Grouped by item, the page listed orders in the sequence the names were
    typed in and said nothing about which to place first - which is the whole
    job of the rank. Cancels and raises stay on top: they free the capacity
    the places then spend."""
    import datetime as dt

    # Worth falls as the float rises; each item stops at a different rival, so
    # neither item is better than the other at every rung.
    market = {"A Tight | Book (Field-Tested)": 92.0,
              "B Loose | Book (Field-Tested)": 86.0}
    spread = (-12.0, -8.0, -4.0, 0.0, 4.0, 8.0, 12.0, 16.0)
    c = _app(list(market))
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    now = dt.datetime.now(dt.timezone.utc)
    for name, rival in market.items():
        item_id = db.get_item_id(name)
        rows = []
        for k, f in enumerate((0.155, 0.165, 0.175, 0.185)):
            worth = 104.0 - 3.0 * k
            for i in range(16):
                price = worth + spread[i % len(spread)]
                rows.append((f"{name}-{f}-{i}", item_id, name, int(price * 100),
                             price, f,
                             (now - dt.timedelta(days=(i % 13) + 0.5)).isoformat()))
        db.conn.executemany(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, "
            "price_cents, price, float_value, sold_at, sold_at_estimated, "
            "scraped_at) VALUES (?,?,?,?,?,?,?,0,?)",
            [r + (r[-1],) for r in rows])
        db.replace_buy_orders(item_id, [
            {"price": rival, "qty": 1, "float_min": 0.15, "float_max": 0.19}])
        db.conn.commit()
        c.post("/api/analysis/items", json={"market_hash_name": name})
    db.close()
    c.post("/api/analysis/params",
           json={"an_total_capital": "5000", "an_max_per_item": "4"})

    actions = c.get("/api/analysis/plan").get_json()["actions"]
    places = [a for a in actions if a["kind"] == "place"]
    assert len(places) > 4, places

    scored = {i["item"]: {round(b["float_max"], 4): b["rank"]
                          for b in i["bands"] if b["take"]}
              for i in c.get("/api/analysis").get_json()["items"]}
    ranks = [scored[a["item"]][round(a["float_max"], 4)] for a in places]
    assert ranks == sorted(ranks, reverse=True), ranks

    # And not merely all of one item and then all of the other, which sorting
    # by name alone would also satisfy.
    listed = [a["item"][0] for a in places]
    assert len(set(listed)) > 1 and listed != sorted(listed), listed


def test_a_placement_reports_the_room_it_has_to_answer_an_outbid():
    """"запас None перебив." shipped on every row: the field it read belonged
    to a model that no longer exists."""
    from src.executor import Limits, reconcile
    from src.pricing import Band

    band = Band(float_min=0.15, float_max=0.17, bid=48.40, ceiling=48.90,
                margin=0.051, lam=0.125, take=True)
    act = reconcile("x", [band], [], [], Limits())[0]
    assert "None" not in act.reason
    assert "$0.50" in act.reason and "5 перебив" in act.reason
    assert "0.12/день" in act.reason


def test_the_plan_prints_the_rank_it_is_ordered_by():
    """Ordering by a number the page never shows leaves "why is this one
    first" unanswerable from the table - which is the question the order
    exists to answer."""
    import datetime as dt

    name = "AK-47 | Inheritance (Minimal Wear)"
    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    now = dt.datetime.now(dt.timezone.utc)
    spread = (-8.0, -4.0, 0.0, 4.0, 8.0, 12.0)
    rows = []
    for k, f in enumerate((0.075, 0.085, 0.095)):
        for i in range(16):
            price = (60.0 - 3.0 * k) + spread[i % len(spread)]
            rows.append((f"{name}{f}{i}", item_id, name, int(price * 100), price,
                         f, (now - dt.timedelta(days=(i % 13) + 0.5)).isoformat()))
    db.conn.executemany(
        "INSERT INTO sales (sale_id, item_id, market_hash_name, price_cents, "
        "price, float_value, sold_at, sold_at_estimated, scraped_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)", [r + (r[-1],) for r in rows])
    db.replace_buy_orders(item_id, [
        {"price": 46.0, "qty": 1, "float_min": 0.07, "float_max": 0.11}])
    db.conn.commit()
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})
    c.post("/api/analysis/params", json={"an_total_capital": "5000"})

    places = [a for a in c.get("/api/analysis/plan").get_json()["actions"]
              if a["kind"] == "place"]
    assert places, "the fixture has to produce something to rank"
    assert all("rank" in a for a in places)
    ranks = [a["rank"] for a in places]
    assert ranks == sorted(ranks, reverse=True)
    assert all(r > 0 for r in ranks), "a placed order has a rank by definition"


def test_the_rank_shown_is_the_rank_sorted_by():
    """Rounding it before the sort made two distinct ranks a tie, broken by
    item name - so the list stopped matching the column beside it."""
    import webapp

    source = open("static/analysis.js", encoding="utf-8").read()
    assert "a.rank.toFixed(4)" in source, "rounded where it is printed"
    plan = source.split("function renderPlan")[1] if "function renderPlan" in source \
        else source
    assert "a.rank" in plan
    assert "round(ranked.get(" not in open("webapp.py", encoding="utf-8").read(), \
        "and not rounded before the ordering"


def test_the_stale_script_warning_compares_like_with_like():
    """It compared a script's own `?v=` stamp against `asset_build`, which is
    neither the same file nor the same length: the newest mtime across all of
    static, cut to its last six digits for the header. The two could never be
    equal, so the page announced "браузер выполняет старый analysis.js" on
    every single load, while the script it named was current."""
    c = _app()
    page = c.get("/analysis").get_data(as_text=True)

    import re
    expect = re.search(r'var expect = "(\d*)"', page)
    src = re.search(r'analysis\.js\?v=(\d+)', page)
    assert expect and src, page[:400]
    assert expect.group(1) == src.group(1), \
        "the expected stamp is the one actually put in the script's URL"


def test_the_header_still_shows_the_short_build_of_all_assets():
    """Two different jobs: the header names the deployment at a glance, the
    check names one file exactly. Collapsing them is what broke the check."""
    c = _app()
    page = c.get("/analysis").get_data(as_text=True)
    import re
    header = re.search(r'сб\. (\S+)</span>', page)
    assert header and len(header.group(1)) <= 6


def test_an_item_whose_book_was_never_read_is_not_placed_on():
    """With no book the pricing finds no rival, bids the whole ceiling, leaves
    no room to answer an outbid, and counts every cheap sale as a fill nobody
    would have taken from us. All three err the same way, so an unswept item
    outranks the ones we have looked at - and the plan offered $787.50 on one.
    """
    import datetime as dt

    name = "★ Broken Fang Gloves | Unhinged (Field-Tested)"
    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    now = dt.datetime.now(dt.timezone.utc)
    spread = (-8.0, -4.0, 0.0, 4.0, 8.0, 12.0)
    rows = []
    for k, f in enumerate((0.155, 0.165, 0.175)):
        for i in range(16):
            price = (50.0 - 2.0 * k) + spread[i % len(spread)]
            rows.append((f"{name}{f}{i}", item_id, name, int(price * 100), price,
                         f, (now - dt.timedelta(days=(i % 13) + 0.5)).isoformat()))
    db.conn.executemany(
        "INSERT INTO sales (sale_id, item_id, market_hash_name, price_cents, "
        "price, float_value, sold_at, sold_at_estimated, scraped_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)", [r + (r[-1],) for r in rows])
    db.conn.commit()
    assert db.book_swept_at(item_id) is None
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})
    c.post("/api/analysis/params", json={"an_total_capital": "5000"})

    body = c.get("/api/analysis").get_json()["items"][0]
    assert body["bands"], "the bands are still shown"
    assert not any(b["take"] for b in body["bands"])
    assert all("стакан покупки не читался" in b["reason"]
               for b in body["bands"])

    plan = c.get("/api/analysis/plan").get_json()
    assert not [a for a in plan["actions"] if a["kind"] == "place"]


def test_a_book_read_and_found_empty_is_a_reading_like_any_other():
    """"Nobody is bidding" is a fact about the market; "nobody has looked" is
    a fact about us. They used to store identically."""
    name = "★ Broken Fang Gloves | Unhinged (Field-Tested)"
    c = _app([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    db.replace_buy_orders(item_id, [])
    assert db.book_swept_at(item_id), "an empty sweep still counts as read"
    db.close()


def test_adding_an_item_does_not_re_sweep_the_ones_already_done():
    """The button queued the whole list, so a tenth item re-bought the nine
    already read - thirty-odd requests a head, against a budget of two hundred
    an hour per address."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _many_items([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    from src.depth import depth_profile

    db.replace_buy_orders(item_id, [
        {"price": 50.0, "qty": 1, "float_min": 0.15, "float_max": 0.19}])
    db.record_listing_depth(item_id, depth_profile(
        [{"id": "L1", "price": 52.0, "float": 0.16, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.38)))
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})

    body = c.post("/api/analysis/sweep", json={}).get_json()
    assert body["queued"] == []
    assert any("обойдён" in s["reason"] for s in body["skipped"])
    assert "запросов" in body["note"], "and says what that saved"


def test_a_forced_sweep_reads_it_anyway():
    """"Обойти" has to mean it when the operator says so."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _many_items([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    from src.depth import depth_profile

    db.replace_buy_orders(item_id, [
        {"price": 50.0, "qty": 1, "float_min": 0.15, "float_max": 0.19}])
    db.record_listing_depth(item_id, depth_profile(
        [{"id": "L1", "price": 52.0, "float": 0.16, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.38)))
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})

    assert c.post("/api/analysis/sweep",
                  json={"force": True}).get_json()["queued"] == [name]


def test_an_item_short_of_bands_is_swept_even_when_recent():
    """A sweep cut short leaves fewer bands than the wear range implies, and
    that is precisely the item that needs the requests."""
    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _many_items([name])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    from src.depth import depth_profile

    db.replace_buy_orders(item_id, [
        {"price": 50.0, "qty": 1, "float_min": 0.15, "float_max": 0.19}])
    # Two bands of the twelve a Field-Tested range holds.
    db.record_listing_depth(item_id, depth_profile(
        [{"id": "L1", "price": 52.0, "float": 0.16, "type": "buy_now",
          "created_at": "2026-09-28T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.19)))
    db.close()
    c.post("/api/analysis/items", json={"market_hash_name": name})

    assert c.post("/api/analysis/sweep", json={}).get_json()["queued"] == [name]


def test_the_analysis_does_not_price_an_item_against_our_own_order():
    """The plan strips our orders out of the book; the analysis did not, so
    an item we already bid on was priced against ourselves there, and the
    page's capital disagreed with the plan's."""
    c, name = _stocked()
    before = c.get("/api/analysis").get_json()["items"][0]

    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    top = max((b for b in before["bands"] if b["take"]), key=lambda b: b["bid"])
    db.upsert_our_order(item_id, top["float_min"], top["float_max"],
                        top["bid"] + 1.0, top["ceiling"], state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 170.0, "qty": 1, "float_min": 0.15, "float_max": 0.17},
        {"price": top["bid"] + 1.0, "qty": 1, "float_min": top["float_min"],
         "float_max": top["float_max"]}])
    db.close()

    after = c.get("/api/analysis").get_json()["items"][0]
    assert after["capital"] == before["capital"]
    assert [b["bid"] for b in after["bands"]] == [b["bid"] for b in before["bands"]]


def test_the_plan_never_offers_an_order_dearer_than_the_price_limit():
    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
    places = [a for a in c.get("/api/analysis/plan").get_json()["actions"]
              if a["kind"] == "place"]
    assert places and max(a["price"] for a in places) > 100.0

    c.post("/api/analysis/params", json={"scr_max_price": "100"})
    plan = c.get("/api/analysis/plan").get_json()
    assert not [a for a in plan["actions"] if a["kind"] == "place"]
    assert any("отсев по цене" in q["reason"] for q in plan["queue"]) or \
        not plan["queue"]


def test_the_weakest_order_is_found_and_queued_for_cancel():
    """Fills spent the balance, CSFloat's allowance fell under what stood, and
    every amend came back "insufficient balance". Room is freed by taking down
    the order the plan values least - one it would withdraw anyway first."""
    import json as _json

    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
    c.post("/api/analysis/placement",
           json=c.get("/api/analysis/placement").get_json()["suggested"])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    db.upsert_our_order(item_id, 0.36, 0.37, 50.0, 60.0, state="live",
                        remote_id="weak")
    db.close()

    preview = c.post("/api/analysis/cancel_lowest", json={"preview": True})
    assert preview.status_code == 200
    assert "0.3600–0.3700" in preview.get_json()["order"]
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    assert not db.get_setting("analysis_pending_actions"), "a preview sends nothing"
    db.close()

    body = c.post("/api/analysis/cancel_lowest", json={}).get_json()
    assert "0.3600–0.3700" in body["order"]
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    queued = _json.loads(db.get_setting("analysis_pending_actions"))
    db.close()
    assert queued["source"] == "manual"
    assert [(a["kind"], a["remote_id"]) for a in queued["actions"]] == \
        [("cancel", "weak")]
    assert c.post("/api/analysis/cancel_lowest", json={}).status_code == 409, \
        "never over a plan already waiting"


def test_positions_say_what_the_orders_add_up_to():
    c, name = _stocked()
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.get_item_id(name)
    db.upsert_our_order(item_id, 0.36, 0.37, 50.0, 60.0, state="live",
                        remote_id="a", quantity=2)
    db.upsert_our_order(item_id, 0.30, 0.31, 20.0, 30.0, state="live",
                        remote_id="b")
    db.close()
    c.post("/api/analysis/params", json={"an_balance": "700"})
    body = c.get("/api/analysis/positions").get_json()
    assert body["face"] == 120.0
    assert body["allowance"] == 7000.0


def test_every_band_carries_what_the_other_pricing_makes_of_it():
    """Switching the pricing is a decision about money, made on the items it
    would change: each band says what the other one would bid, and the toggle
    sticks."""
    c, name = _stocked()
    plain = c.get("/api/analysis").get_json()
    item = next(i for i in plain["items"] if i["item"] == name)
    assert "alt_take" not in item and not any("alt" in b for b in item["bands"]), \
        "scored once unless a comparison is asked for"
    body = c.get("/api/analysis?compare=1").get_json()
    item = next(i for i in body["items"] if i["item"] == name)
    assert body["params"]["adaptive"] in (0, False)
    assert item["bands"] and all("alt" in b for b in item["bands"])
    assert "alt_take" in item

    r = c.post("/api/analysis/params", json={"an_adaptive": "1"})
    assert r.get_json()["params"]["adaptive"] == 1
    body = c.get("/api/analysis").get_json()
    assert body["params"]["adaptive"] == 1


def test_the_plan_downloads_as_an_excel_file():
    """Two plans side by side - before and after the pricing changed - is how
    the switch gets decided, and that takes the whole queue in a file."""
    import io
    import zipfile

    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
    r = c.get("/api/analysis/plan.xlsx")
    assert r.status_code == 200
    assert r.mimetype.endswith("spreadsheetml.sheet")
    assert "plan_" in r.headers["Content-Disposition"] and "_old.xlsx" in \
        r.headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "xl/workbook.xml" in names and "xl/worksheets/sheet3.xml" in names
    queue = z.read("xl/worksheets/sheet1.xml").decode()
    assert "Specialist Gloves" in queue and "потолок $" in queue
    settings = z.read("xl/worksheets/sheet3.xml").decode()
    assert "старый" in settings
    c.post("/api/analysis/params", json={"an_adaptive": "1"})
    r = c.get("/api/analysis/plan.xlsx")
    assert "_new.xlsx" in r.headers["Content-Disposition"]


def test_the_xlsx_writer_keeps_numbers_numbers_and_escapes_text():
    import io
    import zipfile

    from src.xlsx import _col, workbook

    assert [_col(i) for i in (0, 25, 26, 27)] == ["A", "Z", "AA", "AB"]
    data = workbook([("Лист", ["a", "b"], [[1.5, "x < y & z"], [None, True]])])
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "<v>1.5</v>" in sheet and "x &lt; y &amp; z" in sheet and ">да<" in sheet
