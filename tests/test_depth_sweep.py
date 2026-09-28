"""Filling in the prices the `asks` column was added too late to catch.

Bands written before it hold a count and a minimum, and the pricing reads that
minimum as the whole queue. The sweep that fixes them has to be resumable -
CSFloat will stop it partway - and it must not spend quota re-reading books it
has already priced.
"""
import logging
import os
import tempfile

import pytest

from src.depth_sweep import needs_prices, pick, sweep


def _db():
    logging.disable(logging.WARNING)
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    from src.db import Database

    return Database(os.environ["CSFLOAT_DB_PATH"])


def band(listings, asks, lo=0.15, hi=0.17):
    return {"float_min": lo, "float_max": hi, "listings": listings,
            "cheapest": asks[0][0] if asks else None, "asks": asks,
            "median_age_days": None, "oldest_days": None,
            "offerable": 0, "best_offer": None}


# -- which items are missing prices ----------------------------------------

def test_a_band_that_counted_lots_but_kept_no_prices_is_the_old_shape():
    assert needs_prices([band(50, [])])


def test_a_band_with_no_lots_is_not_missing_anything():
    """A float range nobody sells in has no prices to store. Reading it again
    would cost a request to learn the same nothing."""
    assert not needs_prices([band(0, [])])


def test_an_item_with_prices_is_left_alone():
    assert not needs_prices([band(2, [[52.09, 0.165], [53.4, 0.168]])])


def test_one_stale_band_among_priced_ones_still_calls_for_a_sweep():
    """The sweep reads the whole item, so a single band short is enough."""
    assert needs_prices([band(2, [[52.09, 0.165]]), band(50, [], 0.17, 0.19)])


def test_an_item_never_swept_at_all_needs_reading():
    assert needs_prices([])


# -- choosing the targets --------------------------------------------------

def test_the_unpriced_are_chosen_and_the_priced_are_told_why_not():
    d = _db()
    from src.depth import depth_profile

    old = d.add_item("Old | Book (Field-Tested)")
    new = d.add_item("New | Book (Field-Tested)")
    d.conn.execute(
        "INSERT INTO listing_depth (item_id, fetched_at, float_min, float_max,"
        " listings, cheapest) VALUES (?,?,?,?,?,?)",
        (old, "2026-09-01T00:00:00+00:00", 0.15, 0.17, 50, 52.09))
    d.conn.commit()
    # The whole wear range, as a finished sweep leaves it.
    d.record_listing_depth(new, depth_profile(
        [{"id": "L1", "price": 52.09, "float": 0.165, "type": "buy_now",
          "created_at": "2026-09-01T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.38)))

    wanted, skipped = pick(d, ["Old | Book (Field-Tested)",
                               "New | Book (Field-Tested)"])
    assert [n for n, _ in wanted] == ["Old | Book (Field-Tested)"]
    assert skipped and "цены уже есть" in skipped[0][1]
    d.close()


def test_an_untracked_name_is_reported_rather_than_swept():
    """Sweeping it would create nothing to store the result against, and
    silently dropping it is how a typo becomes 'the bot ignored my item'."""
    d = _db()
    wanted, skipped = pick(d, ["Nothing | Here (Field-Tested)"])
    assert wanted == []
    assert skipped == [("Nothing | Here (Field-Tested)", "не отслеживается")]
    d.close()


def test_force_takes_the_priced_ones_too():
    d = _db()
    from src.depth import depth_profile

    item = d.add_item("New | Book (Field-Tested)")
    d.record_listing_depth(item, depth_profile(
        [{"id": "L1", "price": 52.09, "float": 0.165, "type": "buy_now",
          "created_at": "2026-09-01T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.38)))
    wanted, skipped = pick(d, ["New | Book (Field-Tested)"], force=True)
    assert len(wanted) == 1 and skipped == []
    d.close()


# -- the run ---------------------------------------------------------------

class FakeClient:
    def __init__(self, pause=60.0):
        self.pause = pause

    def cooldown_remaining(self):
        return self.pause

    class _Pool:
        def wait_seconds(self):
            return 0.0

    pool = _Pool()


class FakeCollector:
    """Answers per item; a list of answers is consumed one call at a time, so
    an item can be refused and then succeed on the retry."""

    def __init__(self, results, pause=60.0):
        self.results = results
        self.asked = []
        self.client = FakeClient(pause)

    def sweep_listing_depth(self, name, item_id, start=None):
        self.asked.append((name, start))
        out = self.results.get(name, {"bands": 2, "listings": 9, "requests": 2})
        if isinstance(out, list):
            out = out.pop(0) if len(out) > 1 else out[0]
        if isinstance(out, Exception):
            raise out
        return out


def test_a_rate_limit_stops_the_run_instead_of_burning_it_item_by_item():
    """The next item would be refused too. A hundred refusals spend the whole
    reset window learning that once per item."""
    col = FakeCollector({"B": {"bands": 1, "listings": 3, "requests": 1,
                               "rate_limited": True, "error": "лимит"}})
    out = sweep(col, [("A", 1), ("B", 2), ("C", 3)])
    assert [n for n, _ in col.asked] == ["A", "B"], "C was not attempted"
    assert "запусти ещё раз позже" in out["stopped"]
    assert out["requests"] == 3, "what it did spend is still counted"


def test_one_item_blowing_up_does_not_lose_the_rest():
    col = FakeCollector({"A": RuntimeError("boom")})
    out = sweep(col, [("A", 1), ("B", 2)])
    assert [n for n, _ in col.asked] == ["A", "B"]
    assert out["failed"][0][0] == "A" and "boom" in out["failed"][0][1]
    assert [n for n, _ in out["swept"]] == ["B"]


def test_an_item_that_read_no_bands_is_a_failure_not_a_success():
    """A wearless item answers with zero bands and no error worth raising;
    counting it as swept would hide it from the next run's retry."""
    col = FakeCollector({"A": {"bands": 0, "listings": 0, "requests": 0,
                               "error": "без износа в названии"}})
    out = sweep(col, [("A", 1)])
    assert out["swept"] == []
    assert out["failed"] == [("A", "без износа в названии")]


def test_the_totals_add_up_across_items():
    col = FakeCollector({})
    out = sweep(col, [("A", 1), ("B", 2), ("C", 3)])
    assert out["requests"] == 6 and out["listings"] == 27
    assert len(out["swept"]) == 3 and out["stopped"] == ""


def test_the_caller_hears_about_each_item_as_it_goes():
    """A three hundred item run that prints nothing until the end is a run you
    cannot tell from a hang."""
    seen = []
    sweep(FakeCollector({}), [("A", 1), ("B", 2)],
          lambda name, result: seen.append((name, result["bands"])))
    assert seen == [("A", 2), ("B", 2)]


# -- what it buys ----------------------------------------------------------

def test_after_the_sweep_the_queue_is_priced_off_the_survivor():
    """The point of the whole exercise: with only the minimum stored, one
    cheap lot priced the exit for a band of fifty."""
    from src.ladder import Params, evaluate

    sales = [{"price": 110.0, "float_value": 0.165} for _ in range(40)]
    sales += [{"price": 96.0, "float_value": 0.165} for _ in range(4)]
    p = Params(window_days=16.0)

    before = evaluate(0.17, sales, [], (0.15, 0.38), lots=40,
                      asks=[[95.0, 0.165]], params=p)
    after = evaluate(0.17, sales, [], (0.15, 0.38), lots=40,
                     asks=[[95.0, 0.165]] + [[108.0, 0.165]] * 39, params=p)
    assert after.queue_price > before.queue_price
    assert after.ceiling > before.ceiling


# -- running it on the server ----------------------------------------------

def _tool():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "tools" / "sweep_depth.py"
    spec = importlib.util.spec_from_file_location("sweep_depth_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_missing_dependency_names_the_interpreter_to_use_instead(
        monkeypatch, capsys, tmp_path):
    """Ubuntu has no `python` and its `python3` is the system one, so a server
    run ends in `No module named 'dotenv'` - which says nothing about which
    interpreter would have worked."""
    import builtins

    tool = _tool()
    real = builtins.__import__

    def missing(name, *a, **kw):
        if name == "src.config":
            raise ModuleNotFoundError("No module named 'dotenv'", name="dotenv")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", missing)
    root = os.path.dirname(os.path.dirname(os.path.abspath(tool.__file__)))
    venv = os.path.join(root, ".venv", "bin", "python")
    os.makedirs(os.path.dirname(venv), exist_ok=True)
    open(venv, "a").close()
    try:
        with pytest.raises(SystemExit) as exit:
            tool.project_imports()
    finally:
        os.remove(venv)

    assert exit.value.code == 1
    err = capsys.readouterr().err
    assert "dotenv" in err, "say which module, not just that one is missing"
    assert ".venv/bin/python" in err and "tools/sweep_depth.py" in err


def test_without_an_environment_it_says_where_to_look_for_one(
        monkeypatch, capsys):
    """Guessing a path that does not exist is worse than saying so: the
    service file is the one place that knows what actually runs the bot."""
    import builtins

    tool = _tool()
    real = builtins.__import__

    def missing(name, *a, **kw):
        if name == "src.config":
            raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", missing)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    with pytest.raises(SystemExit):
        tool.project_imports()
    assert "systemctl cat csfloat-collector" in capsys.readouterr().err


# -- waiting out the limit -------------------------------------------------

def test_with_patience_it_waits_and_resumes_where_it_stopped():
    """The pause was one minute and the run gave up on it, having read nothing.
    Resuming from the float it stopped on keeps the bands already stored from
    being bought a second time."""
    col = FakeCollector({"A": [
        {"bands": 3, "listings": 5, "requests": 3, "rate_limited": True,
         "stopped_at": 0.21, "error": "лимит"},
        {"bands": 8, "listings": 40, "requests": 8},
    ]}, pause=60.0)
    slept = []
    out = sweep(col, [("A", 1), ("B", 2)], patience=600.0, sleep=slept.append)

    assert slept == [60.0], "it waited exactly the pause the client named"
    assert col.asked == [("A", None), ("A", 0.21), ("B", None)]
    assert out["requests"] == 13 and out["listings"] == 54
    assert out["stopped"] == "", "the run finished"


def test_patience_is_bounded_so_a_run_cannot_turn_into_a_hang():
    """The pauses escalate - 1, 2, 4 minutes and up - so an unbounded wait is
    indistinguishable from a hang."""
    col = FakeCollector({"A": {"bands": 1, "listings": 2, "requests": 1,
                               "rate_limited": True, "stopped_at": 0.17}},
                        pause=300.0)
    slept = []
    out = sweep(col, [("A", 1)], patience=120.0, sleep=slept.append)
    assert slept == [], "300s of waiting does not fit in 120s of patience"
    assert "запусти ещё раз позже" in out["stopped"]
    assert out["requests"] == 1, "what it did read is still counted"


def test_a_refused_item_is_not_also_filed_as_failed():
    """It is coming back to this item, either after the wait or on the next
    run. Listing it as failed reads as 'this one is broken'."""
    col = FakeCollector({"A": {"bands": 0, "listings": 0, "requests": 0,
                               "rate_limited": True, "error": "лимит"}})
    out = sweep(col, [("A", 1)], patience=0.0)
    assert out["failed"] == []
    assert "запусти ещё раз позже" in out["stopped"]


def test_the_wait_is_the_longer_of_the_account_pause_and_the_route_park():
    """Two clocks, parked separately. Waiting the shorter one walks straight
    back into the limit."""
    col = FakeCollector({})
    col.client.pause = 30.0
    col.client.pool = type("P", (), {"wait_seconds": lambda self: 90.0})()
    from src.depth_sweep import cooldown
    assert cooldown(col) == 90.0


def test_a_client_that_cannot_say_still_yields_a_sane_wait():
    """A collector without the clocks must not end the run with an
    AttributeError."""
    from src.depth_sweep import cooldown
    assert cooldown(object()) == 5.0
