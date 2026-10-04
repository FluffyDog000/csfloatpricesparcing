"""Which key speaks for which request.

keys.txt holds keys for reading the market - order books and listings, a
hundred at once. Our own account is something else: placing, amending and
taking down orders, and reading which orders we hold. Those go out on the
main key in .env and nothing else. With the key ring attached, a write used
to take whichever ring key was free, and an order lands on the account of
the key that placed it.
"""
import json
import logging

from requests.structures import CaseInsensitiveDict

from src.keyring import KeyRing

logging.disable(logging.WARNING)

MAIN = "main-account-key"


class Resp:
    status_code = 200
    text = "{}"

    def __init__(self):
        self.headers = CaseInsensitiveDict()

    def json(self):
        return {}


class Session:
    """Records what each request carried and where it went."""

    def __init__(self):
        self.sent = []
        self.headers = {}

    def _record(self, headers, proxies):
        self.sent.append({"auth": (headers or {}).get("Authorization"),
                          "proxy": (proxies or {}).get("https")})
        return Resp()

    def get(self, url, headers=None, proxies=None, **_):
        return self._record(headers, proxies)

    def request(self, method, url, headers=None, proxies=None, **_):
        return self._record(headers, proxies)


def _client(key_proxies=None):
    from tests.test_sync import _collector

    col, db = _collector()
    col.config.http.api_key = MAIN
    col.client.http.api_key = MAIN
    db.set_setting("proxies", "\n".join(f"http://u:p@main{n}:8000" for n in range(3)))
    db.set_setting("use_direct", "0")
    if key_proxies:
        db.set_setting("key_proxies", "\n".join(key_proxies))
    col.sync_proxies()
    col.client.keyring = KeyRing(["ring-a", "ring-b", "ring-c"],
                                 col.key_pool_or_main(), spacing=0.0)
    col.client.session = Session()
    col.client.polling.min_seconds_between_requests = 0
    return col, db


def test_orders_and_our_own_list_go_out_on_the_main_key():
    col, db = _client()
    col.client.send_json("POST", "https://csfloat.com/api/v1/buy-orders", {})
    col.client.fetch_json("https://csfloat.com/api/v1/me/buy-orders", account=True)
    sent = col.client.session.sent
    assert [s["auth"] for s in sent] == [MAIN, MAIN]
    db.close()


def test_reading_the_market_goes_out_on_a_ring_key():
    col, db = _client()
    col.client.fetch_json("https://csfloat.com/api/v1/listings?limit=1")
    assert col.client.session.sent[0]["auth"].startswith("ring-")
    db.close()


def test_the_keys_have_their_own_addresses_when_the_page_gives_some():
    col, db = _client(key_proxies=[f"http://u:p@keys{n}:8000" for n in range(4)])
    col.client.fetch_json("https://csfloat.com/api/v1/listings?limit=1")
    col.client.send_json("POST", "https://csfloat.com/api/v1/buy-orders", {})
    ring_req, order_req = col.client.session.sent
    assert "keys" in ring_req["proxy"], "a sweep leaves from the keys' addresses"
    assert "main" in order_req["proxy"], "an order never does"
    db.close()


def test_clearing_the_list_puts_the_keys_back_on_the_main_pool():
    col, db = _client(key_proxies=["http://u:p@keys0:8000", "http://u:p@keys1:8000"])
    assert col.client.keyring.pool is col.client.key_pool
    db.set_setting("key_proxies", "")
    assert col.sync_proxies()
    assert col.client.key_pool is None
    assert col.client.keyring.pool is col.client.pool
    assert col.client.keyring.lease() is not None
    db.close()


def test_the_load_page_saves_the_keys_addresses():
    import os
    from tests.test_analysis_page import _app

    c = _app([])
    r = c.post("/api/load/key_proxies",
               json={"key_proxies": "http://u:p@keys0:8000\nhttp://u:p@keys0:8000"})
    assert r.get_json()["count"] == 1, "the same address once"
    d = c.get("/api/load").get_json()
    assert d["key_proxies_text"] == "http://u:p@keys0:8000"
    assert "keys" in d and "main_key" in d
    bad = c.post("/api/load/key_proxies", json={"key_proxies": "not a proxy"})
    assert bad.status_code == 400
