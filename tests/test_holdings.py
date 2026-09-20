"""The account is the authority, not our own record of it.

Orders placed by the bot were cancelled by hand from the site. Nothing in
our_orders changed, so the dashboard went on reporting four standing orders
that did not exist, the capital limit went on reserving money that was free,
and the defence went on tending positions that were gone.
"""
from src.holdings import (ADOPTED, FILLED, GONE, MATCHED, REPRICED,
                          reconcile_holdings, summary)
from src.placement import parse_order_list


def ours(remote_id="r1", price=150.0, lo=0.15, hi=0.17, name="A (FT)"):
    return {"id": 1, "remote_id": remote_id, "price": price, "float_min": lo,
            "float_max": hi, "market_hash_name": name, "ceiling": 160.0}


def theirs(remote_id="r1", price=150.0, lo=0.15, hi=0.17, name="A (FT)",
           bought=0):
    return {"remote_id": remote_id, "price": price, "float_min": lo,
            "float_max": hi, "market_hash_name": name, "bought": bought}


def test_an_order_cancelled_by_hand_is_noticed():
    """The whole point. Four orders were placed, all four taken down from the
    site, and the bot reported four standing."""
    got = reconcile_holdings([ours("r1"), ours("r2")], [])
    assert [c.kind for c in got] == [GONE, GONE]
    assert "снят вручную или исполнен" in got[0].detail


def test_what_agrees_is_left_alone():
    got = reconcile_holdings([ours()], [theirs()])
    assert [c.kind for c in got] == [MATCHED]


def test_a_fill_is_not_a_disappearance():
    got = reconcile_holdings([ours()], [theirs(bought=1)])
    assert got[0].kind == FILLED and "1 шт" in got[0].detail


def test_a_price_we_did_not_write_down_is_reported_not_overwritten_silently():
    got = reconcile_holdings([ours(price=150.0)], [theirs(price=152.0)])
    assert got[0].kind == REPRICED
    assert "$152.00" in got[0].detail and "$150.00" in got[0].detail


def test_an_order_placed_by_hand_is_seen_rather_than_ignored():
    got = reconcile_holdings([], [theirs("x9")])
    assert got[0].kind == ADOPTED and got[0].name == "A (FT)"


def test_an_order_without_an_id_of_ours_matches_on_its_band():
    """Placed before ids were recorded, or one whose reply never arrived.
    Calling it gone would withdraw a position that is standing."""
    got = reconcile_holdings([ours(remote_id="")], [theirs("site-id")])
    assert [c.kind for c in got] == [MATCHED]


def test_two_orders_on_different_bands_do_not_match_each_other():
    got = reconcile_holdings([ours(remote_id="", lo=0.15, hi=0.17)],
                             [theirs("z", lo=0.20, hi=0.22)])
    assert sorted(c.kind for c in got) == [ADOPTED, GONE]


def test_the_tally_is_what_the_page_shows():
    got = reconcile_holdings([ours("r1"), ours("r2", lo=0.2, hi=0.22)],
                             [theirs("r1"), theirs("r9", lo=0.3, hi=0.32)])
    assert summary(got) == {GONE: 1, FILLED: 0, REPRICED: 0, ADOPTED: 1,
                            MATCHED: 1}


ORDER = {"id": "1021510122612067461", "qty": 1, "price": 630,
         "market_hash_name": "AK-47 | Crane Flight (Battle-Scarred)",
         "hybrid_properties": {"min_float": 0.605, "max_float": 1},
         "bought_item_count": 0}


def test_a_list_is_read_whatever_it_is_wrapped_in():
    """Reading one shape and treating the others as empty would report "you
    hold nothing" for an account that holds plenty - the answer that makes the
    bot place everything a second time."""
    for payload in (
        [ORDER],
        {"data": [ORDER]},
        {"orders": [ORDER]},
        {"results": [ORDER], "count": 1},
        ORDER,
    ):
        got = parse_order_list(payload)
        assert len(got) == 1, payload
        assert got[0]["remote_id"] == ORDER["id"]
        assert got[0]["price"] == 6.30

    for empty in (None, {}, [], {"data": []}, "nonsense", {"error": "no"}):
        assert parse_order_list(empty) == []


def test_our_own_order_is_not_a_rival_to_itself():
    """The book read off a listing is the public one and our orders are in it.
    Left there, an order alone in its band reads as having one rival at
    exactly its own price: the wait to fill doubles and the defence answers an
    outbid that never happened."""
    from src.holdings import strip_own

    book = [{"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17},
            {"price": 90.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    mine = [{"price": 100.0, "float_min": 0.15, "float_max": 0.17}]

    left = strip_own(book, mine)
    assert [r["price"] for r in left] == [90.0]


def test_only_as_many_entries_are_removed_as_we_hold():
    from src.holdings import strip_own

    book = [{"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17},
            {"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    left = strip_own(book, [{"price": 100.0, "float_min": 0.15,
                             "float_max": 0.17}])
    assert len(left) == 1, "one order held, one entry removed"


def test_an_entry_standing_for_several_orders_loses_only_ours():
    """CSFloat groups equal orders, so a qty of four at our price is us plus
    three other people."""
    from src.holdings import strip_own

    book = [{"price": 100.0, "qty": 4, "float_min": 0.15, "float_max": 0.17}]
    left = strip_own(book, [{"price": 100.0, "float_min": 0.15,
                             "float_max": 0.17}])
    assert len(left) == 1 and left[0]["qty"] == 3


def test_a_different_price_or_a_different_band_is_someone_else():
    from src.holdings import strip_own

    book = [{"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    assert len(strip_own(book, [{"price": 100.1, "float_min": 0.15,
                                 "float_max": 0.17}])) == 1
    assert len(strip_own(book, [{"price": 100.0, "float_min": 0.20,
                                 "float_max": 0.22}])) == 1


def test_holding_nothing_leaves_the_book_alone():
    from src.holdings import strip_own

    book = [{"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    assert strip_own(book, []) == book
