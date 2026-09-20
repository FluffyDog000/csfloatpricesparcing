"""Does the page script actually run?

A browser error in a dashboard script is invisible from Python: the page
renders, the endpoints answer, and the only symptom is a button that appears
to do nothing. That is exactly how a missing common.js shipped. These run the
script under node against a stub DOM and the real API response.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _run(api_response, expect_ok=True):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(api_response, fh, ensure_ascii=False)
        path = fh.name
    try:
        out = subprocess.run(
            [NODE, "tests/js/dom_stub.js", "static/analysis.js", path],
            capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, f"скрипт упал:\n{out.stderr}"
        result = json.loads(out.stdout.strip().splitlines()[-1])
        if expect_ok:
            assert "Ошибка" not in result["status"], result["status"]
        return result
    finally:
        os.unlink(path)


def _api(client):
    return client.get("/api/analysis").get_json()


def test_the_page_script_runs_against_a_real_response():
    from tests.test_analysis_page import _app

    name = "★ Driver Gloves | Snow Leopard (Field-Tested)"
    c = _app([name])
    c.post("/api/analysis/items", json={"market_hash_name": name})
    result = _run(_api(c))

    assert "Ошибка" not in result["status"], result["status"]
    assert result["kind"] != "err"
    assert result["chips"] == 1, "the item added must appear in the list"
    assert result["sections"] == 1, "and its report must render"
    assert result["funnel"] >= 1, "the summary above it must render too"
    assert "сборка" in result["status"], \
        "the status names the build, so a screenshot says which script ran"


def test_an_empty_list_says_so_rather_than_rendering_nothing():
    from tests.test_analysis_page import _app

    result = _run(_api(_app()))
    assert "пуст" in result["status"]
    assert "пуст" in result["listHtml"]
    assert result["sections"] == 0


def test_a_failure_midway_does_not_blank_the_list():
    """Chips used to be appended after clearing the box, so anything throwing
    in the middle left an empty list under a filled-in status line."""
    from tests.test_analysis_page import _app

    name = "★ Specialist Gloves | Big Swell (Field-Tested)"
    c = _app([name])
    c.post("/api/analysis/items", json={"market_hash_name": name})
    body = _api(c)
    # A band missing the fields the row renderer reads. The item has no sales,
    # so the screen reports it instead of scoring it - the malformed band is
    # put in by hand, which is the point of the test either way.
    body["items"][0]["screened_out"] = ""
    body["items"][0]["bands"] = [{"float_min": 0.15, "float_max": 0.17,
                                  "take": True}]
    result = _run(body, expect_ok=False)
    assert result["chips"] == 1, "the list survives a broken report"
    assert "Ошибка" in result["status"], \
        "and the failure is stated rather than swallowed"


def test_the_page_scripts_share_one_scope_without_colliding():
    """A browser loads common.js and a page script into the same global scope,
    so one name declared in both is a SyntaxError at parse time - and the page
    script is skipped whole, with no listener attached and every button dead.
    `const money` in analysis.js against `function money` in common.js is
    exactly that, and it looked like a page that ignored every click."""
    import pathlib
    import re

    def declared(path):
        found = {}
        for i, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
            m = re.match(r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
                         line)
            if m:
                found[m.group(1)] = i
        return found

    common = declared("static/common.js")
    for page in ("analysis.js", "index.js", "item.js", "load.js",
                 "journal.js", "settings.js"):
        path = f"static/{page}"
        if not pathlib.Path(path).exists():
            continue
        clash = sorted(set(common) & set(declared(path)))
        assert not clash, f"{page} redeclares {clash} from common.js"


def test_both_scripts_parse_as_one_program():
    """The check above reads declarations line by line; this one hands the
    pair to a real parser, the way a browser gets them."""
    import pathlib
    import subprocess
    import tempfile

    source = (pathlib.Path("static/common.js").read_text() + "\n"
              + pathlib.Path("static/analysis.js").read_text())
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(source)
        path = fh.name
    try:
        out = subprocess.run([NODE, "--check", path],
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
    finally:
        os.unlink(path)


def _plan_payload(**over):
    base = {
        "actions": [{"kind": "place", "item": "★ Gloves | Fade (Field-Tested)",
                     "float_min": 0.32, "float_max": 0.38, "price": 159.0,
                     "ceiling": 170.0, "reason": "59%/мес", "order_id": None,
                     "remote_id": None, "was": None}],
        "limits": {"total_capital": 500.0, "max_orders": 20,
                   "max_orders_per_item": 3, "per_item_capital": 0.0,
                   "patience_minutes": 60.0},
        "held": {}, "by_item": {"★ Gloves | Fade (Field-Tested)": 159.0},
        "planned_total": 159.0, "concentration": 1.0,
        "armed": True, "dry_run": True, "pending": False, "last_apply": None,
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "last_defend": None,
        "placement": "настроено: POST /api/v1/buy-orders",
        "can_place": True, "can_cancel": True, "waiting": [],
    }
    base.update(over)
    return base


def _run_with_plan(plan, api=None):
    import json
    import os
    import subprocess
    import tempfile

    from tests.test_analysis_page import _app
    c = _app()
    body = api if api is not None else c.get("/api/analysis").get_json()
    paths = []
    for blob in (body, plan):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(blob, fh, ensure_ascii=False)
        fh.close()
        paths.append(fh.name)
    try:
        out = subprocess.run(
            [NODE, "tests/js/dom_stub.js", "static/analysis.js", *paths],
            capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        for path in paths:
            os.unlink(path)


def test_the_apply_button_says_why_it_would_refuse():
    """A button that will be refused should say so before it is pressed:
    "ничего не произошло" is the one outcome that teaches nothing."""
    import pathlib

    js = pathlib.Path("static/analysis.js").read_text()
    for reason in ("запрос постановки не настроен", "бюджет равен нулю",
                   "выставление не разрешено", "нечего выполнять"):
        assert reason in js

    _run_with_plan(_plan_payload(can_place=False))
    _run_with_plan(_plan_payload(limits={"total_capital": 0.0, "max_orders": 20,
                                         "max_orders_per_item": 3,
                                         "per_item_capital": 0.0,
                                         "patience_minutes": 60.0}))


def test_handing_a_plan_over_shows_what_went():
    """The collector works on its own cycle, so between the press and the
    result there is a gap the page used to spend in silence."""
    import pathlib

    js = pathlib.Path("static/analysis.js").read_text()
    assert "renderQueued" in js, "what was sent is listed at once"
    assert "Жду сборщик" in js, "and the wait is shown as progress"
    assert "не отчитался" in js, "and it gives up saying so, not silently"


def test_the_limit_fields_are_loaded_before_they_can_be_saved_back():
    """fillLimits was defined and never called, so the inputs kept the
    markup's defaults while the database held real numbers. The next save of
    any threshold posted a budget of zero over a live one, and the only sign
    was the plan going quiet."""
    import pathlib
    import re

    js = pathlib.Path("static/analysis.js").read_text()
    calls = [m for m in re.finditer(r"\bfillLimits\(", js)]
    assert len(calls) >= 3, "defined, called on load, and after saving"

    result = _run_with_plan(_plan_payload(
        limits={"total_capital": 500.0, "per_item_capital": 120.0,
                "max_orders": 12, "max_orders_per_item": 3,
                "patience_minutes": 60.0}))
    assert result["budget"] == 500, "the saved budget reaches the form"


def test_saving_thresholds_keeps_the_budget():
    """The round trip the bug broke: change a threshold, keep the money."""
    from tests.test_analysis_page import _app

    c = _app()
    c.post("/api/analysis/params", json={"an_total_capital": "500"})
    r = c.post("/api/analysis/params", json={"an_min_margin": "0.05"}).get_json()

    assert r["limits"]["total_capital"] == 500.0, \
        "a threshold save must not touch the money"
    assert c.get("/api/analysis/plan").get_json()["limits"]["total_capital"] == 500.0


def test_a_failed_row_shows_the_server_text_as_text():
    """The reason cell now carries a server message and the JSON that drew it.
    Built with innerHTML it would be markup; a `<` in an error body would eat
    the rest of the row."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    table = source.split("function actionTable")[1].split("\n  }")[0]
    assert "why.textContent" in table
    assert 'class="muted">${r.detail' not in table, \
        "the server's own words must not be pasted in as markup"


def _run_script(script, api_response):
    """The same stand-in DOM, pointed at another page's script."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(api_response, fh, ensure_ascii=False)
        path = fh.name
    try:
        out = subprocess.run([NODE, "tests/js/dom_stub.js", script, path],
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, f"скрипт упал:\n{out.stderr}"
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def test_the_journal_page_script_renders_what_the_endpoint_returns():
    """The analysis tab shipped dead once - a parse error, a page that looked
    fine and did nothing. A second page deserves the same check before it goes
    out, not after."""
    reply = {
        "events": [
            {"id": 2, "at": "2026-09-19T13:55:00", "item_id": 1,
             "market_hash_name": "★ Gloves | Fade (FT)", "float_min": 0.15,
             "float_max": 0.17, "kind": "raise", "price": 151.0, "was": 150.0,
             "ceiling": 160.0, "remote_id": "r1", "ok": True, "dry": False,
             "source": "defence", "reason": "перебили", "detail": "цена изменена"},
            {"id": 1, "at": "2026-09-19T12:49:00", "item_id": 1,
             "market_hash_name": "★ Gloves | Fade (FT)", "float_min": 0.15,
             "float_max": 0.17, "kind": "place", "price": 150.0, "was": None,
             "ceiling": 160.0, "remote_id": "r1", "ok": True, "dry": False,
             "source": "plan", "reason": "полоса проходит", "detail": "поставлен"},
        ],
        "held": 1, "items": ["★ Gloves | Fade (FT)"],
        "defend": True, "defend_minutes": 60, "defend_at": "2026-09-19T14:00:00",
    }
    got = _run_script("static/journal.js", reply)
    assert "Ошибка" not in got["status"], got["status"]
    assert got["journalRows"] == 2, "both events must reach the table"
    assert got["tiles"] >= 5, "the summary tiles must render"
    assert "60" in got["defence"], "the defence interval must be stated"


def test_the_journal_says_so_when_nothing_has_happened():
    got = _run_script("static/journal.js",
                      {"events": [], "held": 0, "items": [], "defend": False,
                       "defend_minutes": 60, "defend_at": None})
    assert "Ошибка" not in got["status"], got["status"]
    assert got["journalRows"] == 0
    assert "выключена" in got["defence"]


def test_only_items_with_orders_can_be_shown():
    """A hundred items is a hundred tables, and most of them say "no". The
    filter re-renders what is already loaded rather than asking the server to
    score everything again."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    assert "an-only-take" in source
    assert "lastData" in source, "the reply is kept for re-rendering"
    handler = source.split('const filter = $("an-only-take");')[1][:400]
    assert "renderResults(lastData)" in handler
    assert "/api/analysis" not in handler, "a filter must not re-score"


def test_each_item_report_collapses():
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    body = source.split("function renderResults")[1]
    assert 'createElement("details")' in body, \
        "one open panel per item is the layout that made the tab unusable"
    assert 'createElement("summary")' in body


def test_the_journal_says_the_count_is_unverified_until_it_is():
    """Four orders were placed and all four taken down from the site by hand.
    The tile went on saying four. Until a comparison has run, the page has to
    say the number is our own record rather than the account's."""
    got = _run_script("static/journal.js",
                      {"events": [], "held": 4, "manual": 0, "items": [],
                       "defend": False, "defend_minutes": 60, "defend_at": None,
                       "sync": None, "sync_at": None, "sync_pending": False})
    assert "не сверялись" in got["sync"]
    assert "бот записал себе" in got["sync"]


def test_the_journal_reports_what_the_comparison_found():
    got = _run_script("static/journal.js", {
        "events": [], "held": 0, "manual": 1, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync_pending": False, "sync_at": "2026-09-20T10:00:00",
        "sync": {"seen": 1, "error": "",
                 "counts": {"gone": 4, "filled": 0, "repriced": 0,
                            "adopted": 1, "matched": 0}},
    })
    assert "4 нет на сайте" in got["sync"]
    assert "1 не наших" in got["sync"]
    assert got["tiles"] >= 6, "the manual tile must render too"


def test_a_failed_comparison_is_not_reported_as_agreement():
    got = _run_script("static/journal.js", {
        "events": [], "held": 4, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync_pending": False, "sync_at": "2026-09-20T10:00:00",
        "sync": {"seen": 0, "error": "не настроен запрос списка ордеров",
                 "counts": {}},
    })
    assert "не удалась" in got["sync"] and "не настроен" in got["sync"]


def test_an_unverified_holding_count_is_flagged_without_blocking_placement():
    """Not being sure the orders are still there does not stop the bot placing
    new ones - but it does change what the held figures mean, and the two must
    not be said in the same sentence."""
    from tests.test_analysis_page import _app

    c = _app()
    result = _run(_api(c))
    assert "сверялись" in result["planSync"]
    assert "сверялись" not in result.get("status", ""), \
        "a warning is not a blocker"


def test_a_failed_search_lists_what_was_asked():
    """Without the attempts, the next step is guessing at the same paths by
    hand."""
    got = _run_script("static/journal.js", {
        "events": [], "held": 1, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync_pending": False, "sync_at": "2026-09-20T10:00:00",
        "sync": {"seen": 0, "counts": {},
                 "error": "не нашёл, где CSFloat отдаёт список ордеров",
                 "tried": ["/api/v1/buy-orders — HTTPError: HTTP 405",
                           "/api/v1/me/buy-orders — HTTPError: HTTP 404"]},
    })
    assert "не нашёл" in got["sync"]
    assert "405" in got["sync"] and "404" in got["sync"]


def test_a_found_listing_path_is_reported():
    got = _run_script("static/journal.js", {
        "events": [], "held": 1, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync_pending": False, "sync_at": "2026-09-20T10:00:00",
        "sync": {"seen": 1, "error": "",
                 "found_path": "/api/v1/me/buy-orders",
                 "counts": {"matched": 1, "gone": 0, "filled": 0,
                            "repriced": 0, "adopted": 0}},
    })
    assert "нашёлся" in got["sync"] and "/api/v1/me/buy-orders" in got["sync"]
    assert "1 совпало" in got["sync"]


def test_a_screened_out_item_renders_as_one_line_not_a_table():
    """Three hundred items, most of them dropped before any request: the
    report has to say which rule dropped each one, once, without pretending to
    have scored bands it never computed."""
    from tests.test_analysis_page import _app

    c = _app()
    body = {
        "items": [
            {"item": "A (FT)", "sales": 40, "orders": 0, "depth": 0,
             "bands": [], "capital": 0.0, "monthly": 0.0, "swept_at": None,
             "screened_out": "медиана $412.00 дороже $150.00",
             "screen": {"median": 412.0, "flow": 0.3, "quiet_days": 2.0,
                        "spread": 0.1, "gap": 0.2, "sales": 40,
                        "passed": False, "reason": ""}},
            {"item": "B (FT)", "sales": 40, "orders": 0, "depth": 0,
             "bands": [], "capital": 0.0, "monthly": 0.0, "swept_at": None,
             "screened_out": "медиана $900.00 дороже $150.00",
             "screen": {"median": 900.0, "flow": 0.3, "quiet_days": 2.0,
                        "spread": 0.1, "gap": 0.2, "sales": 40,
                        "passed": False, "reason": ""}},
        ],
        "params": {"fee": 0.02, "band_step": 0.02},
        "screen": {"max_price": 150.0},
        "error": "", "waiting": [],
    }
    got = _run(body)
    assert "Ошибка" not in got["status"], got["status"]
    assert got["sections"] == 2, "one collapsed line each"
    # Grouped by the rule, not by the value: two prices, one reason.
    assert "2 — медиана … дороже …" in got["funnelText"], got["funnelText"]
    assert "запросов" in got["funnelText"]


def test_a_borrowed_price_is_labelled_as_borrowed():
    """A price worked out from neighbouring bands looks exactly like a
    measured one in a table, and it is not the same claim."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.borrowed" in row and "b.reach" in row
    assert "priced_from" in row


def test_the_surcharge_says_what_the_cheap_price_would_have_earned():
    """"Why bid over the book" has been asked twice from behind a tooltip.
    The surcharge is only defensible by the number it buys, so that number is
    in the table rather than on hover."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "cheapWhy" in row
    assert "по ней сделок нет" in row, "the case where the minimum fills nothing"
    assert "по ней набор" in row


def test_the_table_spans_its_own_columns():
    """A row that skips spans the table, so the span has to follow the
    columns. It has been wrong twice: once when a column arrived, once when
    one left."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    headers = source.split("<th>float</th>")[1].split("</thead>")[0].count("<th")
    span = int(row.split('colspan="')[1].split('"')[0])
    assert span == headers, f"{span} columns spanned, {headers} exist"


def test_the_report_states_no_monthly_rate():
    """A rate needs a cycle, and a cycle needs the trade lock and the payout
    wait - a fortnight nothing here measures. Better silent than wrong."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "%/мес" not in row and "monthly" not in row


def test_the_journal_shows_where_each_order_stands():
    """"Am I still first" is not answered by the plan or by the log, and the
    only other route to it was the defence - which also acts."""
    got = _run_script("static/journal.js", {
        "events": [], "held": 2, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync": None, "sync_at": None, "sync_pending": False,
        "positions": {
            "outbid": 1, "checking": False, "test_pending": False,
            "test_amend": None,
            "orders": [
                {"id": 1, "item": "A (FT)", "float_min": 0.15,
                 "float_max": 0.17, "price": 100.0, "ceiling": 120.0,
                 "state": "live", "remote_id": "r1", "top": 90.0, "ahead": 0,
                 "first": True, "swept_at": "2026-09-20T10:00:00", "book": 3},
                {"id": 2, "item": "B (FT)", "float_min": 0.20,
                 "float_max": 0.22, "price": 50.0, "ceiling": 60.0,
                 "state": "live", "remote_id": "r2", "top": 55.0, "ahead": 2,
                 "first": False, "swept_at": "2026-09-20T10:00:00", "book": 4},
            ],
        },
    })
    assert "Ошибка" not in got["status"], got["status"]
    assert got["positionRows"] == 2
    assert "Перебили 1 из 2" in got["positions"]
    assert not got["probeOff"], "there is a live order to probe with"


def test_the_amend_probe_needs_an_order_that_exists_on_the_site():
    got = _run_script("static/journal.js", {
        "events": [], "held": 0, "manual": 1, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync": None, "sync_at": None, "sync_pending": False,
        "positions": {
            "outbid": 0, "checking": False, "test_pending": False,
            "test_amend": None,
            "orders": [{"id": 9, "item": "C (FT)", "float_min": 0.15,
                        "float_max": 0.17, "price": 10.0, "ceiling": 12.0,
                        "state": "manual", "remote_id": "x9", "top": 0,
                        "ahead": 0, "first": True, "swept_at": None,
                        "book": 0}],
        },
    })
    assert got["probeOff"], "an order the bot does not drive is not a probe"


def test_a_finished_probe_reports_what_was_sent():
    got = _run_script("static/journal.js", {
        "events": [], "held": 1, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync": None, "sync_at": None, "sync_pending": False,
        "positions": {
            "outbid": 0, "checking": False, "test_pending": False,
            "test_amend": {"ok": True, "confirmed": True,
                           "detail": "цена изменена на $39.70",
                           "sent": {"max_price": 3970, "quantity": 1,
                                    "min_float": 0.15, "max_float": 0.17}},
            "orders": [{"id": 1, "item": "A (FT)", "float_min": 0.15,
                        "float_max": 0.17, "price": 39.7, "ceiling": 45.0,
                        "state": "live", "remote_id": "r1", "top": 30.0,
                        "ahead": 0, "first": True,
                        "swept_at": "2026-09-20T10:00:00", "book": 2}],
        },
    })
    assert "Проверка правки" in got["positions"]
    assert "max_price" in got["positions"], "what went out, not just the verdict"


def test_the_amend_probe_says_why_it_is_greyed_out():
    """A disabled button with no reason beside it reads as a button that is
    not there, and that is what it was asked about."""
    got = _run_script("static/journal.js", {
        "events": [], "held": 0, "manual": 0, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync": None, "sync_at": None, "sync_pending": False,
        "positions": {"orders": [], "outbid": 0, "checking": False,
                      "test_amend": None, "test_pending": False},
    })
    assert got["probeOff"]
    assert "нужен ордер" in got["probeLabel"], got["probeLabel"]
