"""The status panel and the auto-sweep: what runs on its own, when it runs
next, and what is being read or sent right now."""
import json
import os
from datetime import datetime, timedelta, timezone

from tests.test_analysis_page import _stocked
from tests.test_analysis_js import _run_script


def _iso(minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)) \
        .isoformat(timespec="seconds")


def _db():
    import webapp
    return webapp, webapp.Database(os.environ["CSFLOAT_DB_PATH"])


def test_auto_sweep_sends_stale_books_and_leaves_fresh_ones():
    c, name = _stocked()
    webapp, db = _db()
    with webapp.app.app_context():
        db = webapp.get_db()
        assert webapp.auto_sweep_once(db) == {"skipped": "выключено"}
        db.set_setting(webapp.AUTO_SWEEP_KEY, "1")
        db.set_setting(webapp.AUTO_SWEEP_MIN_KEY, "60")
        # Fresh means "read within 90% of the interval": a book read 115
        # minutes ago is due on a two-hour round.
        seen = []
        real = webapp._sweep_not_needed

        def fresh(db_, item_id, name_, fresh_minutes=None):
            seen.append(fresh_minutes)
            return "свежий"
        webapp._sweep_not_needed = fresh
        try:
            out = webapp.auto_sweep_once(db)
        finally:
            webapp._sweep_not_needed = real
        assert out == {"queued": 0, "fresh": 1, "screened": 0}, out
        assert seen == [54.0]
        assert not db.pending_order_requests()

        # Never read (no listings stored at all): sent.
        out = webapp.auto_sweep_once(db)
        assert out["queued"] == 1, out
        assert [r["market_hash_name"] for r in db.pending_order_requests()] == [name]
        assert webapp.auto_sweep_once(db) == {"skipped": "предыдущий обход ещё идёт"}
        assert json.loads(db.get_setting(webapp.AUTO_SWEEP_RESULT_KEY))["skipped"]


def test_auto_fill_runs_right_after_an_auto_sweep_finishes():
    """Placing from the books just read, not from ones half an hour old."""
    c, name = _stocked()
    webapp, _ = _db()
    with webapp.app.app_context():
        db = webapp.get_db()
        db.set_setting(webapp.AUTO_SWEEP_KEY, "1")
        db.set_setting(webapp.AUTO_SWEEP_MIN_KEY, "120")
        db.set_setting(webapp.AUTO_FILL_KEY, "1")
        calls = []
        real_fill, real_sweep = webapp.auto_fill_once, webapp.auto_sweep_once
        webapp.auto_fill_once = lambda d: calls.append("fill") or {"queued": 0}
        webapp.auto_sweep_once = lambda d: calls.append("sweep") or {"queued": 3}
        try:
            now = 1_000_000.0
            webapp.automation_tick(db, now)
            assert calls == ["sweep", "fill"], "both due on the first tick"
            calls.clear()
            webapp.automation_tick(db, now + 600)
            assert calls == [], "the sweep is still running"

            db.set_setting("sweep_state", json.dumps(
                {"started_at": _iso(-5), "finished_at": datetime.fromtimestamp(
                    now + 700, timezone.utc).isoformat(), "total": 3, "done": 3}))
            # The tick that queued the sweep ran the fill before it: a sweep
            # queued after the last fill is what earns the early one.
            db.set_setting(webapp.AUTO_SWEEP_LAST_KEY, str(now + 100))
            webapp.automation_tick(db, now + 800)
            assert calls == ["fill"], calls
            calls.clear()
            webapp.automation_tick(db, now + 900)
            assert calls == [], "once per sweep, not every minute after it"
        finally:
            webapp.auto_fill_once, webapp.auto_sweep_once = real_fill, real_sweep


def test_the_sweep_interval_is_bounded():
    c, _ = _stocked()
    r = c.post("/api/analysis/arm", json={"auto_sweep": True, "auto_sweep_minutes": "5"})
    body = r.get_json()
    assert body["auto_sweep"] is True and body["auto_sweep_minutes"] == 30
    body = c.post("/api/analysis/arm", json={"auto_sweep_minutes": "90"}).get_json()
    assert body["auto_sweep_minutes"] == 90


def test_status_says_what_runs_and_when():
    c, name = _stocked()
    webapp, db = _db()
    db.set_setting("an_defend", "1")
    db.set_setting("an_defend_minutes", "10")
    db.set_setting("defend_last_at", _iso(-4))
    db.set_setting("placing_state", json.dumps(
        {"source": "auto", "started_at": _iso(-1), "finished_at": None,
         "total": 5, "done": 2, "ok": 2, "current": "ставлю X 0.1500–0.1600 за $10.00"}))
    db.request_orders(name)
    db.close()
    body = c.get("/api/bot_status").get_json()
    assert body["defence"]["on"] and body["defence"]["next_at"] > body["now"]
    assert body["placing"]["state"]["done"] == 2
    assert body["sweep"]["queued"] == 1 and body["sweep"]["queued_names"] == [name]
    assert body["books"]["items"] == 1
    assert body["creates"]["limit"] == 200

    text = _run_script("static/status.js", {"bot_status": body})["botStatus"]
    assert "Защита" in text and "каждые 10 мин" in text and "следующая через" in text
    assert "идёт (автодобор): 2 из 5" in text and "ставлю X" in text
    assert "в очереди 1" in text
    assert "Автообход стаканов" in text and "выключен" in text


def test_sweep_progress_is_written_as_it_goes():
    import run_collector
    webapp, db = _db()
    p = run_collector.SweepProgress(db, ["A", "B"])
    p.start("A")
    state = json.loads(db.get_setting("sweep_state"))
    assert state["total"] == 2 and state["current"] == ["A"] and not state["finished_at"]
    p.finish("A", {})
    p.close({"swept": {"A": {}, "B": {}}, "failed": {}})
    state = json.loads(db.get_setting("sweep_state"))
    assert state["done"] == 2 and state["current"] == [] and state["finished_at"]
    db.close()


def test_held_items_have_their_listings_read_every_few_hours():
    from tests.test_profit import parsed, trade
    c, name = _stocked()
    webapp, _ = _db()
    with webapp.app.app_context():
        db = webapp.get_db()
        for t in parsed(trade("1", "buy", 10000, name=name, at="2026-10-01T10:00:00Z")):
            db.upsert_trade(t)
        item_id = db.get_item_id(name)
        db.conn.execute("UPDATE items SET orders_swept_at = ? WHERE id = ?",
                        (_iso(-300), item_id))
        db.conn.commit()
        assert webapp.hold_sweep_once(db) == {"queued": 1, "held": 1}
        assert [r["market_hash_name"] for r in db.pending_order_requests()] == [name]
        db.conn.execute("UPDATE items SET orders_requested_at = NULL, "
                        "orders_swept_at = ? WHERE id = ?", (_iso(-30), item_id))
        db.conn.commit()
        assert webapp.hold_sweep_once(db)["queued"] == 0, "read half an hour ago"
        db.set_setting("profit_hold_sweep_hours", "0")
        assert webapp.hold_sweep_once(db) == {"skipped": "выключено"}
