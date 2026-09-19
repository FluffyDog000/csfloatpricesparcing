"""Does the page script actually run?

A browser error in a dashboard script is invisible from Python: the page
renders, the endpoints answer, and the only symptom is a button that appears
to do nothing. That is exactly how a missing common.js shipped. These run the
script under node against a stub DOM and the real API response.
"""
import json
import os
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
