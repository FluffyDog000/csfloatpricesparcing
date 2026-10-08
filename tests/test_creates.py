"""The day's 200 creations: counted, respected by "apply", and spent by the
auto-fill on its own - best first, fresh books only."""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

logging.disable(logging.WARNING)


def _db():
    from src.db import Database
    return Database(os.path.join(tempfile.mkdtemp(), "t.db"))


def _placed(db, n, name="A (FT)"):
    for i in range(n):
        db.record_order_event(name=name, kind="place", ok=True, dry=False,
                              source="plan", float_min=0.1 + i / 1000,
                              float_max=0.2, price=1.0, reason="")


def test_the_bot_counts_its_own_creations_over_a_day():
    from src import creates

    db = _db()
    _placed(db, 3)
    db.record_order_event(name="A (FT)", kind="place", ok=False, dry=False,
                          source="plan", price=1.0, reason="")      # refused
    db.record_order_event(name="A (FT)", kind="place", ok=True, dry=True,
                          source="plan", price=1.0, reason="")      # rehearsal
    db.record_order_event(name="A (FT)", kind="raise", ok=True, dry=False,
                          source="defence", price=1.0, reason="")   # an amend
    got = creates.status(db)
    assert got["used"] == 3 and got["left"] == 197 and got["reset"]
    db.close()


def test_what_csfloat_said_wins_over_the_count():
    from src import creates

    db = _db()
    _placed(db, 3)
    later = (datetime.now(timezone.utc) + timedelta(hours=5)).timestamp()
    db.set_setting("main_key_state", json.dumps([
        {"kind": "create", "remaining": 0, "limit": 200, "reset": later}]))
    got = creates.status(db)
    assert got["left"] == 0
    db.close()


def test_the_limit_csfloat_reports_replaces_the_200():
    from src import creates

    db = _db()
    _placed(db, 3)
    later = (datetime.now(timezone.utc) + timedelta(hours=5)).timestamp()
    db.set_setting("main_key_state", json.dumps([
        {"kind": "create", "remaining": 1990, "limit": 2000, "reset": later}]))
    got = creates.status(db)
    assert got["limit"] == 2000 and got["left"] == 1990 and got["source"] == "csfloat"
    # A stale report (reset already past) is ignored: back to the 200.
    db.set_setting("main_key_state", json.dumps([
        {"kind": "create", "remaining": 1990, "limit": 2000,
         "reset": later - 86400}]))
    assert creates.status(db)["limit"] == 200
    assert creates.status(db)["source"] == "own"
    db.close()


def test_places_are_capped_best_first_and_nothing_else_is():
    from src.creates import cap_places

    acts = [{"kind": "cancel"}, {"kind": "raise"},
            {"kind": "place", "rank": 3}, {"kind": "place", "rank": 2},
            {"kind": "place", "rank": 1}]
    kept, deferred = cap_places(acts, 2)
    assert [a["kind"] for a in kept] == ["cancel", "raise", "place", "place"]
    assert [a.get("rank") for a in kept if a["kind"] == "place"] == [3, 2]
    assert deferred == 1


def _plan_app():
    from tests.test_analysis_page import _stocked
    c, name = _stocked()
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
    c.post("/api/analysis/placement",
           json=c.get("/api/analysis/placement").get_json()["suggested"])
    return c, name


def test_apply_places_no_more_than_the_creations_left():
    c, name = _plan_app()
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    _placed(db, 200, name="Other (FT)")
    db.close()
    plan = c.get("/api/analysis/plan").get_json()
    assert plan["creates"]["left"] == 0
    c.post("/api/analysis/arm", json={"armed": True})
    body = c.post("/api/analysis/apply", json={}).get_json()
    assert body["queued"] == 0 and body["deferred"] >= 1
    assert "после сброса" in body["note"]


def test_auto_fill_places_fresh_ones_and_sends_old_books_for_a_sweep():
    c, name = _plan_app()
    import webapp
    with webapp.app.app_context():
        db = webapp.get_db()
        assert webapp.auto_fill_once(db)["skipped"] == "выключено"
        db.set_setting(webapp.AUTO_FILL_KEY, "1")
        got = webapp.auto_fill_once(db)
        assert got["queued"] >= 1, got
        queued = json.loads(db.get_setting("analysis_pending_actions"))
        assert queued["source"] == "auto"
        assert {a["kind"] for a in queued["actions"]} == {"place"}
        assert webapp.auto_fill_once(db)["skipped"] == "в очереди уже есть план"

        # An old book: nothing placed, the item sent for a sweep instead.
        db.set_setting("analysis_pending_actions", "")
        item_id = db.get_item_id(name)
        db.conn.execute("UPDATE items SET orders_swept_at = ? WHERE id = ?",
                        ("2020-01-01T00:00:00+00:00", item_id))
        db.conn.commit()
        got = webapp.auto_fill_once(db)
        assert got["queued"] == 0 and got["swept"] == 1
        assert not db.get_setting("analysis_pending_actions")


def test_auto_fill_places_only_into_the_room_left_under_the_cap():
    """It sends no cancels, so the plan's places would go on top of every
    standing order - past the cap, and into the room the defence had just
    freed for an outbid one."""
    c, name = _plan_app()
    import webapp
    c.post("/api/analysis/params", json={"an_balance": "5000", "an_leverage": "6"})
    with webapp.app.app_context():
        db = webapp.get_db()
        db.set_setting(webapp.AUTO_FILL_KEY, "1")
        item_id = db.get_item_id(name)
        other = db.add_item("★ Driver Gloves | King Snake (Field-Tested)")
        db.upsert_our_order(other, 0.20, 0.25, 29900.0, 30000.0, state="live",
                            remote_id="full")
        got = webapp.auto_fill_once(db)
        assert got.get("queued", 0) == 0, got
        assert "нет места" in got.get("skipped", ""), got
        assert not db.get_setting("analysis_pending_actions")

        # Room made: the same order goes.
        db.set_our_order_state(db.our_orders(other)[0]["id"], "cancelled", "")
        assert webapp.auto_fill_once(db)["queued"] == 1
