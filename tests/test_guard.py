"""The brake: a day that buys too much takes every order down.

On CSFloat the balance falls for one reason we control - orders filling - so
"the balance fell N% in a day" is measured as the fills the account sync saw.
A day that buys a third of the balance is sellers dumping into our orders or a
ceiling everyone has noticed is wrong; both are cases for stopping first.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from src.executor import Limits
from src.guard import TRIPPED_KEY, read

logging.disable(logging.WARNING)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _row(state, price, hours_ago, lo=0.15, hi=0.18):
    at = (NOW - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    return {"state": state, "price": price, "updated_at": at,
            "float_min": lo, "float_max": hi}


def test_only_the_last_days_purchases_count():
    rows = [_row("filled", 100.0, 2), _row("gone", 50.0, 20),
            _row("filled", 999.0, 30),           # yesterday's
            _row("cancelled", 999.0, 1),         # we took it down ourselves
            _row("live", 999.0, 1)]
    r = read(rows, Limits(balance=1000.0, guard_share=0.3), NOW)
    assert (r.spent, r.fills) == (150.0, 2)
    assert r.limit == 300.0 and not r.tripped


def test_a_third_of_the_balance_in_a_day_trips_it():
    rows = [_row("gone", 120.0, h) for h in (1, 5, 9)]
    r = read(rows, Limits(balance=1000.0, guard_share=0.3), NOW)
    assert r.tripped
    assert "36%" in r.reason() and "30%" in r.reason()


def test_without_a_balance_the_budget_stands_in():
    rows = [_row("gone", 120.0, 1)]
    r = read(rows, Limits(total_capital=400.0, guard_share=0.3), NOW)
    assert r.reference == 400.0 and r.tripped


def test_it_can_be_turned_off():
    rows = [_row("gone", 900.0, 1)]
    assert not read(rows, Limits(balance=1000.0, guard_share=0.0), NOW).tripped
    assert not read(rows, Limits(guard_share=0.3), NOW).tripped, \
        "no balance and no budget: nothing to measure against"


# -- through the collector --------------------------------------------------

def _setup():
    from tests.test_sync import NAME, _answers, _collector, _site_order

    col, db = _collector()
    db.set_setting("analysis_dry_run", "0")
    db.set_setting("an_balance", "1000")
    db.set_setting("an_guard_share", "0.3")
    item_id = db.add_item(NAME)
    for i in range(5):
        db.upsert_our_order(item_id, 0.15 + i / 100, 0.16 + i / 100, 150.0,
                            160.0, state="live", remote_id=f"r{i}")
    db.upsert_our_order(item_id, 0.40, 0.45, 90.0, 90.0, state="manual",
                        remote_id="hand")
    return col, db, NAME, _answers, _site_order


def test_the_brake_takes_the_bots_orders_down_and_disarms():
    col, db, name, answers, site = _setup()
    db.set_setting("analysis_armed", "1")
    db.set_setting("analysis_pending_actions", '{"actions": []}')
    sent = []
    col.client.send_json = lambda *a, **k: sent.append(a) or {}

    # Three of five filled and vanished: $450 against a $300 threshold.
    answers(col, {"data": [site("r3", lo=0.18, hi=0.19),
                           site("r4", lo=0.19, hi=0.20),
                           site("hand", price_cents=9000, lo=0.40, hi=0.45)]})
    col.sync_our_orders()

    state = json.loads(db.get_setting(TRIPPED_KEY))
    assert "за сутки исполнилось 3" in state["reason"]
    assert state["cancelled"] == 2 and state["failed"] == 0
    assert len(sent) == 2, "the two still standing, and nothing else"
    assert db.our_orders() == [], "both marked taken down"
    hand = [r for r in db.our_orders(live_only=False)
            if r["remote_id"] == "hand"]
    assert hand and all(r["state"] == "manual" for r in hand), \
        "an order placed by hand is the owner's"
    assert db.get_setting("analysis_armed") == "0"
    assert db.get_setting("analysis_pending_actions") == ""
    assert any(e["source"] == "guard" for e in db.order_events())
    db.close()


def test_once_tripped_it_stays_tripped_until_reset_by_hand():
    from src.guard import reset, tripped

    col, db, name, answers, site = _setup()
    col.client.send_json = lambda *a, **k: {}
    answers(col, {"data": []})
    col.sync_our_orders()
    assert tripped(db)

    # Nothing to do on the next pass, and a plan queued anyway is dropped.
    db.set_setting("analysis_pending_actions", json.dumps({"actions": [{
        "kind": "place", "item": name, "float_min": 0.15, "float_max": 0.2,
        "price": 10.0, "ceiling": 11.0, "reason": "test"}]}))
    assert col.apply_pending_actions() is None
    assert col.check_guard() is None

    reset(db)
    assert not tripped(db)
    db.close()


def test_the_page_refuses_to_queue_a_plan_while_it_is_on():
    import os
    from tests.test_analysis_page import _app

    c = _app([])
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.set_setting("analysis_armed", "1")
    db.set_setting(TRIPPED_KEY, json.dumps({"at": "x", "reason": "тест"}))
    db.close()

    r = c.post("/api/analysis/apply")
    assert r.status_code == 403
    plan = c.get("/api/analysis/plan").get_json()
    assert plan["guard"]["tripped"]["reason"] == "тест"

    assert not c.post("/api/analysis/guard",
                      json={"reset": True}).get_json()["tripped"]
