"""The digest and the order alerts: read from the database, nothing sent
anywhere by them, and quiet when nothing is wrong."""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

logging.disable(logging.WARNING)


def _db():
    from src.db import Database
    return Database(os.path.join(tempfile.mkdtemp(), "t.db"))


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
        .replace(microsecond=0).isoformat()


def test_a_quiet_bot_has_nothing_to_report():
    from src import digest
    db = _db()
    assert digest.alerts(db) == {}
    text = digest.summary_text(db)
    assert "Сводка" in text and "Проблемы" in text and "нет" in text
    db.close()


def test_the_summary_says_balance_orders_and_the_day():
    from src import digest
    from src.db import utcnow_iso
    from src.settings import BALANCE_AT_KEY, BALANCE_KEY

    db = _db()
    db.set_setting(BALANCE_KEY, "630.00")
    db.set_setting(BALANCE_AT_KEY, utcnow_iso())
    item = db.add_item("AK-47 | Redline (Field-Tested)")
    db.upsert_our_order(item, 0.15, 0.16, 50.0, 60.0, state="live",
                        remote_id="r1", quantity=2)
    db.record_order_event(name="AK-47 | Redline (Field-Tested)", kind="place",
                          ok=True, dry=False, source="plan", item_id=item,
                          float_min=0.15, float_max=0.16, price=50.0, reason="")
    db.record_order_event(name="AK-47 | Redline (Field-Tested)", kind="raise",
                          ok=False, dry=False, source="defence", item_id=item,
                          float_min=0.15, float_max=0.16, price=51.0, reason="",
                          detail="insufficient balance")
    text = digest.summary_text(db)
    assert "$630.00" in text and "(с аккаунта)" in text
    assert "Ордеров: <b>1</b> на <b>$100.00</b>" in text
    assert "поставлено 1" in text and "отказов 1" in text
    db.close()


def test_amends_held_for_balance_raise_an_alert_after_a_while():
    from src import digest
    from src.collector import AMEND_HOLD_KEY

    db = _db()
    db.set_setting(AMEND_HOLD_KEY, json.dumps({"balance": 630, "face": 7000,
                                               "at": _ago(5)}))
    assert "balance" not in digest.alerts(db), "five minutes is weather"
    db.set_setting(AMEND_HOLD_KEY, json.dumps({"balance": 630, "face": 7000,
                                               "at": _ago(45)}))
    assert "не хватило баланса" in digest.alerts(db)["balance"]
    db.close()


def test_a_dead_main_proxy_is_one_alert_not_a_flood_of_refusals():
    from src import digest

    db = _db()
    item = db.add_item("A (FT)")
    for _ in range(25):
        db.record_order_event(name="A (FT)", kind="raise", ok=False, dry=False,
                              source="defence", item_id=item, float_min=0.1,
                              float_max=0.2, price=1.0, reason="",
                              detail="NoRouteAvailable: адреса главного ключа недоступны")
    got = digest.alerts(db)
    assert "route" in got and "refusals" not in got
    db.close()


def test_a_sync_failing_for_long_is_reported():
    from src import digest

    db = _db()
    db.set_setting("orders_sync_result", json.dumps({"error": "HTTP 500"}))
    db.set_setting("orders_sync_at", _ago(10))
    assert "sync" not in digest.alerts(db)
    db.set_setting("orders_sync_at", _ago(90))
    assert "HTTP 500" in digest.alerts(db)["sync"]
    db.close()


def test_the_daily_digest_goes_once_after_its_time():
    from src import digest

    db = _db()
    early = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)      # 18:00 MSK
    late = datetime(2026, 10, 5, 18, 30, tzinfo=timezone.utc)      # 21:30 MSK
    assert not digest.digest_due(db, early)
    assert digest.digest_due(db, late)
    digest.mark_sent(db, late)
    assert not digest.digest_due(db, late + timedelta(hours=1))
    db.set_setting(digest.DIGEST_TIME_KEY, "")
    assert not digest.digest_due(db, late + timedelta(days=1)), "off is off"
    db.close()


def test_summary_is_a_telegram_command():
    from src import tgcommands
    db = _db()
    assert tgcommands.known("/summary") and tgcommands.known("/s")
    assert "Сводка" in tgcommands.answer("/summary", db)
    assert "/summary" in tgcommands.HELP
    db.close()


def test_the_settings_page_reports_the_database_size():
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    import importlib

    import webapp
    importlib.reload(webapp)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    body = c.get("/api/settings/storage").get_json()
    assert body["db"] > 0 and body["disk_total"]
    assert any(t["table"] == "sales" for t in body["tables"])
    assert c.post("/api/settings", json={"digest_time": "25:00"}).status_code == 400
    assert c.post("/api/settings", json={"digest_time": "20:30"}).status_code == 200
    assert c.get("/api/settings").get_json()["digest"]["time"] == "20:30"
