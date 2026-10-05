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
    col.read_balance = lambda: None      # no account to ask in a test
    return col, db


def _stock(db, name="★ Gloves | Fade (Field-Tested)", rival=None):
    """An item with a tight market, one order of ours, and a book."""
    import datetime as dt

    item_id = db.add_item(name)
    now = dt.datetime.now(dt.timezone.utc)
    # Floats sit in the last 0.01 before the orders' top: an order is priced
    # from the lots it will actually be handed, which are the ones at its
    # upper edge, so sales parked in the middle price nothing.
    rows = []
    for i in range(12):
        price = 150.0 + (i % 3)
        rows.append((f"c{i}", item_id, name, int(price * 100), price,
                     0.371 + (i % 5) * 0.001,
                     (now - dt.timedelta(days=i % 20)).isoformat()))
    for i in range(30):
        rows.append((f"m{i}", item_id, name, 20000, 200.0,
                     0.371 + (i % 7) * 0.001,
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
    db.set_setting("an_patience_min", "720")     # any queue is worth answering

    out = col.defend_orders()
    kinds = [r["action"]["kind"] for r in out["results"]]
    assert kinds == ["raise"], kinds
    assert sent and sent[0][0] == "PATCH", "amended, not replaced"
    assert sent[0][1].endswith("/r1"), "the order keeps its identity"

    held = db.our_orders(item_id)[0]
    assert held["price"] == 154.0 and held["remote_id"] == "r1"
    db.close()


def test_a_rival_above_our_ceiling_does_not_take_us_out():
    """Outbid past the ceiling: the order stays, behind. Taking it down cost
    one of the day's 200 creates to put it back."""
    col, db = _collector()
    rival = [{"price": 191.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: sent.append((m, u)) or {}
    db.set_setting("an_patience_min", "720")

    out = (col.defend_orders() or {}).get("results", [])
    assert "cancel" not in [r["action"]["kind"] for r in out]
    assert all(m != "DELETE" for m, _ in sent)
    assert len(db.our_orders(item_id)) == 1, "still standing"
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
    db.set_setting("an_patience_min", "720")

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


def test_patience_set_in_days_is_not_read_as_minutes(tmp_path):
    """The field changed unit. A database written before that holds days under
    the old key, and reading 14 as minutes would turn "wait a fortnight" into
    "wait a quarter of an hour" - a live position bid to its ceiling by the
    next defence pass, with nothing on screen having changed."""
    from src.db import Database
    from src.settings import limits

    db = Database(str(tmp_path / "old.db"))
    db.set_setting("an_patience", "14")
    assert limits(db).patience_minutes == 14 * 1440

    # Once the new field is saved, the old one stops being consulted.
    db.set_setting("an_patience_min", "45")
    assert limits(db).patience_minutes == 45


def test_the_journal_records_what_happened_and_when():
    """our_orders holds where a position stands: raising it overwrites the
    price and the reason with it. "Placed at 12:49, raised at 13:55 because
    someone outbid us" exists nowhere else, and it is the only record that
    says whether the bot is working at all."""
    col, db = _collector()
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda m, u, b=None, h=None: {}
    db.set_setting("an_patience_min", "720")

    col.defend_orders()

    events = db.order_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "raise" and ev["source"] == "defence"
    assert ev["market_hash_name"] == name and ev["item_id"] == item_id
    assert ev["price"] == 154.0 and ev["was"] == 152.0
    assert ev["remote_id"] == "r1" and ev["ok"] and not ev["dry"]
    assert "перебива" in ev["reason"], "why, not just what"
    assert ev["at"], "the time is the point of the record"
    db.close()


def test_a_rehearsal_is_logged_as_one():
    """A dry run that looked identical in the journal would be the worst kind
    of record: it reads as a position that exists."""
    col, db = _collector()
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    db.set_setting("an_patience_min", "720")
    db.set_setting("analysis_dry_run", "1")

    col.defend_orders()
    assert [e["dry"] for e in db.order_events()] == [True]
    assert db.order_events(include_dry=False) == []
    db.close()


def test_a_refusal_is_kept_too():
    """A journal that only records successes cannot answer "why is there no
    order" - the question it exists for."""
    import requests

    col, db = _collector()
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    db.set_setting("an_patience_min", "720")

    def refuse(*a, **k):
        raise requests.HTTPError("HTTP 400 — {\"code\":5}")

    col.client.send_json = refuse
    col.defend_orders()

    ev = db.order_events()[0]
    assert not ev["ok"] and "400" in ev["detail"]
    assert db.our_orders(item_id)[0]["price"] == 152.0, "and nothing was written"
    db.close()


def test_the_defence_does_not_count_itself_as_standing_above_itself():
    """Our order is in the public book. Left there, an order alone in its band
    reads as having one rival at exactly its own price - and the defence
    answers an outbid nobody made."""
    col, db = _collector()
    item_id, name = _stock(db, rival=[])
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    # The sweep reads the book back with our own order in it.
    db.replace_buy_orders(item_id, [
        {"price": 152.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}])
    col.sweep_buy_orders = lambda n, i: None
    db.set_setting("an_patience_min", "1")
    sent = []
    col.client.send_json = lambda *a, **k: sent.append(a) or {}

    out = col.defend_orders()
    kinds = [r["action"]["kind"] for r in (out or {}).get("results", [])]
    assert "raise" not in kinds, kinds
    assert sent == [], "nothing should have been sent"
    db.close()


def test_the_defence_refreshes_the_asks_it_prices_from():
    """The ceiling is built from the cheapest ask. Left to a sweep nobody runs
    except by hand, it goes stale and the ceiling stops moving with the
    market - so an order can sit above a ceiling that moved under it."""
    col, db = _collector()
    item_id, name = _stock(db, rival=[])
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    col.sweep_buy_orders = lambda n, i: None
    col.client.send_json = lambda *a, **k: {}

    asked = []

    def answer(url, headers=None):
        asked.append(url)
        if "me/buy-orders" in url:
            # The sync runs first; without our order in the reply it would be
            # marked gone and there would be nothing left to defend.
            return {"data": [{"id": "r1", "price": 15200, "qty": 1,
                              "market_hash_name": name,
                              "bought_item_count": 0,
                              "hybrid_properties": {"min_float": 0.35,
                                                    "max_float": 0.38}}]}
        return {"data": [{"id": "L1", "price": 15000, "type": "buy_now",
                          "created_at": "2026-09-10T00:00:00Z",
                          "item": {"float_value": 0.36}}]}

    col.client.fetch_json = answer
    col.defend_orders()

    assert any("min_float=0.35&max_float=0.38" in u for u in asked), asked
    stored = db.listing_depth(item_id)
    assert stored and stored[0]["cheapest"] == 150.0
    db.close()


def test_a_failure_reading_the_asks_does_not_abort_the_pass():
    """One half of the market being unreadable is not a reason to stop
    defending against the other."""
    import requests

    col, db = _collector()
    item_id, name = _stock(db, rival=[])
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    col.sweep_buy_orders = lambda n, i: None
    col.client.send_json = lambda *a, **k: {}

    def refuse(url, headers=None):
        raise requests.HTTPError("HTTP 429")

    col.client.fetch_json = refuse
    out = col.defend_orders()
    assert out is not None, "the pass still ran"
    db.close()


def test_the_defence_reads_the_books_of_its_items_at_the_same_time():
    """Forty-seven items one after another took twenty minutes - longer than
    the ten-minute interval - so the defence never stopped and an approved
    plan waited behind it for an hour."""
    import threading
    import time

    from src.keyring import KeyRing

    col, db = _collector()
    col.client.keyring = KeyRing([f"key-{i}" for i in range(8)], col.client.pool,
                                 spacing=0.0)
    col.sync_our_orders = lambda *a, **k: {}
    for i in range(6):
        item_id = (_stock(db)[0] if i == 0
                   else db.add_item(f"Item {i} (Field-Tested)"))
        db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                            remote_id=f"r{i}")
    seen = set()

    def slow(name, item_id):
        seen.add(threading.get_ident())
        time.sleep(0.3)
        return {"bands": 0}

    col.sweep_buy_orders = slow
    col.refresh_held_asks = lambda *a, **k: None
    col.client.send_json = lambda *a, **k: {}

    started = time.monotonic()
    col.defend_orders()
    assert time.monotonic() - started < 1.2, "six items, not six in a row"
    assert len(seen) > 1
    db.close()


def test_a_plan_approved_during_the_defence_goes_out_during_it():
    col, db = _collector()
    col.sync_our_orders = lambda *a, **k: {}
    item_id, _ = _stock(db)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: {}
    applied = []
    col.apply_pending_actions = lambda: applied.append(1)

    col.defend_orders()
    assert applied, "the queued plan is looked at between the items"
    db.close()


def test_the_defence_brings_an_order_down_when_the_rival_below_has_left():
    """Ours at $175, the only bid left at $150: first at $150.x, so every
    fill at $175 overpaid. Amended in place, like a raise."""
    col, db = _collector()
    rival = [{"price": 150.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 175.0, 190.0, state="live",
                        remote_id="r1", quantity=2)
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: sent.append((m, u, b)) or {}

    out = col.defend_orders()
    kinds = [r["action"]["kind"] for r in out["results"]]
    assert kinds == ["lower"], kinds
    assert sent and sent[0][0] == "PATCH" and sent[0][1].endswith("/r1")
    assert sent[0][2]["quantity"] == 2
    held = db.our_orders(item_id)[0]
    assert 150.0 < held["price"] < 152.0 and held["quantity"] == 2
    ev = [e for e in db.order_events() if e["kind"] == "lower"]
    assert ev and ev[0]["was"] == 175.0
    db.close()


def test_the_defence_keeps_its_verdict_on_an_order_it_left_alone():
    """Outbid and not answered is a decision, and nothing in the journal
    records it - no action, no event. The verdict is what lets the page say
    "standing behind, the answer is over the ceiling" instead of nothing."""
    from src.collector import VERDICTS_KEY

    col, db = _collector()
    rival = [{"price": 191.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=rival)
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: {}
    db.set_setting("an_patience_min", "720")

    col.defend_orders()
    order_id = str(db.our_orders(item_id)[0]["id"])
    verdict = json.loads(db.get_setting(VERDICTS_KEY))[order_id]
    assert verdict["kind"] == "keep"
    assert "стоим позади" in verdict["reason"]
    assert verdict["ceiling"] > 0 and verdict["at"]
    db.close()


def test_a_verdict_goes_with_its_order():
    from src.collector import VERDICTS_KEY

    col, db = _collector()
    item_id, name = _stock(db)
    db.set_setting(VERDICTS_KEY, json.dumps({"999": {"kind": "keep"}}))
    db.upsert_our_order(item_id, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    col.client.send_json = lambda *a, **k: {}
    col.defend_orders()
    kept = json.loads(db.get_setting(VERDICTS_KEY))
    assert "999" not in kept, "an order no longer held drops its verdict"
    assert str(db.our_orders(item_id)[0]["id"]) in kept
    db.close()


def test_after_moving_an_order_the_stored_book_no_longer_shows_its_old_price():
    """Brought down from $175 with the book read before: the snapshot still
    held us at $175, ours were matched at the new price, and the old entry
    read as a rival standing above us - the journal said "outbid" while we
    led, and a pass on that book would answer ourselves."""
    from src.holdings import strip_own

    col, db = _collector()
    book = [{"price": 175.0, "qty": 1, "float_min": 0.35, "float_max": 0.38},
            {"price": 150.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    item_id, name = _stock(db, rival=book)
    db.upsert_our_order(item_id, 0.35, 0.38, 175.0, 190.0, state="live",
                        remote_id="r1")
    _quiet_sweep(col)
    sent = []
    col.client.send_json = lambda m, u, b=None, h=None: sent.append(m) or {}

    col.defend_orders()
    assert sent == ["PATCH"]
    held = db.our_orders(item_id)
    prices = sorted(o["price"] for o in db.buy_orders(item_id))
    assert 175.0 not in prices, prices
    rivals = strip_own(db.buy_orders(item_id), held)
    assert [o["price"] for o in rivals] == [150.0], "only the real rival is left"

    sent.clear()
    col.defend_orders()                      # the same, unrefreshed book
    assert sent == [], "and the next pass does not answer itself"
    db.close()


def _two_orders(col, db):
    """A: outbid and worth answering. B: a weak order on another item, on a
    range with no sales near its top - nothing to rank it by."""
    rival = [{"price": 153.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}]
    a_item, _ = _stock(db, rival=rival)
    b_item = db.add_item("★ Driver Gloves | King Snake (Field-Tested)")
    db.upsert_our_order(a_item, 0.35, 0.38, 152.0, 190.0, state="live",
                        remote_id="A")
    db.upsert_our_order(b_item, 0.20, 0.25, 40.0, 45.0, state="live",
                        remote_id="B")
    _quiet_sweep(col)
    db.set_setting("an_patience_min", "720")
    return a_item, b_item


def test_an_outbid_order_gets_room_by_taking_down_a_weaker_one():
    """Fills shrank the balance, the allowance fell under what stood, and
    every raise came back "insufficient balance" for hours. A weaker order
    taken down makes the room, and the raise goes through."""
    col, db = _collector()
    a_item, b_item = _two_orders(col, db)
    calls = []

    def send(method, url, body=None, headers=None):
        calls.append((method, url.rsplit("/", 1)[-1]))
        if method == "PATCH" and ("DELETE", "B") not in calls:
            raise RuntimeError("HTTP 400 — insufficient balance")
        return {}

    col.client.send_json = send
    told = []
    col._tell = told.append
    out = col.defend_orders()
    assert calls == [("PATCH", "A"), ("DELETE", "B"), ("PATCH", "A")], calls
    assert [r["ok"] for r in out["results"]] == [True, True]
    assert db.our_orders(b_item) == [], "the weaker one is down"
    assert db.our_orders(a_item)[0]["price"] == 154.0, "and A raised"
    events = [e for e in db.order_events() if e["kind"] == "cancel"]
    assert events and "освободить лимит" in events[0]["reason"]
    assert events[0]["source"] == "room", "findable apart from other cancels"
    assert len(told) == 1 and "Освободил место" in told[0]
    assert "поднят" in told[0] and "King Snake" in told[0], told[0]
    from src import digest
    assert "снято ради места под перебивание: 1" in digest.summary_text(db)
    db.close()


def test_room_is_never_bought_with_a_stronger_order():
    col, db = _collector()
    a_item, b_item = _two_orders(col, db)
    import src.collector as collector_mod
    real = collector_mod._safe_rank
    # B now ranks above A: nothing may be taken down for A.
    collector_mod._safe_rank = lambda band: 5.0 if band.float_min < 0.3 else real(band)
    calls = []

    def send(method, url, body=None, headers=None):
        calls.append(method)
        if method == "PATCH":
            raise RuntimeError("HTTP 400 — insufficient balance")
        return {}

    try:
        col.client.send_json = send
        col.defend_orders()
    finally:
        collector_mod._safe_rank = real
    assert calls == ["PATCH"]
    assert len(db.our_orders(b_item)) == 1
    db.close()


def test_a_refused_amend_is_not_tried_again_until_something_changes():
    """Thirty orders refused for balance, every ten minutes: 792 refusals in
    a day, all of them known before they were sent."""
    col, db = _collector()
    a_item, b_item = _two_orders(col, db)
    db.set_setting("an_auto_free", "0")
    calls = []

    def send(method, url, body=None, headers=None):
        calls.append(method)
        raise RuntimeError("HTTP 400 — insufficient balance")

    col.client.send_json = send
    col.defend_orders()
    col.defend_orders()
    assert calls == ["PATCH"], "the second pass held back"
    assert "не хватило баланса" in col._amend_held_back()

    # The orders get smaller - room may be there now.
    db.set_our_order_state(db.our_orders(b_item)[0]["id"], "cancelled", "")
    assert col._amend_held_back() == ""
    col.defend_orders()
    assert calls == ["PATCH", "PATCH"]
    db.close()
