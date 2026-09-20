"""Where each standing order actually stands.

The plan says what would be done, the journal says what was done; neither
answers "am I still first". That needs the book read against each order's own
float range, because a rival whose range merely overlaps ours takes the same
lots.
"""
import json
import logging
import os
import tempfile

logging.disable(logging.WARNING)

NAME = "★ Gloves | Fade (Field-Tested)"


def _app():
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    import importlib

    import webapp
    importlib.reload(webapp)
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _db():
    import webapp
    return webapp.Database(os.environ["CSFLOAT_DB_PATH"])


def test_an_order_nobody_has_outbid_reads_as_first():
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 90.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}])
    db.close()

    body = c.get("/api/analysis/positions").get_json()
    assert body["outbid"] == 0
    row = body["orders"][0]
    assert row["first"] and row["ahead"] == 0
    assert row["top"] == 90.0, "the best rival, which is not us"


def test_a_rival_whose_range_merely_overlaps_still_counts():
    """It takes the same lots, so it is ahead of us whatever its own bounds."""
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 105.0, "qty": 2, "float_min": 0.16, "float_max": 0.30}])
    db.close()

    body = c.get("/api/analysis/positions").get_json()
    assert body["outbid"] == 1
    row = body["orders"][0]
    assert not row["first"] and row["ahead"] == 2 and row["top"] == 105.0


def test_an_order_below_a_rival_outside_its_band_is_still_first():
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 400.0, "qty": 1, "float_min": 0.30, "float_max": 0.38}])
    db.close()

    assert c.get("/api/analysis/positions").get_json()["outbid"] == 0


def test_orders_placed_by_hand_are_shown_but_marked():
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 100.0, state="manual",
                        remote_id="x9", note="поставлен вручную")
    db.close()

    rows = c.get("/api/analysis/positions").get_json()["orders"]
    assert len(rows) == 1 and rows[0]["state"] == "manual"


def test_cancelled_orders_are_not_positions():
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    oid = db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                              remote_id="r1")
    db.set_our_order_state(oid, "gone", "снят вручную")
    db.close()

    assert c.get("/api/analysis/positions").get_json()["orders"] == []


def test_refresh_queues_a_book_read_for_every_item_we_hold():
    """Looking is not the same decision as answering, so it must not be the
    defence - which also acts - that is the only way to find out."""
    c = _app()
    db = _db()
    for name in (NAME, "AK-47 | Redline (Field-Tested)"):
        item_id = db.add_item(name)
        db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                            remote_id=f"r{item_id}")
    db.close()

    body = c.post("/api/analysis/positions/refresh", json={}).get_json()
    assert sorted(body["queued"]) == sorted(
        [NAME, "AK-47 | Redline (Field-Tested)"])

    db = _db()
    pending = {r["market_hash_name"] for r in db.pending_order_requests()}
    db.close()
    assert pending == set(body["queued"]), "the collector does the fetching"


def test_an_order_the_book_names_is_matched_by_id():
    """Every order the bot places records the id CSFloat answered with. If the
    book gives ids too, ours is pointed at rather than guessed at - and a
    rival standing at our exact price stops being mistakable for it."""
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"id": "r1", "price": 100.0, "qty": 1,
         "float_min": 0.15, "float_max": 0.17},
        {"id": "z9", "price": 100.0, "qty": 1,
         "float_min": 0.15, "float_max": 0.17}])
    db.close()

    body = c.get("/api/analysis/positions").get_json()
    row = body["orders"][0]
    assert row["seen_in_book"], "ours is named in the book"
    assert not row["first"], "and the other one at our price is a real rival"
    assert row["ahead"] == 0 or row["top"] == 100.0
    assert body["book_named"] == 2 and body["book_rows"] == 2


def test_a_book_without_ids_says_so():
    c = _app()
    db = _db()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.15, 0.17, 100.0, 120.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}])
    db.close()

    body = c.get("/api/analysis/positions").get_json()
    assert body["book_named"] == 0 and body["book_rows"] == 1
    assert not body["orders"][0]["seen_in_book"]


def test_an_older_database_gains_the_column_rather_than_failing():
    """buy_orders predates the id, so an install upgrading into this has a
    table without the column."""
    import sqlite3

    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE buy_orders (item_id INTEGER, price REAL, "
                 "qty INTEGER, float_min REAL, float_max REAL, "
                 "paint_seed INTEGER, position INTEGER, fetched_at TEXT)")
    conn.commit()
    conn.close()

    from src.db import Database
    db = Database(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(buy_orders)")}
    assert "order_id" in cols
    db.close()
