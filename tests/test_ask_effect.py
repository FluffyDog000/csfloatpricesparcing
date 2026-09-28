"""Measuring what the lot prices buy, rather than asserting it.

The `asks` column was added on an argument: one seller undercutting the rest
prices the whole queue, so the exit price is too low and sound rungs get
rejected. This checks the measurement itself - including that it reports "no
difference" when there is none, which is the outcome that would send the
effort elsewhere.
"""
import os
import tempfile

import pytest

from src.ask_effect import blinded, compare, summarise
from src.ladder import Params


def sales(prices_by_float, days=14):
    out = []
    for f, prices in prices_by_float.items():
        for i, p in enumerate(prices):
            out.append({"price": p, "float_value": f,
                        "age_days": (i % days) + 0.5})
    return out


def band(lo, hi, asks, listings=None):
    return {"float_min": lo, "float_max": hi,
            "listings": listings if listings is not None else len(asks),
            "cheapest": min((a[0] for a in asks), default=None),
            "asks": [list(a) for a in asks]}


# -- the blinding ----------------------------------------------------------

def test_blinding_keeps_the_minimum_because_the_pricing_falls_back_to_it():
    """Dropping the list without keeping `cheapest` would measure what having
    no sell side at all does - a different and more flattering question."""
    out = blinded([band(0.15, 0.17, [(52.09, 0.165), (61.0, 0.168)])])
    assert out[0]["asks"] == []
    assert out[0]["cheapest"] == 52.09
    assert out[0]["listings"] == 2, "the count is what an old row still has"


def test_blinding_does_not_touch_the_original():
    rows = [band(0.15, 0.17, [(52.09, 0.165)])]
    blinded(rows)
    assert rows[0]["asks"] == [[52.09, 0.165]]


# -- the measurement -------------------------------------------------------

def test_a_lone_discounter_behind_a_deep_queue_shows_up_as_a_gain():
    """The case the column was added for: one cheap lot, many dearer ones
    behind it, and a queue too deep to clear during the lock."""
    history = sales({0.165: [110.0] * 20 + [96.0] * 4})
    depth = [band(0.15, 0.17, [(95.0, 0.165)] + [(108.0, 0.165)] * 39)]
    changes = compare(history, [], (0.15, 0.38), depth, Params(window_days=16.0))

    moved = [c for c in changes if abs(c.ceiling_gain) > 1e-9]
    assert moved, "the prices behind the discounter have to count for something"
    assert all(c.ceiling_gain > 0 for c in moved), \
        "seeing more of the queue cannot lower what we may pay"
    assert summarise(changes)["best_gain"] > 1.0


def test_a_queue_that_clears_during_the_lock_shows_no_difference():
    """The outcome that sends the effort elsewhere: with the standing lots all
    gone before we could list, they cap nothing either way."""
    history = sales({0.165: [110.0] * 30})
    depth = [band(0.15, 0.17, [(95.0, 0.165), (108.0, 0.165)])]
    changes = compare(history, [], (0.15, 0.38), depth, Params(window_days=16.0))
    assert summarise(changes)["moved"] == 0


def test_the_summary_counts_rungs_that_open_rather_than_only_prices():
    """A cent of ceiling that changes no decision is not the claim being made.
    Whether a rung becomes worth placing is."""
    history = sales({0.165: [110.0] * 20 + [96.0] * 4})
    depth = [band(0.15, 0.17, [(95.0, 0.165)] + [(108.0, 0.165)] * 39)]
    found = summarise(compare(history, [], (0.15, 0.38), depth,
                              Params(window_days=16.0)))
    assert found["take_after"] >= found["take_before"]
    assert found["opened"] >= 0 and found["closed"] == 0


def test_every_rung_is_paired_so_a_gain_is_like_for_like():
    """Both readings must price the same rungs: comparing a rung that only one
    of them produced would count a missing row as an improvement."""
    history = sales({0.165: [110.0] * 20, 0.175: [104.0] * 20})
    depth = [band(0.15, 0.17, [(108.0, 0.165)] * 30),
             band(0.17, 0.19, [(102.0, 0.175)] * 30)]
    changes = compare(history, [], (0.15, 0.38), depth, Params(window_days=16.0))
    tops = [c.top for c in changes]
    assert len(tops) == len(set(tops))
    assert all(c.lots >= 0 for c in changes)


def test_an_item_with_no_wear_range_yields_nothing_rather_than_guessing():
    assert compare(sales({0.16: [10.0] * 20}), [], None, [], Params()) == []


# -- why there was no difference -------------------------------------------

def test_a_null_result_says_which_of_the_two_reasons_it_was():
    """An unexplained null cannot be told apart from a measurement that never
    ran. The two reasons behave differently: a queue that empties during the
    lock caps nothing at any price, while one that stands but sits above the
    history median would start binding if the market cooled."""
    # Liquid: 30 sales in the band over 14 days clears any queue of two.
    history = sales({0.165: [110.0] * 30})
    depth = [band(0.15, 0.17, [(95.0, 0.165), (108.0, 0.165)])]
    found = summarise(compare(history, [], (0.15, 0.38), depth,
                              Params(window_days=16.0)))
    assert found["moved"] == 0
    assert found["queue_gone"] > 0, "the lock disposed of the queue"
    assert found["lots_cleared"] > found["lots"]


def test_a_queue_priced_above_the_median_is_not_a_queue_at_all():
    """It does not stand in our way: we list under it and the buyer reaches us
    first. It used to be counted as competition that the median then outbid,
    which is two ways of saying the same thing - and it inflated the count the
    page shows past the lots actually ahead of us."""
    history = sales({0.165: [60.0] * 12})
    depth = [band(0.15, 0.17, [(200.0, 0.165)] * 40)]
    found = summarise(compare(history, [], (0.15, 0.38), depth,
                              Params(window_days=16.0)))
    assert found["median_binds"] == 0
    assert found["queue_gone"] == found["rungs"], \
        "nothing is ahead of us, so there is no queue to clear"


def test_only_the_lots_under_the_median_are_counted_as_ahead_of_us():
    """Half the band dearer than the item is worth, half under it."""
    from src.ladder import Params as LadderParams
    from src.ladder import evaluate

    history = [{"price": 100.0, "float_value": 0.165, "age_days": i % 14 + 0.5}
               for i in range(20)]
    asks = [[80.0, 0.165]] * 6 + [[150.0, 0.165]] * 14
    rung = evaluate(0.17, history, [], (0.15, 0.38), lots=20, asks=asks,
                    params=LadderParams(window_days=16.0))
    assert rung.lots == 6, "the fourteen dearer lots are not ahead of us"
    assert all(p <= rung.market for p in rung.asks)
