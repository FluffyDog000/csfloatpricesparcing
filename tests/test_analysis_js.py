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
    # A report the renderer cannot walk at all. It used to be a band missing
    # the fields the row reads, but the row now prints "—" for a field that is
    # not there rather than throwing - which is better behaviour and a worse
    # vehicle for this test, whose subject is the list surviving, not which
    # field breaks.
    body["items"][0]["screened_out"] = ""
    body["items"][0]["bands"] = "не массив"
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


def test_the_table_shows_the_rival_it_is_outbidding():
    """The bid is one step over whoever covers our top, so the bid alone is
    unreadable: $48.40 says nothing until the $48.30 beside it does."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.top" in row, "the rival's bid is its own column"
    assert "никого" in row, \
        "an empty band is said outright - it is why the bid is the ceiling"
    header = source.split("полоса</th>")[1].split("</thead>")[0]
    assert "соперник" in header


def test_the_table_shows_the_rank_the_plan_orders_by():
    """The plan places in rank order, and the rank was nowhere on the page:
    "why is this one first" had no answer you could read off the table."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "const rank = b.rank || 0" in row, \
        "the rank the ladder computed, not one worked out again on the page"
    header = source.split("полоса</th>")[1].split("</thead>")[0]
    assert "ранг" in header


def test_the_headroom_is_shown_in_outbids_not_only_dollars():
    """Dollars of room mean nothing without the grid step: $1.20 is twelve
    outbids at a dime and one at a dollar."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.step" in row and "Math.floor" in row


def test_no_column_reads_a_field_the_ladder_stopped_filling():
    """The old disjoint-band model left `entry`, `wars` and `t_buy`
    behind (`t_sell` is filled again: the rank divides by it). Nothing fills them now, so a column reading one renders a blank
    or throws - which is how "запас None перебив." reached the plan page."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    for dead in ("b.entry", "b.wars", "b.t_buy", "b.entry_lam",
                 "b.paid", "b.margin_worst"):
        assert dead not in row, f"{dead} is not filled any more"


def test_the_table_spans_its_own_columns():
    """A row that skips spans the table, so the span has to follow the
    columns. It has been wrong twice: once when a column arrived, once when
    one left."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    headers = source.split("полоса</th>")[1].split("</thead>")[0].count("<th")
    span = int(row.split('colspan="')[1].split('"')[0])
    assert span == headers, f"{span} columns spanned, {headers} exist"


def test_the_report_states_no_monthly_rate():
    """A rate needs a cycle, and a cycle needs the trade lock and the payout
    wait - a fortnight nothing here measures. Better silent than wrong."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "%/мес" not in row and "monthly" not in row




def test_the_price_column_says_where_the_number_came_from():
    """History or the sell queue: the two can differ by three dollars, and
    which one bound is the first thing a reader needs."""
    import pathlib

    source = pathlib.Path("static/analysis.js").read_text()
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.priced_from" in row
    assert "b.sample" in row, "and on how many sales it rests"


def test_waiting_is_counted_in_minutes_not_in_polls():
    """"проверка 3 из 25" sat beside "обойдено 0 из 1" and read as a second
    progress bar over the same work - it was asked whether 25 was the float
    bands of a Minimal Wear skin, which has four. It is this page asking the
    server again, and what the reader wants from it is how long is left."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    assert "проверка ${tries} из" not in source
    assert "проверка ${i} из" not in source
    assert source.count("спрашиваю ещё") == 2, "both waits say it the same way"


def test_the_two_sides_of_the_market_are_named_apart():
    """"ордеров в стакане 21" sat above a column also reading 21 and was taken
    for the same number. One is who bids against us; the other is who we would
    queue behind when selling."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    assert "чужих ордеров на покупку" in source
    # The old wording survives in the comment explaining why it went; the
    # check is on what the page renders, not on what it says about itself.
    rendered = source.split("meta.textContent")[1].split(";")[0]
    assert "ордеров в стакане" not in rendered


def test_the_depth_line_counts_bands_and_says_so():
    """`it.depth` is the number of float bands read, and the line called them
    listings - a different thing, and one the reader can check against four
    bands holding fifty lots each."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    assert "стакан продаж прочитан по ${it.depth} полосам" in source
    assert "листингов по полосам" not in source


def test_the_exit_shows_the_candidate_that_lost_as_well():
    """"история" says which of the two priced the rung and not by how much.
    Whether the sell queue came anywhere near binding is the whole question
    the stored lot prices were collected to answer, and the table was silent
    on it."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.queue_price" in row
    assert "против" in row, "the losing candidate is named beside the winner"
    assert "за блокировку" in row, \
        "and when the queue cannot bind, why - the lock cleared it"


def test_a_queue_that_cannot_bind_says_so_in_the_row_not_on_hover():
    """The case with no second price - the lock emptying the queue - is the
    one that most needs saying, and it rendered as a blank cell with the
    explanation hidden in a tooltip. Half of this is read on a phone, where
    there is no hover at all."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "очередь не ограничивает" in row
    assert "стакан продаж не читался" in row, \
        "and an item never swept is its own case"
    # The second line is unconditional: every branch of exitSecond produces text.
    assert "other !== null ?" not in row


def test_the_queue_column_is_a_count_of_what_blocks_us():
    """The prices were shown for one commit and taken out again: once the
    count means "lots cheaper than the median", it answers the question the
    prices were added for, and eight numbers per row on a phone did not."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "b.queue" in row
    assert "b.asks" not in row
    header = source.split("полоса</th>")[1].split("</thead>")[0]
    assert "дешевле медианы" in header, "the header says what it counts"


def test_a_truncated_band_is_marked_in_the_count_and_explained():
    """Fifty is the endpoint's maximum, so the count is a floor. Printing it
    as a plain number claims a completeness nobody measured."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert 'b.queue_capped ? "+" : ""' in row
    assert "Полоса прочитана до предела выдачи" in row, \
        "and the row says what the plus means"
    assert "b.queue_read" in row, \
        "with the band's own size, or '8+' against a limit of fifty reads " \
        "as a contradiction"


def test_the_queue_count_carries_the_age_of_the_reading():
    """The listings are written only by a requested sweep, never on a
    schedule, so the count is exactly as old as that sweep. Eight hours was
    enough for three lots to sell and a fourth to be repriced, and the number
    read as a claim about the market now rather than about the market then."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "staleness(depthAt)" in row
    assert "86400000" in row, "a reading over a day old is marked, not just aged"
    assert "function bandRow(b, depthAt, depthBands)" in source


def test_the_age_is_given_in_units_a_reader_thinks_in():
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    fn = source.split("function staleness")[1].split("\n  }")[0]
    for unit in ("мин назад", "ч назад", "дн назад"):
        assert unit in fn


def test_a_sweep_counts_as_done_only_when_both_halves_landed():
    """`swept_at` is the buy side's timestamp. Judging the sweep by it alone
    reported success whenever the book arrived while the listings were
    refused - and the queue then stayed empty with nothing saying why."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    sweep = source.split('$("an-sweep")')[1].split('$("an-run")')[0]
    assert 'const stamp = (it) =>' in sweep
    assert "it.depth_at" in sweep, "the sell side's own timestamp counts too"
    assert '(it.swept_at || "") !== (before[it.item]' not in sweep
    assert "только стакан покупки" in sweep, \
        "and the half that is missing is named"


def test_a_band_read_empty_is_not_called_uncollected():
    """The header said twelve bands were read while every rung said "лоты не
    собраны": a band read and holding nothing got the same sentence as a band
    nobody read. The first says the float range is empty; the second says we
    are guessing."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    row = source.split("function bandRow")[1].split("\n  }")[0]
    assert "стакан продаж не читался" in row
    assert "в этой полосе лотов не было при обходе" in row
    assert "function bandRow(b, depthAt, depthBands)" in source


def test_nothing_queued_says_which_filter_took_the_items():
    """"Список пуст" was said whenever nothing was queued - which, since the
    sweep began skipping fresh and screened-out items, is nearly never what
    happened. The list had one item, it was filtered, and the reason was in
    the reply and thrown away."""
    source = pathlib.Path("static/analysis.js").read_text(encoding="utf-8")
    block = source.split('$("an-sweep")')[1].split("const before")[0]
    assert "r.skipped" in block
    assert "все предметы отсеяны" in block


# -- the journal page -------------------------------------------------------


def _ago(minutes=5):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)) \
        .replace(tzinfo=None).isoformat(timespec="seconds")


def _event(id_, kind, ok=True, name="A (FT)", lo=0.15, hi=0.17, price=10.0,
           detail="", at=None, source="plan", reason=""):
    return {"id": id_, "at": at or _ago(60 - id_), "item_id": 1,
            "market_hash_name": name, "float_min": lo, "float_max": hi,
            "kind": kind, "price": price, "was": None, "ceiling": 20.0,
            "remote_id": None, "ok": ok, "dry": False, "source": source,
            "reason": reason, "detail": detail}


def _journal(events, **extra):
    body = {"events": events, "held": 0, "manual": 0, "items": [],
            "defend": False, "defend_minutes": 60, "defend_at": None,
            "sync": {"seen": 0, "error": "", "counts": {}},
            "sync_at": _ago(10), "sync_pending": False}
    body.update(extra)
    return body


def _row(**over):
    row = {"id": 1, "item": "A (FT)", "float_min": 0.15, "float_max": 0.17,
           "price": 10.0, "ceiling": 12.0, "placed_ceiling": 12.0, "room": 2.0,
           "quantity": 1, "state": "live", "remote_id": "r1", "top": 9.0,
           "ahead": 0, "first": True, "seen_in_book": True,
           "swept_at": _ago(5), "book": 3, "verdict": None}
    row.update(over)
    return row


def _position(*rows, **over):
    rows = list(rows) or [_row(**over)]
    return {"orders": rows, "outbid": sum(1 for r in rows if not r["first"]),
            "book_named": 0, "book_rows": 3, "checking": False}


def test_the_journal_page_script_renders_what_the_endpoint_returns():
    """The analysis tab shipped dead once - a parse error, a page that looked
    fine and did nothing. A second page deserves the same check before it goes
    out, not after."""
    events = [_event(2, "raise", price=11.0, source="defence"),
              _event(1, "place", price=10.0)]
    got = _run_script("static/journal.js", _journal(
        events, held=1, defend=True, positions=_position(_row(price=11.0))))
    assert "Ошибка" not in got["bar"], got["bar"]
    assert got["orderGroups"] == 1, "two events of one order are one card"
    assert "поставлен" in got["ordersText"] and "поднят" in got["ordersText"], \
        "and both are in its history"
    assert "поставлен" in got["journalText"], "and in the history below"
    assert got["tiles"] >= 6
    assert "60" in got["defence"], "the defence interval must be stated"


def test_only_orders_standing_now_are_listed():
    """A filled or withdrawn order is history: listing it beside the live
    ones made the page a long scroll of things that no longer exist."""
    events = [_event(3, "cancel", name="Gone (FT)", source="sync"),
              _event(2, "place", name="Gone (FT)"),
              _event(1, "place")]
    got = _run_script("static/journal.js", _journal(
        events, positions=_position(_row())))
    assert got["orderGroups"] == 1
    assert "Gone" not in got["ordersText"]
    assert "Gone" in got["journalText"], "it is in the history"


def test_each_order_carries_its_own_history():
    events = [_event(5, "raise", price=14.0), _event(4, "raise", price=13.0),
              _event(3, "raise", price=12.0), _event(2, "place", price=11.0),
              _event(1, "place", name="B (FT)", lo=0.2, hi=0.21)]
    got = _run_script("static/journal.js", _journal(events, positions=_position(
        _row(price=14.0), _row(id=2, item="B (FT)", float_min=0.2,
                               float_max=0.21))))
    assert got["orderGroups"] == 2
    assert got["ordersText"].count("поднят") == 3


def test_an_unanswered_refusal_is_a_problem_and_explained():
    events = [_event(2, "place", ok=False,
                     detail="HTTP 400 для /api/v1/buy-orders — price too low"),
              _event(1, "place", ok=False, detail="HTTP 400")]
    got = _run_script("static/journal.js", _journal(events))
    assert "не поставлен (2 раза подряд)" in got["attention"]
    assert "отклонил параметры ордера: price too low" in got["attention"]
    assert "Проблемы (1)" in got["problemsTitle"]


def test_a_refusal_answered_since_is_history_not_a_problem():
    events = [_event(2, "place"), _event(1, "place", ok=False, detail="HTTP 500")]
    got = _run_script("static/journal.js", _journal(events))
    assert "Проблем нет" in got["attention"]


def test_an_old_refusal_is_not_a_problem_any_more():
    """Errors already fixed stayed on top of the page for days."""
    events = [_event(1, "raise", ok=False, detail="HTTP 500",
                     at=_ago(60 * 30))]
    got = _run_script("static/journal.js", _journal(
        events, positions=_position(_row())))
    assert "Проблем нет" in got["attention"]


def test_a_refusal_the_defence_has_looked_at_since_is_settled():
    events = [_event(1, "raise", ok=False, at=_ago(30),
                     detail="NoRouteAvailable: адреса главного ключа недоступны")]
    verdict = {"kind": "keep", "reason": "мы первые в полосе", "ceiling": 12.0,
               "price": 10.0, "at": _ago(5)}
    got = _run_script("static/journal.js", _journal(
        events, positions=_position(_row(verdict=verdict))))
    assert "Проблем нет" in got["attention"]


def test_a_refusal_on_an_order_no_longer_held_is_not_a_problem():
    events = [_event(1, "raise", ok=False, detail="HTTP 500")]
    got = _run_script("static/journal.js", _journal(events))
    assert "Проблем нет" in got["attention"]


def test_one_cause_failing_many_orders_is_one_line():
    """Forty raises refused by one limit read as forty lines on top of the
    page, and the one line that mattered was lost among them."""
    events = [_event(i, "raise", ok=False, name=f"Item {i} (FT)",
                     detail="лимит CSFloat")
              for i in range(1, 9)]
    rows = [_row(id=i, item=f"Item {i} (FT)") for i in range(1, 9)]
    got = _run_script("static/journal.js", _journal(
        events, positions=_position(*rows)))
    assert "8 ордер(ов): не поднят" in got["attention"]
    assert got["attention"].count("Item ") == 8, "the orders are listed under it"


def test_the_journal_reports_what_the_comparison_found():
    got = _run_script("static/journal.js", {
        "events": [], "held": 0, "manual": 1, "items": [],
        "defend": False, "defend_minutes": 60, "defend_at": None,
        "sync_pending": False, "sync_at": "2026-09-20T10:00:00",
        "sync": {"seen": 1, "error": "",
                 "counts": {"gone": 4, "filled": 0, "repriced": 0,
                            "adopted": 1, "matched": 0, "duplicate": 2}},
    })
    assert "4 ушло с сайта" in got["sync"]
    assert "1 не наших" in got["sync"]
    assert "2 повторных записей убрано" in got["sync"]


def test_the_journal_says_so_when_nothing_has_happened():
    got = _run_script("static/journal.js",
                      {"events": [], "held": 0, "items": [], "defend": False,
                       "defend_minutes": 60, "defend_at": None})
    assert "Ошибка" not in got["bar"], got["bar"]
    assert got["orderGroups"] == 0
    assert "ордеров нет" in got["ordersText"]
    assert "Пока ничего не происходило" in got["journalText"]
    assert "выключена" in got["defence"]


def test_outbid_orders_come_first():
    rows = [_row(id=1, item="A (FT)"),
            _row(id=2, item="B (FT)", float_min=0.2, float_max=0.22,
                 first=False, ahead=2, top=11.0)]
    got = _run_script("static/journal.js", _journal([], positions=_position(*rows)))
    text = got["ordersText"]
    assert text.index("B (FT)") < text.index("A (FT)")
    assert "перебит: впереди 2" in text


def test_an_order_shows_its_ceiling_and_what_comes_next():
    got = _run_script("static/journal.js", _journal(
        [_event(1, "place")], defend=True, positions=_position()))
    assert "потолок $12.00" in got["ordersText"]
    assert "запас $2.00" in got["ordersText"]
    assert "поднимем максимум до $12.00" in got["ordersText"]


def test_an_order_outbid_past_its_ceiling_says_it_will_not_answer():
    """Outbid, and the bot does nothing - is that a decision or a miss? The
    card says which, and it is not raised as a problem: it is a decision."""
    pos = _position(first=False, ahead=1, top=12.5, verdict={
        "kind": "keep", "at": _ago(3), "ceiling": 12.0, "price": 10.0,
        "reason": "перебили до $12.50, ответ $12.51 выше потолка $12.00 — "
                  "стоим позади, ордер сохраняем"})
    got = _run_script("static/journal.js", _journal(
        [_event(1, "place")], defend=True, positions=pos))
    assert "не перебиваем, стоим позади" in got["ordersText"]
    assert "стоим позади, ордер сохраняем" in got["ordersText"], \
        "and the defence's own last verdict is shown"
    assert "Проблем нет" in got["attention"]


def test_an_order_at_its_ceiling_is_marked_as_such():
    got = _run_script("static/journal.js", _journal(
        [_event(1, "place", price=12.0)], defend=True,
        positions=_position(price=12.0, room=0.0)))
    assert "на потолке" in got["ordersText"]
    assert "выше не пойдём" in got["ordersText"]


def test_the_tiles_say_what_the_orders_add_up_to_against_the_allowance():
    pos = _position(_row(price=10.0, quantity=3))
    pos.update({"face": 7100.0, "allowance": 7000.0})
    got = _run_script("static/journal.js", _journal([], positions=pos))
    assert "$7100.00" in got["tilesText"]
    assert "лимит CSFloat $7000.00" in got["tilesText"]


def test_the_weakest_order_is_named_with_why():
    pos = _position(_row(first=False, ahead=2, top=11.0))
    got = _run_script("static/journal.js", _journal([], positions=pos, weakest={
        "order": "A (FT) …", "item": "A (FT)", "float_min": 0.15,
        "float_max": 0.17, "price": 10.0, "quantity": 2, "rank": 0.0012,
        "withdrawn": False, "reason": "", "held": 40, "next_rank": 0.0019}))
    text = got["weakest"]
    assert "Самый слабый ордер" in text and "A (FT) 0.1500–0.1700" in text
    assert "ранг 0.0012" in text and "следующий 0.0019" in text
    assert "×2" in text and "перебит" in text and "из 40" in text


def test_orders_taken_down_to_make_room_are_counted_and_filterable():
    ev = _event(1, "cancel", source="room", reason="освободить лимит CSFloat")
    got = _run_script("static/journal.js", _journal([ev], positions=_position()))
    assert "снято ради места" in got["tilesText"]
    assert "ради места" in got["journalText"], "the source is named in the history"
