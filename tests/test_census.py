"""Counting the orders that exist across the whole database.

The number decides whether there is a business here, so the way it is added up
matters more than the arithmetic. An item nobody has swept has no rivals on
record: the pricing sees an empty book and bids the whole ceiling. That number
is not wrong so much as unearned, and adding it to the rest answers the
question with mostly fiction.
"""
import pytest

from src.census import Item, bucket, census, look
from src.pricing import Params


def sales(prices_by_float, days=14):
    out = []
    for f, prices in prices_by_float.items():
        for i, p in enumerate(prices):
            out.append({"price": p, "float_value": f,
                        "age_days": (i % days) + 0.5})
    return out


# -- grouping the refusals -------------------------------------------------

def test_each_refusal_lands_in_the_bucket_you_would_act_on():
    assert bucket("мало продаж у верха: 3 при пороге 8") == "истории не хватает"
    assert bucket("перебить стоит $99.00, а маржа позволяет лишь $90.00") == \
        "перебить дороже, чем позволяет маржа"
    assert bucket("по $48.00 никто не продавал") == \
        "по нашей цене никто не продавал"
    assert bucket("весь поток забрали ступени выше") == \
        "поток уже занят ступенью выше"
    assert bucket("") == "принята"


def test_an_unrecognised_refusal_is_counted_rather_than_dropped():
    """A reason nobody bucketed still has to show up in the total, or the
    percentages quietly stop summing to the rungs."""
    assert bucket("что-то новое") == "прочее"


# -- the division that matters ---------------------------------------------

def test_items_with_no_book_are_counted_apart_never_added_in():
    """With no rivals on record the bid is the whole ceiling. Totalling that
    with measured items would report competition nobody checked for."""
    seen = Item(name="Measured", orders=12, rungs=5, taken=2, capital=100.0)
    unseen = Item(name="Unswept", orders=0, rungs=5, taken=4, capital=900.0)
    found = census([seen, unseen])

    assert found["measured"]["capital"] == 100.0
    assert found["blind"]["capital"] == 900.0
    assert found["measured"]["items"] == 1 and found["blind"]["items"] == 1


def test_concentration_says_whether_it_is_a_portfolio_or_one_bet():
    """Twenty rungs in one item fill together when that market moves; they are
    one bet in pieces, and the split has to be visible."""
    one = Item(name="A", orders=5, taken=9, capital=900.0)
    two = Item(name="B", orders=5, taken=1, capital=100.0)
    assert census([one, two])["measured"]["concentration"] == 0.9


def test_an_item_that_failed_to_score_is_named_not_silently_dropped():
    bad = Item(name="Sticker | Fnatic", orders=3, error="без износа в названии")
    found = census([bad])
    assert found["failed"] == [("Sticker | Fnatic", "без износа в названии")]
    assert found["measured"]["items"] == 0, "and it is not counted as measured"


# -- scoring one item ------------------------------------------------------

def test_an_item_is_scored_with_its_refusals_counted():
    history = sales({0.165: [110.0] * 12 + [96.0] * 6})
    item = look("AWP | Printstream (Field-Tested)", history, [], [],
                (0.15, 0.38), Params(window_days=16.0))
    assert item.rungs > 0
    assert sum(item.reasons.values()) == item.rungs, \
        "every rung is accounted for, taken or refused"
    assert item.sales == len(history)


def test_a_wearless_item_is_reported_rather_than_scored():
    item = look("Sticker | Fnatic", sales({0.16: [10.0] * 20}), [], [],
                None, Params())
    assert item.error and item.rungs == 0


def test_the_capital_is_the_sum_of_what_the_taken_rungs_would_cost():
    history = sales({0.165: [110.0] * 12 + [96.0] * 6})
    item = look("AWP | Printstream (Field-Tested)", history, [], [],
                (0.15, 0.38), Params(window_days=16.0))
    if item.taken:
        assert item.capital > 0 and item.best_rank > 0
    else:
        assert item.capital == 0.0
