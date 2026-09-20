"""Reading the account instead of trusting our own record of it.

Four orders were placed by the bot and all four taken down by hand from the
site. Nothing in our_orders changed, so the dashboard reported four standing
orders that did not exist, the capital limit reserved money that was free, and
the defence would have amended positions that were gone.
"""
import json
import logging
import os
import tempfile

logging.disable(logging.WARNING)


def _collector(list_path="/api/v1/buy-orders"):
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database
    from src.placement import PLACEMENT_KEY, SUGGESTED

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    spec = SUGGESTED.as_dict()
    spec["list_path"] = list_path
    db.set_setting(PLACEMENT_KEY, json.dumps(spec, ensure_ascii=False))
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    return col, db


def _answers(col, payload):
    col.client.fetch_json = lambda url, headers=None: payload


NAME = "★ Gloves | Fade (Field-Tested)"


def _site_order(remote_id="r1", price_cents=15000, lo=0.35, hi=0.38, bought=0):
    return {"id": remote_id, "qty": 1, "price": price_cents,
            "market_hash_name": NAME, "bought_item_count": bought,
            "hybrid_properties": {"min_float": lo, "max_float": hi}}


def test_orders_taken_down_by_hand_stop_being_counted():
    col, db = _collector()
    item_id = db.add_item(NAME)
    for i, lo in enumerate((0.35, 0.38, 0.41, 0.44)):
        db.upsert_our_order(item_id, lo, lo + 0.02, 150.0, 160.0,
                            state="live", remote_id=f"r{i}")
    assert len(db.our_orders()) == 4

    _answers(col, [])          # the account holds nothing
    out = col.sync_our_orders()

    assert out["counts"]["gone"] == 4
    assert db.our_orders() == [], "and the page stops claiming four"
    events = db.order_events()
    assert len(events) == 4 and all(e["source"] == "sync" for e in events)
    assert "снят вручную или исполнен" in events[0]["reason"]
    db.close()


def test_an_order_still_standing_is_left_alone():
    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    _answers(col, {"data": [_site_order("r1")]})
    out = col.sync_our_orders()

    assert out["counts"] == {"gone": 0, "filled": 0, "repriced": 0,
                             "adopted": 0, "matched": 1}
    assert len(db.our_orders()) == 1
    assert db.order_events() == [], "nothing happened, nothing to log"
    db.close()


def test_a_fill_is_recorded_as_a_fill():
    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    _answers(col, [_site_order("r1", bought=1)])
    col.sync_our_orders()

    assert db.our_orders() == [], "it is not standing any more"
    held = db.our_orders(live_only=False)
    assert held[0]["state"] == "filled"
    assert db.order_events()[0]["kind"] == "fill"
    db.close()


def test_the_site_price_wins_over_ours():
    """An order standing at a price we did not write down is our record being
    wrong, not CSFloat."""
    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    _answers(col, [_site_order("r1", price_cents=15200)])
    col.sync_our_orders()

    assert db.our_orders()[0]["price"] == 152.0
    ev = db.order_events()[0]
    assert ev["kind"] == "raise" and ev["was"] == 150.0 and ev["price"] == 152.0
    db.close()


def test_an_order_placed_by_hand_is_shown_but_not_taken_over():
    """Adopting it as ours would let the defence amend or withdraw an order
    someone placed deliberately, at a price the bot never chose and has no
    ceiling for."""
    col, db = _collector()
    item_id = db.add_item(NAME)

    _answers(col, [_site_order("x9", price_cents=14000, lo=0.20, hi=0.22)])
    out = col.sync_our_orders()

    assert out["counts"]["adopted"] == 1
    assert db.our_orders() == [], "the defence must not see it"
    found = db.our_orders(live_only=False)
    assert len(found) == 1 and found[0]["state"] == "manual"
    assert found[0]["price"] == 140.0 and found[0]["remote_id"] == "x9"
    db.close()


def test_an_order_for_an_item_we_do_not_track_is_reported_not_stored():
    col, db = _collector()
    _answers(col, [_site_order("x9")])
    out = col.sync_our_orders()

    assert out["counts"]["adopted"] == 1
    assert "не отслеживается" in out["changes"][0]["detail"]
    assert db.our_orders(live_only=False) == []
    db.close()


def test_without_a_configured_list_path_nothing_is_guessed():
    col, db = _collector(list_path="")
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    out = col.sync_our_orders()
    assert "не настроен" in out["error"]
    assert len(db.our_orders()) == 1, "an unanswered question is not a no"
    db.close()


def test_a_failed_request_never_empties_our_record():
    """The dangerous failure: a 403 read as "the account holds nothing" would
    mark every live order gone and re-place all of them."""
    import requests

    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    def refuse(url, headers=None):
        raise requests.HTTPError("HTTP 403")

    col.client.fetch_json = refuse
    out = col.sync_our_orders()

    assert "403" in out["error"]
    assert len(db.our_orders()) == 1
    assert db.order_events() == []
    db.close()


def test_the_listing_endpoint_is_looked_for_when_the_configured_one_refuses():
    """GET /api/v1/buy-orders answers 405: the path exists, but it is where
    orders are created, not where they are listed. A GET spends nothing but a
    request, so the likely paths are tried rather than the feature stopping."""
    import requests

    from src.placement import PLACEMENT_KEY, load

    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    asked = []

    def answer(url, headers=None):
        asked.append(url)
        if url.endswith("/api/v1/buy-orders"):
            raise requests.HTTPError("HTTP 405 для " + url)
        if "/me/buy-orders?page=0" in url:
            return {"data": [_site_order("r1")]}
        raise requests.HTTPError("HTTP 404 для " + url)

    col.client.fetch_json = answer
    out = col.sync_our_orders(discover=True)

    assert out["error"] == ""
    assert out["found_path"] == "/api/v1/me/buy-orders?page=0&limit=100"
    assert out["counts"]["matched"] == 1
    # Found by asking, and asking costs requests, so it is kept.
    assert load(db.get_setting(PLACEMENT_KEY)).list_path == out["found_path"]
    db.close()


def test_a_path_that_answers_with_nothing_is_not_taken_for_the_right_one():
    """The dangerous find. An empty list is what a wrong path returns and also
    exactly the reply that marks every held order gone."""
    import requests

    col, db = _collector()
    item_id = db.add_item(NAME)
    db.upsert_our_order(item_id, 0.35, 0.38, 150.0, 160.0, state="live",
                        remote_id="r1")

    def answer(url, headers=None):
        if url.endswith("/api/v1/buy-orders"):
            raise requests.HTTPError("HTTP 405")
        return {"data": []}        # every candidate answers, none with orders

    col.client.fetch_json = answer
    out = col.sync_our_orders(discover=True)

    assert "не нашёл" in out["error"]
    assert len(db.our_orders()) == 1, "nothing was declared gone"
    assert any("ордеров в ответе нет" in t for t in out["tried"])
    db.close()


def test_the_unattended_pass_does_not_go_probing():
    """The pass before each defence runs with nobody watching, and every
    candidate is a request against the same account."""
    import requests

    col, db = _collector()
    asked = []

    def answer(url, headers=None):
        asked.append(url)
        raise requests.HTTPError("HTTP 405")

    col.client.fetch_json = answer
    out = col.sync_our_orders()           # discover defaults to off

    assert len(asked) == 1, asked
    assert "405" in out["error"]
    db.close()


def test_a_configured_path_that_works_is_not_second_guessed():
    col, db = _collector(list_path="/api/v1/me/buy-orders")
    asked = []

    def answer(url, headers=None):
        asked.append(url)
        return []

    col.client.fetch_json = answer
    out = col.sync_our_orders(discover=True)

    assert asked == ["https://csfloat.com/api/v1/me/buy-orders"]
    assert out["error"] == "" and out["seen"] == 0
    db.close()
