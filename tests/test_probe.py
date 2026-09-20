"""The button that proves the amend request is read the way we think.

A captured request is still a guess about how the server reads it. The probe
settles it by making a real change: sending the price already standing looked
free, and that was its flaw - a body the server quietly ignores and a body it
obeys leave the order identical either way.
"""
import json
import logging
import os
import tempfile

logging.disable(logging.WARNING)

NAME = "★ Gloves | Fade (Field-Tested)"


def _collector():
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database
    from src.placement import PLACEMENT_KEY, SUGGESTED

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    db.set_setting(PLACEMENT_KEY,
                   json.dumps(SUGGESTED.as_dict(), ensure_ascii=False))
    return Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling)), db


def _order(db, price=39.70, ceiling=45.0):
    item_id = db.add_item(NAME)
    return item_id, db.upsert_our_order(item_id, 0.15, 0.17, price, ceiling,
                                        state="live", remote_id="r1")


def test_the_probe_moves_the_price_by_a_step():
    """$0.10 between $10 and $100, which is what the grid allows there."""
    col, db = _collector()
    item_id, oid = _order(db, price=39.70)
    sent = []

    def echo(method, url, body=None, headers=None):
        sent.append((method, url, body))
        return {"id": "r1", "price": body["max_price"], "qty": 1,
                "hybrid_properties": {"min_float": 0.15, "max_float": 0.17},
                "bought_item_count": 0}

    col.client.send_json = echo
    db.set_setting("amend_probe_request", str(oid))
    out = col.probe_amend()

    assert out["ok"] and out["confirmed"] is True
    assert out["was"] == 39.70 and out["price"] == 39.80
    assert sent[0][0] == "PATCH" and sent[0][2]["max_price"] == 3980
    assert sent[0][2]["min_float"] == 0.15, "the scope goes with it"


def test_our_record_follows_the_price_that_moved():
    """Left behind, the next reconciliation reports it as someone else's."""
    col, db = _collector()
    item_id, oid = _order(db, price=39.70)
    col.client.send_json = lambda m, u, b=None, h=None: {
        "id": "r1", "price": b["max_price"], "qty": 1,
        "hybrid_properties": {}, "bought_item_count": 0}
    db.set_setting("amend_probe_request", str(oid))
    col.probe_amend()

    assert db.our_orders(item_id)[0]["price"] == 39.80


def test_a_step_up_that_would_cross_the_ceiling_goes_down_instead():
    """The ceiling is the price above which the trade stops being worth doing,
    and a test is not a reason to cross it."""
    col, db = _collector()
    item_id, oid = _order(db, price=39.70, ceiling=39.70)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: (
        sent.append(b) or {"id": "r1", "price": b["max_price"], "qty": 1,
                           "hybrid_properties": {}, "bought_item_count": 0})
    db.set_setting("amend_probe_request", str(oid))
    out = col.probe_amend()

    assert out["price"] == 39.60 < out["was"]
    assert sent[0]["max_price"] == 3960


def test_a_refusal_leaves_our_record_where_it_was():
    import requests

    col, db = _collector()
    item_id, oid = _order(db, price=39.70)

    def refuse(*a, **k):
        raise requests.HTTPError('HTTP 400 — {"code":5}')

    col.client.send_json = refuse
    db.set_setting("amend_probe_request", str(oid))
    out = col.probe_amend()

    assert not out["ok"] and "400" in out["detail"]
    assert db.our_orders(item_id)[0]["price"] == 39.70


def test_the_probe_is_spent_by_use():
    """A request left standing would fire again on every collector pass."""
    col, db = _collector()
    item_id, oid = _order(db)
    col.client.send_json = lambda m, u, b=None, h=None: {
        "id": "r1", "price": b["max_price"], "qty": 1,
        "hybrid_properties": {}, "bought_item_count": 0}
    db.set_setting("amend_probe_request", str(oid))

    assert col.probe_amend() is not None
    assert col.probe_amend() is None, "asked once, run once"


def test_the_probe_is_written_to_the_journal():
    col, db = _collector()
    item_id, oid = _order(db)
    col.client.send_json = lambda m, u, b=None, h=None: {
        "id": "r1", "price": b["max_price"], "qty": 1,
        "hybrid_properties": {}, "bought_item_count": 0}
    db.set_setting("amend_probe_request", str(oid))
    col.probe_amend()

    event = db.order_events()[0]
    assert event["source"] == "проверка" and event["kind"] == "raise"
    assert event["was"] == 39.70 and event["price"] == 39.80
