"""What the bot answers on a phone.

The dashboard answers these questions with tables; a phone needs a paragraph,
built from the same numbers. And a question about what is standing must never
set a sweep going - every command reads the database and nothing else.
"""
import datetime as dt
import logging
import os
import tempfile

logging.disable(logging.WARNING)

NAME = "★ Gloves | Fade (Field-Tested)"


def _db(with_history=True):
    from src.db import Database

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db = Database(path)
    item_id = db.add_item(NAME)
    if with_history:
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for i in range(120):
            price = 200.0 - (i % 10) * 3.0
            rows.append((f"s{i}", item_id, NAME, int(price * 100), price,
                         round(0.35 + (i % 20) * 0.001, 4),
                         (now - dt.timedelta(days=i % 14)).isoformat()))
        db.conn.executemany(
            "INSERT INTO sales (sale_id,item_id,market_hash_name,price_cents,"
            "price,float_value,sold_at,sold_at_estimated,scraped_at) "
            "VALUES (?,?,?,?,?,?,?,0,?)",
            [(a, b, c, d, e, f, g, g) for a, b, c, d, e, f, g in rows])
        db.conn.commit()
        db.replace_buy_orders(item_id, [
            {"price": 150.0, "qty": 1, "float_min": 0.35, "float_max": 0.38}])
    return db, item_id


def test_nothing_held_says_so_rather_than_printing_zeroes():
    from src.tgcommands import answer

    db, _ = _db()
    assert "Ордеров нет" in answer("/orders", db)
    assert "Ордеров нет" in answer("/items", db)
    db.close()


def test_orders_reports_the_money_and_what_it_would_make():
    from src.tgcommands import answer

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="live",
                        remote_id="r1")
    db.upsert_our_order(item_id, 0.37, 0.38, 150.0, 185.0, state="live",
                        remote_id="r2")

    reply = answer("/orders", db)
    assert "Ордеров: 2" in reply
    assert "$310.00" in reply, "the two bids, which is what the budget reserves"
    assert "Ожидаемая прибыль" in reply
    db.close()


def test_the_free_budget_is_reported_when_a_limit_is_set():
    from src.tgcommands import answer

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="live",
                        remote_id="r1")
    db.set_setting("an_total_capital", "500")

    reply = answer("/orders", db)
    assert "Лимит" in reply and "$340.00" in reply, reply
    db.close()


def test_items_lists_each_order_with_its_band_and_price():
    from src.tgcommands import answer

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="live",
                        remote_id="r1")

    reply = answer("/items", db)
    assert NAME in reply
    assert "0.3500–0.3700" in reply
    assert "$160.00" in reply
    db.close()


def test_an_order_placed_by_hand_is_marked_as_not_ours_to_drive():
    from src.tgcommands import answer

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="manual",
                        remote_id="x9")

    assert "вручную" in answer("/orders", db)
    assert "вручную" in answer("/items", db)
    db.close()


def test_being_outbid_is_flagged():
    """The number worth waking up for."""
    from src.tgcommands import answer

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.38, 140.0, 190.0, state="live",
                        remote_id="r1")
    db.replace_buy_orders(item_id, [
        {"price": 155.0, "qty": 2, "float_min": 0.35, "float_max": 0.38}])

    assert "Перебиты: 1" in answer("/orders", db)
    db.close()


def test_status_says_whether_anything_is_actually_running():
    from src.tgcommands import answer

    db, _ = _db()
    off = answer("/status", db)
    assert "выключена" in off and "вхолостую" in off

    db.set_setting("an_defend", "1")
    db.set_setting("an_defend_minutes", "30")
    db.set_setting("analysis_dry_run", "0")
    on = answer("/status", db)
    assert "каждые 30 мин" in on and "боевая" in on
    db.close()


def test_a_command_is_recognised_however_it_is_typed():
    from src.tgcommands import answer, command

    assert command("/Orders@csfloat_bot") == "orders"
    assert command("/items  ") == "items"
    assert command("orders") == "", "a bare word is not a command"

    db, _ = _db()
    assert answer("/help", db).startswith("<b>Что умею</b>")
    assert answer("просто текст", db) is None, "not ours to answer"
    assert answer("/whatever", db) is None
    db.close()


def test_an_item_without_history_is_counted_but_not_priced():
    """A freshly added item has nothing to score against, and a report that
    quietly dropped its order would understate what is committed."""
    from src.tgcommands import answer

    db, item_id = _db(with_history=False)
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="live",
                        remote_id="r1")

    reply = answer("/orders", db)
    assert "Ордеров: 1" in reply and "$160.00" in reply
    assert "Без оценки: 1" in reply
    db.close()


def test_a_command_never_reaches_the_network():
    """Asking a phone-sized question must not set a sweep going or spend a
    request against the account."""
    from src import tgcommands

    db, item_id = _db()
    db.upsert_our_order(item_id, 0.35, 0.37, 160.0, 190.0, state="live",
                        remote_id="r1")

    import requests

    def boom(*a, **k):
        raise AssertionError("a command tried to use the network")

    saved = (requests.get, requests.post)
    requests.get, requests.post = boom, boom
    try:
        for text in ("/orders", "/items", "/status", "/help"):
            assert tgcommands.answer(text, db)
    finally:
        requests.get, requests.post = saved
    db.close()


def test_only_the_authorised_chat_is_answered():
    """The bot's username is public; anyone can message it."""
    import pathlib

    source = pathlib.Path("src/backup_service.py").read_text(encoding="utf-8")
    block = source.split("text = (msg.get(\"text\")")[1][:400]
    assert "auth_chat" in block, "a text command must check the chat first"
    assert "unauthorized" in block


def test_text_messages_are_no_longer_thrown_away():
    """The poll loop read updates and dropped everything that was not a file."""
    import pathlib

    source = pathlib.Path("src/backup_service.py").read_text(encoding="utf-8")
    assert "_answer_command" in source
    assert source.index("text = (msg.get(\"text\")") < source.index("if not doc:\n                continue")


def test_a_loss_is_signed_once():
    """The percentage carried its own sign and a plus was pasted in front of
    it, so a loss read as "+-7.0%"."""
    from src.holdings_report import Holdings, Position, summary_text

    held = Holdings(positions=[Position("A (FT)", 0.15, 0.17, 100.0, 120.0,
                                        "live")],
                    committed=100.0, profit=-7.0, managed=1)
    text = summary_text(held)
    assert "+-" not in text
    assert "(-7.0%)" in text

    good = Holdings(positions=held.positions, committed=100.0, profit=12.0,
                    managed=1)
    assert "(+12.0%)" in summary_text(good)
