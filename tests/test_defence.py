"""The hourly look at orders we already hold.

It may amend a position upward, never past the ceiling it was placed with, or
take it down. It may not open one: a loop that can also open positions is a
loop that can spend the whole budget while nobody is watching.
"""
import json
import logging
import os
import tempfile

logging.disable(logging.WARNING)


def _collector(dry_run=False):
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
    db.set_setting("analysis_dry_run", "1" if dry_run else "0")
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    return col, db


def _stock(db, name="★ Gloves | Fade (Field-Tested)", rival=None):
    """An item with a tight market, one order of ours, and a book."""
    import datetime as dt

    item_id = db.add_item(name)
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for i in range(12):
        price = 150.0 + (i % 3)
        rows.append((f"c{i}", item_id, name, int(price * 100), price, 0.36,
                     (now - dt.timedelta(days=i % 20)).isoformat()))
    for i in range(30):
        rows.append((f"m{i}", item_id, name, 20000, 200.0, 0.36,
                     (now - dt.timedelta(days=i % 20)).isoformat()))
    db.conn.executemany(
        "INSERT INTO sales (sale_id,item_id,market_hash_name,price_cents,price,"
        "float_value,sold_at,sold_at_estimated,scraped_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        [(a, b, c, d, e, f, g, g) for a, b, c, d, e, f, g in rows])
    db.conn.commit()
    db.replace_buy_orders(item_id, rival or [])
    return item_id, name


def _quiet_sweep(col):
    """The defence re-reads the book; here it already has one."""
    col.sweep_buy_orders = lambda name, item_id: {"bands": 0}


def test_nothing_held_means_nothing_to_defend():
    col, db = _collector()
    assert col.defend_orders() is None
    db.close()


def test_an_order_nobody_touched_is_left_alone():
    col, db = _collector()
    item_id, name = _stock(db)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: pytest_fail()

    out = col.defend_orders()
    assert out["actions"] == 0, "no rival, no action"
    db.close()


def pytest_fail():
    raise AssertionError("nothing should have been sent")


def test_a_slow_band_bid_over_is_answered_in_place():
    col, db = _collector()
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: sent.append((m, u, b)) or {}
    db.set_setting("an_patience", "0.5")     # any queue is worth answering

    out = col.defend_orders()
    kinds = [r["action"]["kind"] for r in out["results"]]
    assert kinds == ["raise"], kinds
    assert sent and sent[0][0] == "PATCH", "amended, not replaced"
    assert sent[0][1].endswith("/r1"), "the order keeps its identity"

    held = db.our_orders(item_id)[0]
    assert held["price"] == 154.0 and held["remote_id"] == "r1"
    db.close()


def test_a_rival_above_our_ceiling_takes_us_out():
    col, db = _collector()
    rival = [{"price": 191.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: sent.append((m, u)) or {}
    db.set_setting("an_patience", "0.5")

    out = col.defend_orders()
    assert [r["action"]["kind"] for r in out["results"]] == ["cancel"]
    assert sent[0][0] == "DELETE"
    assert db.our_orders(item_id) == [], "and we stop holding it"
    db.close()


def test_defence_never_opens_a_position():
    """Even with budget and qualifying bands, it only tends what is held."""
    col, db = _collector()
    item_id, name = _stock(db)
    db.set_setting("an_total_capital", "10000")
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: {}

    out = col.defend_orders()
    assert all(r["action"]["kind"] != "place" for r in out["results"])
    db.close()


def test_a_dry_run_changes_nothing():
    col, db = _collector(dry_run=True)
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda *a, **k: sent.append(a) or {}
    db.set_setting("an_patience", "0.5")

    out = col.defend_orders()
    assert out["dry_run"] and sent == []
    assert db.our_orders(item_id)[0]["price"] == 152.0, "position untouched"
    db.close()


def test_the_interval_is_bounded_and_off_by_default():
    from src.settings import defend_minutes, defending

    col, db = _collector()
    assert not defending(db), "automatic spending is opt-in"
    assert defend_minutes(db) == 60.0

    db.set_setting("an_defend_minutes", "1")
    assert defend_minutes(db) == 10.0, "a pass costs real quota"
    db.set_setting("an_defend_minutes", "99999")
    assert defend_minutes(db) == 1440.0
    db.close()
