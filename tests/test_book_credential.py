"""A refusal must name the credential the request actually carried.

The book moved onto the API key, but the refusal still read "обнови
CSFLOAT_COOKIE" — so a rejected or missing key sent the reader off to renew a
cookie the request never carried. Worse, with no key configured the book falls
back to the cookie silently, which is how an expired cookie could still break
order reads long after they stopped needing one.
"""
import logging
import os
import tempfile

import pytest


def collector(api_key):
    logging.disable(logging.WARNING)
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    cfg.http.api_key = api_key
    db = Database(cfg.db_path)
    name = "AK-47 | Inheritance (Minimal Wear)"
    item_id = db.add_item(name)
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))
    return col, db, name, item_id


def refuse_the_book(col, listings):
    """Answer the listing query, then refuse every band like CSFloat would."""
    from src.csfloat_client import AuthError

    def fake(url, headers=None):
        if "/buy-orders" in url:
            raise AuthError("HTTP 401")
        return listings

    col.client.fetch_json = fake


LISTINGS = {"data": [{"id": f"L{i}", "item": {"float_value": 0.08 + i * 0.01}}
                     for i in range(3)]}


def test_the_book_goes_out_on_the_key_and_drops_the_cookie():
    col, db, _, _ = collector("il1-testkey")
    headers = col._book_headers()
    assert headers["Authorization"] == "il1-testkey"
    assert headers["Cookie"] is None, (
        "the session cookie must be removed, not merged in")
    db.close()


def test_a_rejected_key_is_not_reported_as_a_stale_cookie():
    col, db, name, item_id = collector("il1-revoked")
    refuse_the_book(col, LISTINGS)
    result = col.sweep_buy_orders(name, item_id)
    assert "CSFLOAT_API_KEY" in result["error"]
    assert "CSFLOAT_COOKIE" not in result["error"], (
        "the request carried a key, so renewing the cookie fixes nothing")
    db.close()


def test_a_missing_key_says_so_instead_of_blaming_the_cookie_alone():
    """Without a key the book quietly falls back to the cookie; say that."""
    col, db, name, item_id = collector(None)
    assert col._book_headers() is None
    refuse_the_book(col, LISTINGS)
    result = col.sweep_buy_orders(name, item_id)
    assert "CSFLOAT_API_KEY" in result["error"], (
        "the real fix is configuring a key, not renewing the cookie")
    db.close()


@pytest.mark.parametrize("key,expected", [("il1-x", "key"), (None, "cookie")])
def test_the_named_credential_follows_what_the_request_carries(key, expected):
    col, db, _, _ = collector(key)
    assert col._book_credential() == expected
    db.close()
