"""A held skin valued by the queue it will join, not the median alone."""
from datetime import datetime, timedelta, timezone

from src.holding_value import value_one

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _sales(n, price=50.0, f=0.163, days=30):
    return [{"price": price, "float_value": f, "age_days": i * days / n} for i in range(n)]


def _row(f=0.165, bought=45.0, days_ago=2):
    return {"float_value": f, "bought": bought,
            "bought_at": (NOW - timedelta(days=days_ago)).isoformat()}


def test_lots_cheaper_and_no_worse_stand_ahead_and_some_clear_before_the_unlock():
    sales = _sales(30)                                   # one a day in 0.16-0.17
    lots = [(46.0, 0.161), (47.0, 0.164), (48.0, 0.162), (49.0, 0.150),
            (45.0, 0.169),                               # worse float: not ahead
            (55.0, 0.160)]                               # dearer than the median
    out = value_one(_row(), sales, lots, "2026-10-06T10:00:00+00:00", 0,
                    0.02, 30.0, 4.0, NOW)
    assert out["median"] == 50.0 and out["ahead"] == 4
    assert out["unlock_days"] == 5.0
    assert out["cleared"] == 4.0, "four days of one a day, not the five left"
    assert out["queue_price"] is None and out["estimate"] == 50.0, "the queue clears"

    out = value_one(_row(), sales, lots, None, 0, 0.02, 30.0, 2.0, NOW)
    assert out["cleared"] == 2.0
    assert out["queue_price"] == 48.0 - 0.1, "a step under the first lot left"
    assert out["estimate"] == 47.9
    assert out["est_profit"] == round(47.9 * 0.98 - 45.0, 2)
    assert out["t_sell"] == 1.0


def test_our_own_skins_ahead_lengthen_the_wait_and_no_listings_fall_back():
    out = value_one(_row(), _sales(30), [], None, 2, 0.02, 30.0, 4.0, NOW)
    assert out["estimate"] == 50.0 and "не читались" in out["queue_note"]
    assert out["t_sell"] == 3.0, "two of ours first, then this one"


def test_too_few_sales_say_so():
    out = value_one(_row(), [], [(40.0, 0.16)], None, 0, 0.02, 30.0, 4.0, NOW)
    assert out["estimate"] is None and out["est_profit"] is None


def test_a_loss_is_told_once():
    import json
    from tests.test_defence import _collector
    from src import profit_report
    col, db = _collector()
    told = []
    col._tell = told.append
    real = profit_report.build
    profit_report.build = lambda *a, **k: {"holding": [
        {"trade_id": "t1", "market_hash_name": "A", "float_value": 0.16,
         "bought": 50.0, "estimate": 45.0, "est_profit": -5.9, "queue_note": "впереди 3"},
        {"trade_id": "t2", "market_hash_name": "B", "float_value": 0.2,
         "bought": 10.0, "estimate": 12.0, "est_profit": 1.76}]}
    try:
        assert len(col.check_holding_alerts(now=10_000.0)) == 1
        assert len(told) == 1 and "A float 0.1600" in told[0]
        assert col.check_holding_alerts(now=20_000.0) == [], "not twice"
        assert json.loads(db.get_setting(col.HOLD_ALERTED_KEY)) == ["t1"]
    finally:
        profit_report.build = real
    db.close()
