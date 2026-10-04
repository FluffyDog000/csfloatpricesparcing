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


def test_a_refusal_holds_back_one_key_and_the_next_key_answers():
    """A 429 is the key's, not the request's: the next key reads the band,
    rather than the sweep losing it - "стакан прочитан, листинги нет"."""
    client, ring = _client(spacing=0.0)
    calls = []

    def fake_get(url, **kw):
        calls.append(kw["headers"]["Authorization"])
        if len(calls) == 1:
            return Resp(429, {"Retry-After": "60"}, '{"error":"slow down"}')
        return Resp()

    client.session.get = fake_get
    assert client.fetch_json("https://csfloat.com/api/v1/listings") == {"data": []}
    refused = calls[0]
    assert calls[1] != refused

    cooling = [s.key for s in ring.cooling()]
    assert cooling == [refused], "exactly one key is held back"
    client.fetch_json("https://csfloat.com/api/v1/listings")
    client.fetch_json("https://csfloat.com/api/v1/listings")
    assert refused not in calls[1:], "and it was not the one just refused"


def test_a_key_out_of_listings_still_reads_order_books():
    """The two have separate counters at CSFloat: 200 an hour and 20 a minute."""
    client, ring = _client(keys=["only-one-key-here"], spacing=0.0)
    reset = str(int(time.time()) + 2400)

    def fake_get(url, **kw):
        if url.endswith("/listings"):
            return Resp(429, {"x-ratelimit-limit": "200",
                              "x-ratelimit-remaining": "0",
                              "x-ratelimit-reset": reset})
        return Resp(200, {"x-ratelimit-limit": "20",
                          "x-ratelimit-remaining": "19"})

    client.session.get = fake_get
    with pytest.raises(NoRouteAvailable) as err:
        client.fetch_json("https://csfloat.com/api/v1/listings")
    assert "листинг" in str(err.value)
    client.fetch_json("https://csfloat.com/api/v1/listings/123/buy-orders?limit=10")


def test_quota_is_counted_per_key_and_per_kind():
    """Twenty a minute with a reserve of fifteen stopped the book after five
    requests; the reserve is now a share of the limit."""
    from src.keyring import reserve_for

    assert reserve_for(20) == 1
    assert reserve_for(10) == 0, "a small window is spent to the last"
    assert reserve_for(200) == 10
    client, ring = _client(keys=["first-key-abcdefghij", "second-key-klmnopqrst"],
                           spacing=0.0)
    left = {"first-key-abcdefghij": 1, "second-key-klmnopqrst": 15}
    used = []

    def fake_get(url, **kw):
        key = kw["headers"]["Authorization"]
        used.append(key)
        left[key] -= 1
        return Resp(200, {"x-ratelimit-limit": "20",
                          "x-ratelimit-remaining": str(left[key]),
                          "x-ratelimit-reset": str(int(time.time()) + 50)})

    client.session.get = fake_get
    book = "https://csfloat.com/api/v1/listings/1/buy-orders?limit=10"
    for _ in range(6):
        client.fetch_json(book)
    # The first key spoke once, came back with nothing to spare, and was left
    # alone; the other carried the rest.
    assert used.count("first-key-abcdefghij") == 1
    assert used.count("second-key-klmnopqrst") == 5
    # The address counters are not the key's: a ring answer leaves them alone.
    assert all(r.remaining is None for r in client.pool.routes.values())


def test_a_rejected_key_is_taken_out_and_named_by_its_end():
    client, ring = _client(keys=["good-key-000000000000000", "bad-key-0000000XYZ1234567"],
                           spacing=0.0)

    def fake_get(url, **kw):
        if kw["headers"]["Authorization"].startswith("bad"):
            return Resp(401, {"Content-Type": "application/json"},
                        '{"message":"invalid api key"}')
        return Resp()

    client.session.get = fake_get
    for _ in range(4):
        client.fetch_json("https://csfloat.com/api/v1/listings")
    off = ring.disabled()
    assert [s.key for s in off] == ["bad-key-0000000XYZ1234567"]
    row = [r for r in ring.snapshot() if r["disabled"]][0]
    assert row["tail"] == "…XYZ1234567"
    assert "401" in row["disabled"]


def test_every_key_rejected_says_so_with_the_ends():
    from src.csfloat_client import KeyRejected

    client, ring = _client(keys=["bad-key-00000000AAAA111111"], spacing=0.0)
    client.session.get = lambda url, **kw: Resp(
        401, {"Content-Type": "application/json"}, '{"message":"bad"}')
    with pytest.raises(KeyRejected) as err:
        client.fetch_json("https://csfloat.com/api/v1/listings")
    assert "AAAA111111" in str(err.value)
    with pytest.raises(NoRouteAvailable) as err:
        client.fetch_json("https://csfloat.com/api/v1/listings")
    assert "отклонены" in str(err.value)


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


def test_a_key_is_shown_by_its_last_ten_characters_at_most():
    from src.keyring import tail

    assert tail("abcdefghijklmnopqrstuvwxyz0123456789") == "…0123456789"
    # Never more than half: a short key shown whole would be the key.
    assert tail("short-key") == "…-key"


def test_keys_are_never_written_out_in_full():
    from src.keyring import fingerprint

    client, ring = _client()
    for state in ring.live():
        assert state.key not in state.name
        assert state.name == fingerprint(state.key)
        assert len(state.name) == 8
