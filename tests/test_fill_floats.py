from src.fill_floats import match, summary


def test_purchases_are_placed_inside_the_range_of_the_order_that_made_them():
    events = [
        {"ok": 1, "dry": 0, "kind": "place", "market_hash_name": "A",
         "float_min": 0.15, "float_max": 0.25, "price": 40.0},
        {"ok": 1, "dry": 0, "kind": "raise", "market_hash_name": "A",
         "float_min": 0.15, "float_max": 0.25, "price": 41.0},
        {"ok": 1, "dry": 1, "kind": "place", "market_hash_name": "A",
         "float_min": 0.15, "float_max": 0.17, "price": 50.0},     # dry: ignored
        {"ok": 1, "dry": 0, "kind": "place", "market_hash_name": "A",
         "float_min": 0.15, "float_max": 0.16, "price": 41.0},     # narrower, same price
    ]
    buys = [
        {"market_hash_name": "A", "float_value": 0.245, "price": 41.0},   # wide, near top
        {"market_hash_name": "A", "float_value": 0.155, "price": 41.0},   # narrow wins
        {"market_hash_name": "A", "float_value": 0.20, "price": 40.0},    # first price
        {"market_hash_name": "A", "float_value": 0.165, "price": 50.0},   # only a dry run
        {"market_hash_name": "B", "float_value": 0.2, "price": 41.0},     # not ours
    ]
    rows = match(buys, events, [], {})
    assert len(rows) == 3
    by_float = {r["float"]: r for r in rows}
    assert by_float[0.245]["position"] == 0.95 or abs(by_float[0.245]["position"] - 0.95) < 1e-9
    assert by_float[0.245]["top_hundredth"]
    assert (by_float[0.155]["lo"], by_float[0.155]["hi"]) == (0.15, 0.16)
    assert abs(by_float[0.20]["position"] - 0.5) < 1e-9

    s = summary(rows)
    assert s["all"]["count"] == 3 and s["wide"]["count"] == 2 and s["narrow"]["count"] == 1
    assert s["wide"]["deciles"][9] == 1 and s["wide"]["deciles"][5] == 1
    assert summary([])["all"] == {"count": 0}


def test_our_orders_fill_in_when_the_journal_is_gone():
    orders = [{"item_id": 7, "float_min": 0.07, "float_max": 0.09, "price": 30.0}]
    rows = match([{"market_hash_name": "C", "float_value": 0.0899, "price": 30.0}],
                 [], orders, {7: "C"})
    assert len(rows) == 1 and rows[0]["top_hundredth"]
