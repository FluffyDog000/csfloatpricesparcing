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
    d.record_listing_depth(new, depth_profile(
        [{"id": "L1", "price": 52.09, "float": 0.165, "type": "buy_now",
          "created_at": "2026-09-01T00:00:00+00:00", "min_offer_price": None}],
        (0.15, 0.17), step=0.02))

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
        (0.15, 0.17), step=0.02))
    wanted, skipped = pick(d, ["New | Book (Field-Tested)"], force=True)
    assert len(wanted) == 1 and skipped == []
    d.close()


# -- the run ---------------------------------------------------------------

class FakeCollector:
    def __init__(self, results):
        self.results = results
        self.asked = []

    def sweep_listing_depth(self, name, item_id):
        self.asked.append(name)
        out = self.results.get(name, {"bands": 2, "listings": 9, "requests": 2})
        if isinstance(out, Exception):
            raise out
        return out


def test_a_rate_limit_stops_the_run_instead_of_burning_it_item_by_item():
    """The next item would be refused too. A hundred refusals spend the whole
    reset window learning that once per item."""
    col = FakeCollector({"B": {"bands": 1, "listings": 3, "requests": 1,
                               "rate_limited": True, "error": "лимит"}})
    out = sweep(col, [("A", 1), ("B", 2), ("C", 3)])
    assert col.asked == ["A", "B"], "C was not attempted"
    assert "запусти ещё раз позже" in out["stopped"]
    assert out["requests"] == 3, "what it did spend is still counted"


def test_one_item_blowing_up_does_not_lose_the_rest():
    col = FakeCollector({"A": RuntimeError("boom")})
    out = sweep(col, [("A", 1), ("B", 2)])
    assert col.asked == ["A", "B"]
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
