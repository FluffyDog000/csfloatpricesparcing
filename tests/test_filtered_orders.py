"""Orders for particular skins - a pattern, stickers - are not rivals for
the ordinary skin a seller hands us."""
from src.ladder import competes, plain, rival_bid
from src.orders import parse_orders


def test_an_order_with_more_than_a_float_filter_is_marked():
    rows = parse_orders({"data": [
        {"price": 18100, "qty": 1, "hybrid_properties": {"paint_seed": [661, 670]}},
        {"price": 19800, "qty": 1, "expression": "PaintSeed == 387"},
        {"price": 4700, "qty": 1, "hybrid_properties": {"float_value": {"min": 0.15, "max": 0.25}}},
        {"price": 4650, "qty": 1, "hybrid_properties": {}},
    ]})
    by_price = {r["price"]: r for r in rows}
    assert by_price[181.0]["filtered"] and by_price[198.0]["filtered"]
    assert not by_price[47.0]["filtered"] and not by_price[46.5]["filtered"]


def test_filtered_and_far_dearer_bids_do_not_count_as_rivals():
    book = [{"price": 181.0, "float_min": None, "float_max": None, "filtered": True},
            {"price": 198.0, "float_min": None, "float_max": None},       # filter unseen
            {"price": 47.0, "float_min": 0.15, "float_max": 0.38},
            {"price": 46.5, "float_min": None, "float_max": None},
            {"price": 45.0, "float_min": None, "float_max": None}]
    assert not competes(book[0])
    assert rival_bid(book, 0.30) == 198.0, "an unflagged outlier needs plain()"
    kept = plain(book)
    assert [o["price"] for o in kept] == [47.0, 46.5, 45.0]
    assert rival_bid(kept, 0.30) == 47.0
    assert [o["price"] for o in plain(book, market=52.0)] == [47.0, 46.5, 45.0]
    assert len(plain(book[:2] + book[2:3])) == 2, "two bids: no median to judge by"


def test_the_defence_stands_first_when_only_a_pattern_order_is_above():
    from src.executor import Limits, reconcile, KEEP, RAISE
    from src.pricing import Band
    band = Band(float_min=0.15, float_max=0.37, market=52.0, ceiling=47.5, bid=47.0,
                lam=0.1, take=True)
    rows = [{"id": 1, "float_min": 0.15, "float_max": 0.37, "price": 47.5,
             "ceiling": 47.5, "remote_id": "r"}]
    book = [{"price": 181.0, "float_min": None, "float_max": None, "qty": 1},
            {"price": 46.0, "float_min": None, "float_max": None, "qty": 1},
            {"price": 45.5, "float_min": None, "float_max": None, "qty": 1},
            {"price": 45.0, "float_min": None, "float_max": None, "qty": 1}]
    acts = reconcile("Hydra", [band], rows, book, Limits())
    assert acts and "выше потолка" not in acts[0].reason, acts[0].reason
