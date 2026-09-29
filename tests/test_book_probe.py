"""One listing at the bottom of the wear range, against the whole book.

An order is scoped to a float range and a listing shows every order covering
it, so a listing at the bottom reveals every order that starts there. If most
do, the buy-order sweep collapses from a request per 0.01 band to one.

The question is not what share of orders is missed but whether the bid would
change: an order never seen costs nothing while a dearer one covers the same
float anyway.
"""
import importlib.util
import pathlib


def _tool():
    path = pathlib.Path(__file__).resolve().parent.parent / "tools" / "book_probe.py"
    spec = importlib.util.spec_from_file_location("book_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def order(price, lo=None, hi=None):
    return {"price": price, "qty": 1, "float_min": lo, "float_max": hi}


def test_an_order_starting_at_the_wear_minimum_is_visible():
    tool = _tool()
    assert tool.covers(order(40.0, 0.15, 0.38), 0.155)


def test_an_order_scoped_higher_up_is_not():
    tool = _tool()
    assert not tool.covers(order(45.0, 0.25, 0.35), 0.155)


def test_an_order_with_no_float_scope_shows_on_every_listing():
    """It takes anything, so it is on the page whatever the lot's float."""
    tool = _tool()
    assert tool.covers(order(38.0), 0.155)
    assert tool.covers(order(38.0), 0.37)


def test_a_book_of_wide_orders_loses_nothing():
    """The case that would let the sweep drop to one request."""
    tool = _tool()
    orders = [order(40.0, 0.15, 0.38), order(39.0, 0.15, 0.18), order(38.0)]
    found = tool.check("AK-47 | Asiimov (Field-Tested)", orders, (0.15, 0.38))
    assert found["visible"] == 3
    assert found["wrong"] == [], "no rung would have been bid differently"


def test_a_hidden_order_is_reported_where_it_changes_the_bid():
    """And only there: an unseen order costs nothing on rungs a dearer one
    covers anyway."""
    tool = _tool()
    orders = [order(40.0, 0.15, 0.38), order(45.0, 0.25, 0.35)]
    found = tool.check("AK-47 | Asiimov (Field-Tested)", orders, (0.15, 0.38))

    assert len(found["missed"]) == 1
    tops = [row["top"] for row in found["wrong"]]
    assert min(tops) == 0.25 and max(tops) == 0.35, \
        "wrong exactly over the hidden order's own range"
    assert all(row["whole"] == 45.0 and row["partial"] == 40.0
               for row in found["wrong"])


def test_an_empty_book_is_not_a_clean_result():
    tool = _tool()
    found = tool.check("AK-47 | Asiimov (Field-Tested)", [], (0.15, 0.38))
    assert found["orders"] == 0 and found["wrong"] == []
