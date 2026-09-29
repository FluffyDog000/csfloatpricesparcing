"""Two sweeps at once must not share one pinned address.

A pin holds one route for a burst of requests, and the burst belongs to the
thread making it: an order sweep fires a couple of dozen requests inside a
minute, and hopping between addresses inside that window is what CSFloat's
"too many requests from too many IPs" check counts.

Kept in a single slot, two sweeps overwrite each other's pin and both leave
from whichever was pinned last - the opposite of the pin's purpose, and a
failure that would look exactly like the complaint it exists to prevent.
"""
import threading

from src.proxies import ProxyPool

ROUTES = [f"http://u:p@host{i}.example:8080" for i in range(4)]


def test_each_thread_pins_a_route_of_its_own():
    pool = ProxyPool(list(ROUTES), use_direct=False)
    # Both pin before either picks, so a shared slot loses one of them every
    # run rather than whenever the threads happen to interleave.
    pinned = threading.Barrier(2)
    seen: dict[str, str] = {}
    trouble: list[str] = []

    def sweep(name: str) -> None:
        route = pool.pin()
        pinned.wait(5)
        for _ in range(5):
            if pool.pick() is not route:
                trouble.append(name)
                break
        seen[name] = route.key
        pool.unpin()

    threads = [threading.Thread(target=sweep, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not trouble, "a burst left from an address it did not pin"
    assert len(set(seen.values())) == 2, \
        "two sweeps at once took two addresses, not one twice"


def test_unpinning_on_one_thread_leaves_the_other_pinned():
    """The slot used to be shared, so one sweep finishing released another
    sweep's address mid-burst."""
    pool = ProxyPool(list(ROUTES), use_direct=False)
    mine = pool.pin()
    done = threading.Event()

    def other() -> None:
        pool.pin()
        pool.unpin()
        done.set()

    t = threading.Thread(target=other)
    t.start()
    done.wait(5)
    t.join()

    assert pool._pinned is mine, "our pin survived the other thread's unpin"
    pool.unpin()


def test_rebuilding_the_pool_drops_pins_taken_on_every_thread():
    """A pin points at a route object, and after a rebuild that object may not
    be in the pool at all - on any thread, not just the one that rebuilt it.
    """
    pool = ProxyPool(list(ROUTES), use_direct=False)
    held = []
    pinned = threading.Event()
    checked = threading.Event()

    def worker() -> None:
        held.append(pool.pin())
        pinned.set()
        checked.wait(5)
        held.append(pool._pinned)

    t = threading.Thread(target=worker)
    t.start()
    pinned.wait(5)
    pool.replace(["http://u:p@fresh.example:8080"], use_direct=False)
    checked.set()
    t.join()

    assert held[0] is not None, "it really was pinned"
    assert held[1] is None, "and the rebuild reached that thread's pin"


def test_one_thread_still_behaves_exactly_as_before():
    """Nothing about the single-threaded collector changes."""
    pool = ProxyPool(list(ROUTES), use_direct=False)
    route = pool.pin()
    assert pool.pick() is route
    pool.unpin()
    assert pool._pinned is None


def test_a_single_proxy_is_shared_rather_than_refused():
    """Exclusivity is a preference, not a rule. With one route configured it
    is the whole pool, and holding it against the second sweep would stop that
    sweep dead rather than slow it down."""
    pool = ProxyPool(["http://u:p@only.example:8080"], use_direct=False)
    mine = pool.pin()
    got: list[object] = []
    done = threading.Event()

    def other() -> None:
        got.append(pool.pin())
        done.set()

    t = threading.Thread(target=other)
    t.start()
    done.wait(5)
    t.join()

    assert got[0] is mine, "the only address is shared, not withheld"
    pool.unpin()


def test_a_finished_burst_releases_its_address():
    """Otherwise the pool shrinks by one route per sweep that ever ran."""
    pool = ProxyPool(list(ROUTES), use_direct=False)
    taken = []

    def sweep() -> None:
        taken.append(pool.pin().key)
        pool.unpin()

    for _ in range(6):
        t = threading.Thread(target=sweep)
        t.start()
        t.join()

    assert len(set(taken)) == 1, \
        "one at a time, the drain policy keeps returning the same address"
