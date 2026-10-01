"""A hundred keys are only useful if each one keeps its own few addresses.

CSFloat's support allowed 2-4 IPs per API key. The value of that allowance
comes entirely from the binding being stable and private: a key that wanders
the pool is the "too many IPs for one account" pattern that once parked every
rotating route, just spread over more keys.
"""
import time

from src.keyring import (DEFAULT_ROUTES_PER_KEY, MAX_ROUTES_PER_KEY,
                         KeyRing, fingerprint)
from src.proxies import ProxyPool

KEYS = [f"key-{n}" for n in range(6)]


def pool(count=8):
    return ProxyPool([f"http://user:pw@host{n}:8000" for n in range(count)],
                     use_direct=False)


def ring(keys=None, routes=8, per_key=DEFAULT_ROUTES_PER_KEY, spacing=0.0):
    return KeyRing(keys or KEYS, pool(routes), routes_per_key=per_key,
                   spacing=spacing)


def test_each_key_gets_the_allowed_number_of_addresses():
    r = ring()
    for state in r.keys:
        assert len(state.routes) == DEFAULT_ROUTES_PER_KEY


def test_routes_per_key_is_clamped_to_what_support_allowed():
    assert ring(per_key=99).routes_per_key == MAX_ROUTES_PER_KEY
    assert ring(per_key=1).routes_per_key == 2


def test_binding_survives_a_restart():
    """The whole point: the same key comes back to the same addresses."""
    first = {s.key: s.routes for s in ring().keys}
    second = {s.key: s.routes for s in ring().keys}
    assert first == second


def test_binding_ignores_the_order_keys_were_listed_in():
    straight = {s.key: s.routes for s in ring(KEYS).keys}
    shuffled = {s.key: s.routes for s in ring(list(reversed(KEYS))).keys}
    assert straight == shuffled


def test_a_key_never_borrows_an_address_outside_its_slice():
    r = ring(spacing=0.0)
    allowed = {s.key: set(s.routes) for s in r.keys}
    for _ in range(200):
        leased = r.lease()
        assert leased is not None
        state, route = leased
        assert route.key in allowed[state.key], (
            "a key reached outside its own addresses; that is the pattern "
            "that drew 'too many requests from too many IPs'")


def test_a_key_stays_silent_until_its_own_spacing_elapsed():
    r = ring(keys=["solo"], spacing=60.0)
    assert r.lease() is not None      # first call is free
    assert r.lease() is None          # ...the second must wait
    assert r.wait_seconds() > 0


def test_keys_do_not_share_one_clock():
    """A global clock would make a hundred keys as slow as one."""
    r = ring(keys=["a", "b", "c"], spacing=60.0)
    leased = [r.lease() for _ in range(3)]
    assert all(x is not None for x in leased)
    assert len({state.key for state, _ in leased}) == 3
    assert r.lease() is None          # all three are now inside their spacing


def test_leasing_takes_turns_between_keys():
    r = ring(keys=["a", "b", "c"], spacing=0.0)
    used = [r.lease()[0].key for _ in range(9)]
    assert used.count("a") == used.count("b") == used.count("c") == 3


def test_a_disabled_key_is_not_leased():
    r = ring(keys=["good", "revoked"], spacing=0.0)
    r.disable("revoked", "401 from CSFloat")
    for _ in range(20):
        state, _ = r.lease()
        assert state.key == "good"


def test_a_key_with_every_address_spent_is_skipped():
    r = ring(keys=["a"], spacing=0.0)
    for name in r.keys[0].routes:
        route = r.pool.routes[name]
        route.parked_until = time.monotonic() + 600
    assert r.lease() is None


def test_rebinding_after_the_pool_shrinks_keeps_keys_inside_it():
    r = ring()
    r.pool.replace(["http://user:pw@host0:8000"], use_direct=False)
    r.bind()
    live = set(r.pool.routes)
    for state in r.keys:
        assert state.routes, "a key left without any address cannot work"
        assert set(state.routes) <= live


def test_duplicate_keys_do_not_buy_extra_speed():
    """The same key twice would get two clocks and halve its own spacing."""
    r = KeyRing(["same", "same"], pool(), spacing=60.0)
    assert len(r.keys) == 1
    assert r.lease() is not None
    assert r.lease() is None


def test_concurrency_follows_the_number_of_live_keys():
    r = ring(keys=["a", "b", "c", "d"], spacing=0.0)
    assert r.concurrency() == 4
    r.disable("a", "revoked")
    assert r.concurrency() == 3


def test_a_snapshot_never_carries_the_key_itself():
    secret = "il1-averysecretlookingkey"
    r = KeyRing([secret], pool(), spacing=0.0)
    rows = r.snapshot()
    assert rows[0]["key"] == fingerprint(secret)
    assert secret not in repr(rows)


# -- "ни одного рабочего ключа с адресом" -----------------------------------

def test_a_key_whose_addresses_are_cooling_is_a_wait_not_a_dead_end():
    """The clock said "ready" while all three of the key's addresses were
    cooling down after a 429, so the wait came back as 0 and the sweep gave
    up at once with "no working key" - over a few minutes."""
    r = ring(keys=["solo"], spacing=0.0)
    now = time.monotonic()
    for name in r.keys[0].routes:
        r.pool.routes[name].cooldown_until = now + 300
    assert r.lease() is None
    assert 290 < r.wait_seconds() <= 300


def test_new_proxies_reach_the_keys():
    """The proxy list is re-read from the dashboard while the collector runs,
    and the keys stayed bound to the old one: a replaced list left every key
    ready, with nothing in the pool to speak from."""
    r = ring(keys=["a", "b"], spacing=0.0)
    r.pool.replace([f"http://user:pw@fresh{n}:8000" for n in range(6)],
                   use_direct=False)
    assert r.lease() is None, "still bound to addresses that are gone"
    assert r.wait_seconds() == float("inf"), "and that is not a wait"
    r.bind()
    assert r.lease() is not None


def test_the_collector_rebinds_the_keys_when_the_list_changes():
    from tests.test_sync import _collector

    col, db = _collector()
    col.client.keyring = KeyRing(["a", "b"], col.client.pool, spacing=0.0)
    db.set_setting("proxies", "\n".join(
        f"http://user:pw@fresh{n}:8000" for n in range(6)))
    db.set_setting("use_direct", "0")
    assert col.sync_proxies()
    assert col.client.keyring.lease() is not None
    db.close()


def test_the_client_says_why_when_it_cannot_lease():
    import pytest
    from src.csfloat_client import NoRouteAvailable
    from tests.test_sync import _collector

    col, db = _collector()
    r = KeyRing(["a"], col.client.pool, spacing=0.0)
    col.client.keyring = r
    now = time.monotonic()
    for route in col.client.pool.routes.values():
        route.cooldown_until = now + 600
    with pytest.raises(NoRouteAvailable, match="освободится через 10 мин"):
        col.client._lease()

    col.client.pool.replace(["http://user:pw@elsewhere:8000"], use_direct=False)
    with pytest.raises(NoRouteAvailable, match="нет адреса"):
        col.client._lease()
    db.close()
