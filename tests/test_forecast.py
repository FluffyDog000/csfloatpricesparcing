"""What a purchase was expected to sell for, kept until it sells."""
import json
import os
import tempfile

from src.db import Database
from src.forecast import exit_gross, snapshot
from src.pricing import Band, Params


def _band(**kw):
    base = dict(float_min=0.15, float_max=0.17, market=110.0, queue_price=107.0,
                exit_net=104.86, ceiling=99.8, bid=98.0, sample=12, t_sell=1.5,
                queue_lots=[[100.0, 0.161], [107.0, 0.165]])
    base.update(kw)
    return Band(**base)


def test_a_snapshot_keeps_the_queue_with_its_floats_and_the_settings():
    data = snapshot(_band(), Params(fee=0.02, queue_days=4.0))
    assert data["queue_lots"] == [[100.0, 0.161], [107.0, 0.165]]
    assert data["settings"]["queue_days"] == 4.0 and data["settings"]["fee"] == 0.02
    assert exit_gross(data) == 107.0
    assert "queue_lots" not in _band().as_dict(), "kept out of the page payload"
    json.dumps(data)


def test_forecasts_are_written_when_they_change_and_found_for_a_purchase():
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    item = db.add_item("A")
    data = snapshot(_band(), Params())
    kw = dict(order_id=1, item_id=item, name="A", float_min=0.15, float_max=0.17)
    assert db.record_forecast(price=98.0, data=data, **kw)
    assert not db.record_forecast(price=98.0, data=data, **kw), "nothing moved"
    assert db.record_forecast(price=99.0, data=data, **kw), "the price moved"
    moved = snapshot(_band(exit_net=100.0), Params())
    assert db.record_forecast(price=99.0, data=moved, **kw), "the exit moved"

    got = db.forecast_for("A", 0.166, 99.0)
    assert got and got["exit"] == exit_gross(moved)
    assert db.forecast_for("A", 0.166, 98.0)["data"]["queue_lots"]
    assert db.forecast_for("A", 0.20, 99.0) is None, "float outside the range"
    assert db.forecast_for("B", 0.166, 99.0) is None
    db.close()


def test_the_defence_writes_a_forecast_for_every_order_it_prices():
    from tests.test_defence import _collector, _quiet_sweep, _stock
    col, db = _collector()
    item_id, name = _stock(db)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: {}
    col.defend_orders()
    rows = db.conn.execute("SELECT * FROM forecasts").fetchall()
    assert len(rows) == 1 and rows[0]["market_hash_name"] == name
    data = json.loads(rows[0]["data"])
    assert "settings" in data and data["settings"]["fee"] is not None
    db.close()
