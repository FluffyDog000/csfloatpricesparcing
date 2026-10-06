"""Earnings: a sale paired with the purchase of the same skin.

The asset id changes every time a skin moves between Steam inventories, so a
purchase and its later sale are matched by what does not change: the name, the
float and the pattern.
"""
import json
import logging
import os
import tempfile

import pytest

from src import profit as pf

logging.disable(logging.WARNING)

ME = "76561190000000001"
OTHER = "76561190000000002"
NAME = "★ Specialist Gloves | Fade (Field-Tested)"


def trade(id_, role, price_cents, flt=0.1534567891, seed=412, state="verified",
          at="2026-09-01T10:00:00Z", name=NAME, asset="111"):
    """The shape the CSFloat extension reads: a trade around a contract."""
    buyer, seller = (ME, OTHER) if role == "buy" else (OTHER, ME)
    return {"id": id_, "buyer_id": buyer, "seller_id": seller, "state": state,
            "created_at": at, "verified_at": at if state == "verified" else None,
            "contract": {"id": "c" + id_, "price": price_cents, "type": "buy_now",
                         "item": {"asset_id": asset, "market_hash_name": name,
                                  "float_value": flt, "paint_seed": seed}}}


def parsed(*raws):
    return [pf.parse_trade(r, ME) for r in raws]


def test_a_trade_is_read_with_our_side_of_it():
    t = pf.parse_trade(trade("1", "buy", 15050), ME)
    assert t["role"] == "buy" and t["price"] == 150.50
    assert t["float_value"] == pytest.approx(0.1534567891) and t["paint_seed"] == 412
    assert t["done_at"] == "2026-09-01T10:00:00Z"
    assert pf.parse_trade(trade("2", "sell", 100), ME)["role"] == "sell"


def test_an_unfinished_trade_has_no_completion_time():
    t = pf.parse_trade(trade("1", "buy", 100, state="pending"), ME)
    assert t["state"] == "pending" and t["done_at"] is None


def test_a_role_field_answers_when_the_ids_do_not():
    raw = trade("1", "buy", 100)
    del raw["buyer_id"], raw["seller_id"]
    raw["role"] = "seller"
    assert pf.parse_trade(raw, ME)["role"] == "sell"


def test_a_sale_pairs_with_the_purchase_of_the_same_skin():
    book = pf.pair(parsed(
        trade("1", "buy", 10000, at="2026-09-01T10:00:00Z", asset="111"),
        # A new asset id after the move between inventories: same skin.
        trade("2", "sell", 11000, at="2026-09-09T10:00:00Z", asset="999"),
    ), fee=0.02)
    assert len(book.closed) == 1 and not book.holding and not book.unmatched
    d = book.closed[0]
    assert d["fee"] == pytest.approx(2.20)
    assert d["profit"] == pytest.approx(110 * 0.98 - 100)
    assert d["pct"] == pytest.approx(7.8)
    assert d["days"] == pytest.approx(8.0)


def test_another_copy_is_not_the_same_skin():
    book = pf.pair(parsed(
        trade("1", "buy", 10000, flt=0.15, at="2026-09-01T10:00:00Z"),
        trade("2", "sell", 11000, flt=0.16, at="2026-09-09T10:00:00Z"),
    ), fee=0.02)
    assert not book.closed
    assert [t["trade_id"] for t in book.holding] == ["1"]
    assert [t["trade_id"] for t in book.unmatched] == ["2"]


def test_the_pattern_tells_two_copies_with_one_float_apart():
    book = pf.pair(parsed(
        trade("1", "buy", 10000, seed=1, at="2026-09-01T10:00:00Z"),
        trade("2", "sell", 11000, seed=2, at="2026-09-09T10:00:00Z"),
    ), fee=0.02)
    assert not book.closed


def test_a_sale_before_the_purchase_is_not_its_sale():
    book = pf.pair(parsed(
        trade("1", "sell", 11000, at="2026-09-01T10:00:00Z"),
        trade("2", "buy", 10000, at="2026-09-09T10:00:00Z"),
    ), fee=0.02)
    assert not book.closed and len(book.holding) == 1 and len(book.unmatched) == 1


def test_bought_and_sold_twice_pairs_in_order():
    book = pf.pair(parsed(
        trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
        trade("2", "sell", 11000, at="2026-09-05T10:00:00Z"),
        trade("3", "buy", 10500, at="2026-09-10T10:00:00Z"),
        trade("4", "sell", 12000, at="2026-09-15T10:00:00Z"),
    ), fee=0.02)
    pairs = sorted((d["buy_id"], d["sell_id"]) for d in book.closed)
    assert pairs == [("1", "2"), ("3", "4")]


def test_failed_trades_count_for_nothing_and_running_ones_are_listed():
    book = pf.pair(parsed(
        trade("1", "buy", 10000, state="cancelled"),
        trade("2", "buy", 10000, state="pending"),
    ), fee=0.02)
    assert not book.closed and not book.holding
    assert [t["trade_id"] for t in book.pending] == ["2"]


def test_a_held_skin_is_valued_in_its_own_hundredth_when_it_can_be():
    sales = ([{"price": 100.0, "float_value": 0.155}] * 5
             + [{"price": 80.0, "float_value": 0.30}] * 20)
    price, basis = pf.estimate(sales, 0.1534)
    assert price == 100.0 and "0.15" in basis
    price, basis = pf.estimate(sales, 0.40)
    assert price == 80.0 and "предмета" in basis
    assert pf.estimate([], 0.15)[0] is None


def test_a_purchase_is_the_bots_when_one_of_its_orders_covers_it():
    events = [{"market_hash_name": NAME, "price": 100.0, "float_min": 0.15,
               "float_max": 0.16}]
    buy = {"market_hash_name": NAME, "float_value": 0.1534, "price": 100.0}
    assert pf.by_bot(buy, events)
    assert not pf.by_bot(dict(buy, price=101.0), events)
    assert not pf.by_bot(dict(buy, float_value=0.17), events)


# -- reading the account ------------------------------------------------------

def _collector():
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    return Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling)), db


def test_the_collector_finds_the_trades_and_stores_them():
    col, db = _collector()
    asked = []
    pages = {0: {"trades": [trade("2", "sell", 11000, at="2026-09-09T10:00:00Z"),
                            trade("1", "buy", 10000)], "count": 2}}

    def fetch(url, headers=None, account=False):
        asked.append((url, account))
        if url.endswith("/api/v1/me"):
            return {"user": {"steam_id": ME}}
        if "page=" in url and "limit=100" in url:
            page = int(url.split("page=")[1].split("&")[0])
            return pages.get(page, {"trades": []})
        raise RuntimeError("HTTP 404")

    col.client.fetch_json = fetch
    out = col.sync_trades()
    assert out["error"] == "" and out["seen"] == 2 and out["new"] == 2
    assert all(acc for _, acc in asked), "our own account: the main key only"
    assert db.get_setting("trades_path") == pf.TRADES_CANDIDATES[0]
    assert db.get_setting("account_steam_id") == ME
    assert {t["role"] for t in db.all_trades()} == {"buy", "sell"}

    # Nothing new: the first page is settled, so the reading stops there.
    asked.clear()
    again = col.sync_trades(discover=False)
    assert again["new"] == 0
    assert len(asked) == 1, asked


def test_a_trades_path_nobody_answers_is_reported_with_what_was_tried():
    col, db = _collector()

    def fetch(url, headers=None, account=False):
        raise RuntimeError("HTTP 404")

    col.client.fetch_json = fetch
    out = col.sync_trades()
    assert "не нашёл" in out["error"]
    assert len(out["tried"]) >= len(pf.TRADES_CANDIDATES)
    assert json.loads(db.get_setting("trades_sync_result"))["error"]


def test_trades_without_a_side_say_so_and_show_their_fields():
    col, db = _collector()
    raw = trade("1", "buy", 100)
    del raw["buyer_id"], raw["seller_id"]

    def fetch(url, headers=None, account=False):
        if url.endswith("/api/v1/me"):
            return {}
        return [raw]

    col.client.fetch_json = fetch
    out = col.sync_trades()
    assert out["unknown_role"] == 1 and "покупка это или продажа" in out["error"]
    assert "contract.item.float_value" in out["sample_keys"]
    assert "76561" not in json.dumps(out["sample_keys"]), "keys only, no values"


# -- the page ------------------------------------------------------------------

def _app():
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    import importlib

    import webapp
    importlib.reload(webapp)
    webapp.app.config["TESTING"] = True
    return webapp, webapp.app.test_client()


def test_the_earnings_page_pairs_values_and_flags():
    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.add_item(NAME)
    for t in parsed(trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
                    trade("2", "sell", 11000, at="2026-09-09T10:00:00Z"),
                    trade("3", "buy", 9000, flt=0.1523, seed=7,
                          at="2026-09-20T10:00:00Z")):
        db.upsert_trade(t)
    db.record_order_event(name=NAME, kind="place", ok=True, dry=False,
                          source="plan", item_id=item_id, price=90.0,
                          float_min=0.15, float_max=0.16, reason="проба")
    from src.db import utcnow_iso
    for i in range(6):
        db.conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price, "
            "float_value, sold_at, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"s{i}", item_id, NAME, 95.0, 0.155, utcnow_iso(), utcnow_iso()))
    db.conn.commit()
    db.close()

    assert c.get("/profit").status_code == 200
    body = c.get("/api/profit?days=0").get_json()
    assert body["totals"]["deals"] == 1
    assert body["totals"]["profit"] == pytest.approx(110 * 0.98 - 100, abs=0.01)
    assert body["closed"][0]["by_bot"] is False
    held = body["holding"]
    assert len(held) == 1 and held[0]["by_bot"] is True
    assert held[0]["estimate"] == 95.0
    assert held[0]["est_profit"] == pytest.approx(95 * 0.98 - 90, abs=0.01)

    recent = c.get("/api/profit?days=1").get_json()
    assert recent["totals"]["deals"] == 0, "sold before the period"
    assert recent["all_time"]["deals"] == 1


def test_reading_trades_is_asked_of_the_collector():
    webapp, c = _app()
    assert c.post("/api/profit/sync", json={}).status_code == 200
    assert c.get("/api/profit").get_json()["sync_pending"] is True


def test_the_earnings_script_renders_what_the_endpoint_returns():
    from tests.test_analysis_js import _run_script

    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    for t in parsed(trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
                    trade("2", "sell", 11000, at="2026-09-09T10:00:00Z"),
                    trade("3", "buy", 9000, flt=0.2, at="2026-09-20T10:00:00Z"),
                    trade("4", "sell", 5000, flt=0.3, name="AK-47 | Redline (FT)",
                          at="2026-09-21T10:00:00Z")):
        db.upsert_trade(t)
    db.close()
    body = c.get("/api/profit?days=0").get_json()
    got = _run_script("static/profit.js", body)
    assert "Ошибка" not in got["status"], got["status"]
    text = got["profit"]
    assert "+$7.80" in text, text
    assert "AK-47 | Redline (FT)" in text, "a sale with no purchase is listed apart"
    assert "Сделки ещё не читались" in text
    links = got["profitLinks"]
    assert "/item/AK-47%20%7C%20Redline%20(FT)" in links, links
    assert all(h.startswith("/item/") for h in links) and len(links) >= 2


def test_a_dead_proxy_is_not_reported_as_a_missing_trades_path():
    """Every attempt died at the proxy - 'Host unreachable' - and the page
    sent the user to DevTools to find a path the bot already had."""
    col, db = _collector()

    def fetch(url, headers=None, account=False):
        raise RuntimeError("ConnectionError: SOCKSHTTPSConnectionPool(host="
                           "'csfloat.com', port=443): Max retries exceeded "
                           "(0x04: Host unreachable)")

    col.client.fetch_json = fetch
    db.set_setting("account_steam_id", ME)
    out = col.sync_trades()
    assert "прокси" in out["error"] and "Host unreachable" in out["error"]
    assert "DevTools" not in out["error"]


# -- what is counted ------------------------------------------------------------

def _stocked(*raws):
    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.add_item(NAME)
    for t in parsed(*raws):
        db.upsert_trade(t)
    db.close()
    return webapp, c


def test_trades_before_the_start_date_are_not_counted():
    """Bought long before the bot, sold in Steam or kept: those purchases sat
    in "held" for ever and the old sales in "no purchase found"."""
    webapp, c = _stocked(
        trade("1", "buy", 10000, at="2026-08-01T10:00:00Z"),               # old, held
        trade("2", "buy", 5000, flt=0.2, at="2026-08-02T10:00:00Z"),       # old
        trade("3", "sell", 6000, flt=0.2, at="2026-09-05T10:00:00Z"),      # ...sold after
        trade("4", "sell", 7000, flt=0.3, at="2026-08-03T10:00:00Z"),      # old, unmatched
        trade("5", "buy", 9000, flt=0.4, at="2026-09-10T10:00:00Z"),       # new, held
        trade("6", "buy", 8000, flt=0.5, at="2026-09-11T10:00:00Z"),
        trade("7", "sell", 9000, flt=0.5, at="2026-09-12T10:00:00Z"))
    before = c.get("/api/profit?days=0").get_json()
    assert before["all_time"]["deals"] == 2 and len(before["holding"]) == 2

    r = c.post("/api/profit/settings", json={"since": "2026-09-01"})
    assert r.status_code == 200
    body = c.get("/api/profit?days=0").get_json()
    assert body["settings"]["since"] == "2026-09-01"
    assert [h["trade_id"] for h in body["holding"]] == ["5"]
    assert [d["buy_id"] for d in body["closed"]] == ["6"], \
        "bought before the date and sold after is left out too"
    assert body["all_time"]["deals"] == 1
    assert body["unmatched"] == []


def test_a_trade_taken_out_by_hand_is_nowhere_and_can_come_back():
    webapp, c = _stocked(
        trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
        trade("2", "sell", 11000, at="2026-09-09T10:00:00Z"),
        trade("3", "buy", 9000, flt=0.4, at="2026-09-10T10:00:00Z"))
    assert c.post("/api/profit/exclude",
                  json={"trade_ids": ["3"], "excluded": True}).status_code == 200
    body = c.get("/api/profit?days=0").get_json()
    assert body["holding"] == []
    assert [x["trade_id"] for x in body["excluded"]] == ["3"]

    c.post("/api/profit/exclude", json={"trade_ids": ["1", "2"]})
    body = c.get("/api/profit?days=0").get_json()
    assert body["closed"] == [] and body["unmatched"] == [], \
        "a deal goes whole: its sale does not turn up as one with no purchase"

    c.post("/api/profit/exclude", json={"trade_ids": ["1", "2", "3"],
                                        "excluded": False})
    body = c.get("/api/profit?days=0").get_json()
    assert len(body["closed"]) == 1 and len(body["holding"]) == 1
    assert body["excluded"] == []


def test_an_excluded_purchase_does_not_take_the_sale_with_it():
    """Two copies with one float and seed: taking out the first purchase
    leaves the sale to pair with the second, not to vanish."""
    webapp, c = _stocked(
        trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
        trade("2", "buy", 9500, at="2026-09-02T10:00:00Z"),
        trade("3", "sell", 11000, at="2026-09-09T10:00:00Z"))
    c.post("/api/profit/exclude", json={"trade_ids": ["1"]})
    body = c.get("/api/profit?days=0").get_json()
    assert [d["buy_id"] for d in body["closed"]] == ["2"]


def test_the_held_estimate_uses_the_period_set_on_the_page():
    import datetime as dt

    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.add_item(NAME)
    for t in parsed(trade("1", "buy", 9000, at="2026-09-10T10:00:00Z")):
        db.upsert_trade(t)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [(f"r{i}", 100.0, now - dt.timedelta(days=2)) for i in range(6)]
    rows += [(f"o{i}", 60.0, now - dt.timedelta(days=20)) for i in range(7)]
    for sid, price, at in rows:
        db.conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price, "
            "float_value, sold_at, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, item_id, NAME, price, 0.155, at.isoformat(), at.isoformat()))
    db.conn.commit()
    db.close()

    wide = c.get("/api/profit?days=0").get_json()["holding"][0]
    assert wide["estimate"] == 60.0, "thirteen sales over 30 days, median $60"
    assert "за 30 дн" in wide["basis"]
    c.post("/api/profit/settings", json={"estimate_days": 7})
    narrow = c.get("/api/profit?days=0").get_json()["holding"][0]
    assert narrow["estimate"] == 100.0, "only the last week"
    assert "за 7 дн" in narrow["basis"]


def test_bad_settings_are_refused_with_a_reason():
    webapp, c = _app()
    r = c.post("/api/profit/settings", json={"since": "05.10.2026"})
    assert r.status_code == 400 and "ГГГГ-ММ-ДД" in r.get_json()["error"]
    r = c.post("/api/profit/settings", json={"estimate_days": 0})
    assert r.status_code == 400
    body = c.get("/api/profit").get_json()
    assert body["settings"] == {"since": "", "estimate_days": 30}
    assert c.post("/api/profit/settings", json={"since": ""}).status_code == 200


def test_the_page_shows_the_settings_and_the_excluded():
    from tests.test_analysis_js import _run_script

    webapp, c = _stocked(
        trade("1", "buy", 10000, at="2026-09-01T10:00:00Z"),
        trade("3", "buy", 9000, flt=0.4, at="2026-09-10T10:00:00Z"))
    c.post("/api/profit/settings", json={"since": "2026-09-01"})
    c.post("/api/profit/exclude", json={"trade_ids": ["3"]})
    got = _run_script("static/profit.js", c.get("/api/profit?days=0").get_json())
    text = got["profit"]
    assert "Ошибка" not in got["status"], got["status"]
    assert "Сделки до 01.09.2026 не учитываются" in got["settingsNote"]
    assert "Убранные из учёта (1)" in got["excludedTitle"]
    assert "вернуть" in got["excludedText"]


def test_a_purchase_still_in_its_trade_is_held_and_valued():
    """A new bot's every fill spends its first week in a running trade, and
    the page read zero across the board with eleven skins on their way."""
    import datetime as dt

    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.add_item(NAME)
    for t in parsed(trade("1", "buy", 9000, state="pending",
                          at="2026-10-04T15:00:00Z"),
                    trade("2", "sell", 5000, flt=0.4, state="pending",
                          at="2026-10-04T16:00:00Z")):
        db.upsert_trade(t)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for i in range(6):
        db.conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price, "
            "float_value, sold_at, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"s{i}", item_id, NAME, 100.0, 0.155, now, now))
    db.conn.commit()
    db.close()

    body = c.get("/api/profit?days=0").get_json()
    held = body["holding"]
    assert len(held) == 1 and held[0]["pending"] is True
    assert held[0]["est_profit"] == pytest.approx(100 * 0.98 - 90, abs=0.01)
    assert body["holding_totals"]["pending"] == 1
    assert [t["trade_id"] for t in body["pending"]] == ["2"], \
        "listed once: the sale still waits below"
    got = __import__("tests.test_analysis_js", fromlist=["_run_script"]) \
        ._run_script("static/profit.js", body)
    assert "ждёт обмена" in got["profit"]
    assert "ожидаемая прибыль" in got["profit"]


def test_held_skins_read_newest_first_with_a_percentage():
    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.add_item(NAME)
    for t in parsed(trade("1", "buy", 9000, state="verified",
                          at="2026-10-01T10:00:00Z"),
                    trade("2", "buy", 8000, flt=0.1551, state="pending",
                          at="2026-10-05T08:30:00Z"),
                    trade("3", "buy", 8500, flt=0.1552, state="pending",
                          at="2026-10-04T15:00:00Z")):
        db.upsert_trade(t)
    from src.db import utcnow_iso
    for i in range(6):
        db.conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price, "
            "float_value, sold_at, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"s{i}", item_id, NAME, 100.0, 0.155, utcnow_iso(), utcnow_iso()))
    db.conn.commit()
    db.close()

    body = c.get("/api/profit?days=0").get_json()
    assert [h["trade_id"] for h in body["holding"]] == ["2", "3", "1"]
    first = body["holding"][0]
    assert first["est_pct"] == pytest.approx((98 - 80) / 80 * 100, abs=0.1)
    t = body["holding_totals"]
    assert t["est_pct"] == pytest.approx(t["est_profit"] / t["spent"] * 100, abs=0.1)


def test_an_order_brought_down_and_then_filled_is_still_the_bots():
    """The price a purchase is matched on is the order's last one, and a
    lowered order's last price came from a "lower" - which was not looked at,
    so its fill read as a purchase made by hand."""
    webapp, c = _app()
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = db.add_item(NAME)
    db.upsert_trade(parsed(trade("1", "buy", 8800, state="pending",
                                 at="2026-10-05T10:00:00Z"))[0])
    db.record_order_event(name=NAME, kind="place", ok=True, dry=False,
                          source="plan", item_id=item_id, price=90.0,
                          float_min=0.15, float_max=0.16, reason="")
    db.record_order_event(name=NAME, kind="lower", ok=True, dry=False,
                          source="defence", item_id=item_id, price=88.0,
                          was=90.0, float_min=0.15, float_max=0.16, reason="")
    db.close()
    held = c.get("/api/profit?days=0").get_json()["holding"]
    assert held[0]["by_bot"] is True
