"""Is how fast the book moves being recorded anywhere?

buy_orders holds one snapshot per item and replace_buy_orders deletes the
previous one, so the top of a band could be outbid five times a day and the
database would look identical. That rate is what decides whether an order can
be defended: at 3% margin a Fade order at $315 has $0.34 of room before the
trade is dead, which is one outbid by a dollar.
"""


def test_a_band_is_valued_by_every_order_that_competes_for_it():
    from src.orders import book_profile

    orders = [
        {"price": 244.0, "qty": 1, "float_min": 0.15, "float_max": 0.18},
        {"price": 169.0, "qty": 2, "float_min": None, "float_max": None},
        {"price": 180.0, "qty": 1, "float_min": 0.30, "float_max": 0.38},
    ]
    bands = {(b["float_min"], b["float_max"]): b
             for b in book_profile(orders, (0.15, 0.38), step=0.05)}

    # The top of the book is $244, but it is scoped to floats no 0.22 lot can
    # satisfy, so a seller there is offered the unscoped $169 instead.
    assert bands[(0.2, 0.25)]["top_price"] == 169.0
    assert bands[(0.15, 0.2)]["top_price"] == 244.0

    # An order starting exactly where a band ends competes for the next band.
    assert bands[(0.25, 0.3)]["top_price"] == 169.0
    assert bands[(0.3, 0.35)]["top_price"] == 180.0

    assert bands[(0.3, 0.35)]["qty"] == 3, "both overlapping orders counted"


def test_a_book_with_no_float_scoping_still_profiles():
    from src.orders import book_profile

    orders = [{"price": 100.0, "qty": 1, "float_min": None, "float_max": None}]
    assert book_profile(orders, None) == []       # nothing to measure against
    assert book_profile([], (0.15, 0.38)) != []   # an empty book is a data point
    assert all(b["top_price"] is None for b in book_profile([], (0.15, 0.38)))


def test_successive_sweeps_accumulate_instead_of_overwriting():
    """The regression: the second sweep used to erase the first."""
    import os
    import tempfile

    from src.db import Database

    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    item_id = db.add_item("★ Specialist Gloves | Fade (Field-Tested)")

    first = [{"float_min": 0.15, "float_max": 0.17, "top_price": 313.0,
              "orders": 2, "qty": 2}]
    db.record_book_profile(item_id, first)
    db.conn.execute("UPDATE book_history SET fetched_at = ? WHERE item_id = ?",
                    ("2026-09-18T07:00:00+00:00", item_id))
    db.conn.commit()

    db.record_book_profile(item_id, [dict(first[0], top_price=318.0)])

    rows = db.book_history(item_id)
    assert len(rows) == 2, "both sweeps kept, so the movement is visible"
    assert [r["top_price"] for r in rows] == [313.0, 318.0]

    moved = rows[-1]["top_price"] - rows[0]["top_price"]
    assert moved == 5.0, "$5 of outbidding is what a Fade order cannot absorb"
    db.close()


def test_a_sweep_records_the_profile_it_swept():
    import logging
    import os
    import tempfile

    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    name = "★ Specialist Gloves | Fade (Field-Tested)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    listings = {"data": [{"id": f"L{i}", "item": {"float_value": 0.16 + i * 0.05}}
                         for i in range(4)]}

    def fake(url, headers=None):
        if "/buy-orders" in url:
            return {"data": [{"price": 31300, "qty": 1}]}
        return listings

    col.client.fetch_json = fake
    col.sweep_buy_orders(name, item_id)

    rows = db.book_history(item_id)
    assert rows, "a sweep that stored a book must store its profile too"
    # Field-Tested spans 0.15-0.38, so the profile covers the wear, not just
    # the bands that happened to have a lot in them.
    assert min(r["float_min"] for r in rows) == 0.15
    assert max(r["float_max"] for r in rows) == 0.38
    assert len({r["fetched_at"] for r in rows}) == 1, "one sweep, one timestamp"
    db.close()
