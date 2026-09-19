"""Applying an approved plan: what runs is what was approved.

The dashboard stores the actions it displayed rather than a request to
recompute them. A plan rebuilt a minute later against a moved book would be a
different plan, and nothing on screen would say so.
"""
import json
import logging
import os
import tempfile

logging.disable(logging.WARNING)


def _collector(dry_run=True, spec=None):
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database
    from src.placement import PLACEMENT_KEY, SUGGESTED

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    db.add_item("★ Gloves | Fade (Field-Tested)")
    db.set_setting(PLACEMENT_KEY,
                   json.dumps((spec or SUGGESTED).as_dict(), ensure_ascii=False))
    db.set_setting("analysis_dry_run", "1" if dry_run else "0")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    return col, db


def _queue(db, actions):
    db.set_setting("analysis_pending_actions",
                   json.dumps({"at": "now", "actions": actions},
                              ensure_ascii=False))


def _place(price=159.0):
    return {"kind": "place", "item": "★ Gloves | Fade (Field-Tested)",
            "float_min": 0.32, "float_max": 0.38, "price": price,
            "ceiling": 170.0, "reason": "тест", "order_id": None,
            "remote_id": None, "was": None}


def test_nothing_queued_means_nothing_happens():
    col, db = _collector()
    assert col.apply_pending_actions() is None
    db.close()


def test_a_dry_run_records_what_it_would_have_sent():
    col, db = _collector(dry_run=True)
    sent = []
    col.client.send_json = lambda *a, **k: sent.append(a) or {}
    _queue(db, [_place()])

    out = col.apply_pending_actions()
    assert out["dry_run"] and out["done"] == 1
    assert sent == [], "a dry run sends nothing"
    assert db.our_orders() == [], "and records no position"
    assert "вхолостую" in out["results"][0]["detail"]
    db.close()


def test_a_real_run_sends_once_and_records_the_position():
    col, db = _collector(dry_run=False)
    sent = []

    def fake(method, url, body=None, headers=None):
        sent.append((method, url, body))
        return {"id": "remote-1", "price": 15900, "qty": 1,
                "hybrid_properties": {"min_float": 0.32, "max_float": 0.38},
                "bought_item_count": 0}

    col.client.send_json = fake
    _queue(db, [_place()])
    out = col.apply_pending_actions()

    assert out["done"] == 1 and not out["dry_run"]
    assert len(sent) == 1 and sent[0][0] == "POST"
    assert sent[0][2]["max_price"] == 15900

    held = db.our_orders()
    assert len(held) == 1
    assert held[0]["remote_id"] == "remote-1" and held[0]["state"] == "live"
    assert held[0]["ceiling"] == 170.0, "the walk-away price is kept with it"
    db.close()


def test_the_queue_is_cleared_before_the_first_request():
    """A crash halfway must not leave a plan that runs again next pass."""
    col, db = _collector(dry_run=False)

    def explode(method, url, body=None, headers=None):
        assert db.get_setting("analysis_pending_actions") in ("", None), \
            "the plan is taken off the queue before anything is sent"
        raise RuntimeError("boom")

    col.client.send_json = explode
    _queue(db, [_place()])
    out = col.apply_pending_actions()

    assert out["failed"] == 1 and out["done"] == 0
    assert not db.get_setting("analysis_pending_actions")
    assert db.our_orders() == [], "a failed send records no position"
    db.close()


def test_an_unreadable_plan_is_dropped_rather_than_guessed_at():
    col, db = _collector()
    db.set_setting("analysis_pending_actions", "{not json")
    assert col.apply_pending_actions() is None
    assert not db.get_setting("analysis_pending_actions")
    db.close()


def test_cancelling_marks_our_row_rather_than_deleting_it():
    col, db = _collector(dry_run=False)
    col.client.send_json = lambda *a, **k: {}
    item_id = db.get_item_id("★ Gloves | Fade (Field-Tested)")
    db.upsert_our_order(item_id, 0.32, 0.38, 159.0, 170.0, state="live",
                        remote_id="remote-1")

    _queue(db, [dict(_place(), kind="cancel", remote_id="remote-1")])
    out = col.apply_pending_actions()

    assert out["done"] == 1
    assert db.our_orders(item_id) == [], "no longer live"
    rows = db.our_orders(item_id, live_only=False)
    assert rows[0]["state"] == "cancelled", "but the history is kept"
    db.close()
