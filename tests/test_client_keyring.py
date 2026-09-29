"""A hundred keys, a hundred clocks.

The client paced every request against one shared gap and stopped every item
on one 429. That is right for a single key and is the whole of the ceiling a
hundred were meant to lift: 2.5 seconds between requests caps the bot at about
1440 an hour however many addresses or keys it holds.

With a ring, a request leases a key whose own clock has come round, speaks
from one of that key's own addresses, and a refusal holds back that key alone.
Without a ring nothing changes, which is what the collector runs today.
"""
import threading
import time

import pytest

from src.config import load_config
from src.csfloat_client import CSFloatClient, NoRouteAvailable, RateLimited
from src.keyring import KeyRing
from src.proxies import ProxyPool

KEYS = ["key-one", "key-two", "key-three"]
ROUTES = [f"http://u:p@host{i}.example:8080" for i in range(6)]


class Resp:
    def __init__(self, status=200, headers=None, text="{}"):
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def json(self):
        return {"data": []}


def _client(keys=KEYS, spacing=2.5):
    cfg = load_config()
    pool = ProxyPool(list(ROUTES), use_direct=False)
    ring = KeyRing(list(keys), pool, spacing=spacing)
    client = CSFloatClient(cfg.http, cfg.polling, keyring=ring)
    client.pool = pool
    return client, ring


def test_each_request_carries_the_key_it_leased():
    """Sending one key's Authorization from another key's address makes them
    one client again, with the addresses spread over it."""
    client, ring = _client(spacing=0.0)
    sent = []

    def fake_get(url, **kw):
        sent.append(kw["headers"]["Authorization"])
        return Resp()

    client.session.get = fake_get
    for _ in range(3):
        client.fetch_json("https://csfloat.com/api/v1/listings")
    assert set(sent) == set(KEYS), "the keys took turns, one per request"


def test_a_refusal_holds_back_one_key_and_not_the_others():
    client, ring = _client(spacing=0.0)
    calls = []

    def fake_get(url, **kw):
        calls.append(kw["headers"]["Authorization"])
        if len(calls) == 1:
            return Resp(429, {"Retry-After": "60"}, '{"error":"slow down"}')
        return Resp()

    client.session.get = fake_get
    with pytest.raises(RateLimited):
        client.fetch_json("https://csfloat.com/api/v1/listings")
    refused = calls[0]

    cooling = [s.name for s in ring.cooling()]
    assert len(cooling) == 1, "exactly one key is held back"
    # The others keep working.
    client.fetch_json("https://csfloat.com/api/v1/listings")
    client.fetch_json("https://csfloat.com/api/v1/listings")
    assert refused not in calls[1:], "and it was not the one just refused"


def test_spacing_is_per_key_so_the_keys_do_not_queue_behind_each_other():
    """Three keys at a 2.5 second gap answer three requests at once; on one
    shared clock the third would wait five seconds."""
    client, _ = _client(spacing=2.5)
    client.session.get = lambda url, **kw: Resp()

    started = time.monotonic()
    for _ in range(3):
        client.fetch_json("https://csfloat.com/api/v1/listings")
    assert time.monotonic() - started < 1.0


def test_a_key_speaks_only_from_its_own_addresses():
    """Borrowing an address from another key is what turns a hundred tidy
    clients back into one account touring the internet."""
    client, ring = _client(spacing=0.0)
    used = []

    def fake_get(url, **kw):
        used.append((kw["headers"]["Authorization"], kw["proxies"]["https"]))
        return Resp()

    client.session.get = fake_get
    for _ in range(9):
        client.fetch_json("https://csfloat.com/api/v1/listings")

    for key, proxy in used:
        allowed = {client.pool.routes[name].proxies()["https"]
                   for name in ring.routes_for(key)}
        assert proxy in allowed


def test_without_a_ring_the_client_behaves_exactly_as_before():
    cfg = load_config()
    client = CSFloatClient(cfg.http, cfg.polling)
    assert client.keyring is None
    client.pool = ProxyPool(list(ROUTES), use_direct=False)
    sent = []
    client.session.get = lambda url, **kw: (sent.append(kw.get("headers")),
                                            Resp())[1]
    client.fetch_json("https://csfloat.com/api/v1/listings",
                      headers={"Authorization": "from-env"})
    assert sent[0]["Authorization"] == "from-env"


def test_a_jam_is_reported_rather_than_waited_out_forever():
    """Every key disabled is a configuration problem, and a sweep that hangs
    on it looks like a network stall."""
    client, ring = _client(spacing=0.0)
    for key in KEYS:
        ring.disable(key, "revoked")
    with pytest.raises(NoRouteAvailable):
        client.fetch_json("https://csfloat.com/api/v1/listings")


def test_keys_are_never_written_out_in_full():
    from src.keyring import fingerprint

    client, ring = _client()
    for state in ring.live():
        assert state.key not in state.name
        assert state.name == fingerprint(state.key)
        assert len(state.name) == 8
