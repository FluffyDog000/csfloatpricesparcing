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
    # The funnel above the per-item panels is a section too: one summary,
    # one report.
    assert result["sections"] == 2, "the funnel and the item's own report"
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
    # A band missing the fields the row renderer reads.
    body["items"][0]["bands"][0] = {"float_min": 0.15, "float_max": 0.17,
                                    "take": True}
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
                 "settings.js"):
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
