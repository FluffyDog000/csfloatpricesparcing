"""Old logs and snapshots out, what is still read kept - whatever its age."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.db import Database


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .replace(microsecond=0).isoformat()


def _db():
    return Database(os.path.join(tempfile.mkdtemp(), "t.db"))


def _count(db, table, where="1=1", args=()):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def test_prune_keeps_what_is_read_and_drops_the_rest():
    db = _db()
    a = db.add_item("A")
    b = db.add_item("B")

    # poll_log: twelve old polls of A, two old of B, one fresh of A.
    for i in range(12):
        db.log_poll(item_id=a, market_hash_name="A", fetched_count=40,
                    new_count=1, overlap_count=39, status="ok")
    for i in range(2):
        db.log_poll(item_id=b, market_hash_name="B", fetched_count=40,
                    new_count=1, overlap_count=39, status="ok")
    db.conn.execute("UPDATE poll_log SET polled_at = ?", (_ago(5),))
    db.log_poll(item_id=a, market_hash_name="A", fetched_count=40,
                new_count=1, overlap_count=39, status="ok")

    # book_history: old and fresh.
    for when in (_ago(5), _ago(0.5)):
        db.conn.execute("INSERT INTO book_history (item_id, fetched_at, float_min, "
                        "float_max, top_price, orders, qty) VALUES (?, ?, 0.15, 0.16, 10, 1, 1)",
                        (a, when))

    # listing_depth: band 0.15 read three times long ago; band 0.17 read once
    # long ago (its newest reading, kept whatever its age).
    for when in (_ago(9), _ago(6), _ago(5)):
        db.conn.execute("INSERT INTO listing_depth (item_id, fetched_at, float_min, "
                        "float_max, listings) VALUES (?, ?, 0.15, 0.17, 3)", (a, when))
    db.conn.execute("INSERT INTO listing_depth (item_id, fetched_at, float_min, "
                    "float_max, listings) VALUES (?, ?, 0.17, 0.19, 3)", (a, _ago(20)))

    # order_events: a real place 10 days ago (kept: marks trades), a dry run
    # 10 days ago (gone), a real one 40 days ago (gone).
    for dry, age in ((False, 10), (True, 10), (False, 40)):
        db.record_order_event(name="A", kind="place", ok=True, dry=dry, source="plan")
        db.conn.execute("UPDATE order_events SET at = ? WHERE id = "
                        "(SELECT MAX(id) FROM order_events)", (_ago(age),))

    # our_orders: live from long ago (kept), cancelled 20 days ago (gone),
    # filled 5 days ago (kept: still inside the trade lock).
    for i, (state, age) in enumerate((("live", 60), ("cancelled", 20), ("filled", 5))):
        db.upsert_our_order(a, 0.15 + i * 0.01, 0.16 + i * 0.01, 10.0, 11.0,
                            state=state, remote_id=f"r{i}")
        db.conn.execute("UPDATE our_orders SET updated_at = ? WHERE remote_id = ?",
                        (_ago(age), f"r{i}"))
    db.conn.commit()

    removed = db.prune_history(2)
    assert removed["poll_log"] == 3, removed           # A keeps its newest 10, the fresh one among them
    assert _count(db, "poll_log", "item_id = ?", (a,)) == 10
    assert _count(db, "poll_log", "item_id = ?", (b,)) == 2, "B's last polls are its pacing"
    assert removed["book_history"] == 1
    assert removed["listing_depth"] == 2
    assert _count(db, "listing_depth", "float_min = 0.17") == 1, "a band's newest stays"
    assert len(db.listing_depth(a)) == 2
    assert removed["order_events"] == 2
    assert _count(db, "order_events", "dry = 0") == 1
    assert removed["our_orders"] == 1
    assert {r["state"] for r in db.our_orders(live_only=False)} == {"live", "filled"}

    assert db.prune_history(2) == {k: 0 for k in removed}, "nothing left to clear"
    before, after = db.vacuum()
    assert after <= before
    db.close()


def test_the_collector_prunes_hourly_and_on_request():
    from tests.test_defence import _collector
    col, db = _collector()
    db.set_setting("db_keep_days", "3")
    out = col.maintain_db(now=10_000.0)
    assert out and out["days"] == 3.0
    assert json.loads(db.get_setting("db_prune_result"))["days"] == 3.0
    assert col.maintain_db(now=10_600.0) is None, "not again within the hour"
    db.set_setting("db_prune_requested", "1")
    assert col.maintain_db(now=10_700.0) is not None, "asked for: at once"
    assert db.get_setting("db_prune_requested") == "0"
    db.set_setting("db_vacuum_requested", "1")
    col.maintain_db(now=10_800.0)
    res = json.loads(db.get_setting("db_vacuum_result"))
    assert "after" in res and db.get_setting("db_vacuum_requested") == "0"
    db.close()


def test_the_settings_page_sets_the_days_and_asks_for_housekeeping():
    from tests.test_analysis_page import _app
    c = _app([])
    body = c.get("/api/settings/storage").get_json()
    assert body["keep_days"] == 2.0
    assert any(t["table"] == "book_history" for t in body["tables"])
    r = c.post("/api/settings/storage", json={"keep_days": "50", "prune": True})
    assert r.get_json() == {"keep_days": 30.0, "prune_pending": True,
                            "vacuum_pending": False}
    body = c.get("/api/settings/storage").get_json()
    assert body["keep_days"] == 30.0 and body["prune_pending"]
