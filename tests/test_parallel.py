"""Several items at once, one per key.

Three hundred items at roughly twenty-five requests each is over five hours
one after another, which is why "look at the whole list a few times a day" has
never been on offer. The work per item is unchanged; what changes is how many
are in flight.
"""
import os
import tempfile
import threading
import time

from src.config import load_config
from src.csfloat_client import CSFloatClient
from src.db import Database
from src.keyring import KeyRing, read_keys
from src.parallel import MAX_WORKERS, sweep_items, workers_for
from src.proxies import ProxyPool

ROUTES = [f"http://u:p@host{i}.example:8080" for i in range(9)]


def _collector(keys):
    from src.collector import Collector

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    db = Database(cfg.db_path)
    client = CSFloatClient(cfg.http, cfg.polling)
    client.pool = ProxyPool(list(ROUTES), use_direct=False)
    if keys:
        client.keyring = KeyRing(list(keys), client.pool, spacing=0.0)
    return Collector(cfg, db, client), db


def test_one_key_sweeps_one_item_at_a_time():
    """The behaviour this replaces, unchanged: no ring, one worker."""
    col, db = _collector([])
    assert workers_for(col) == 1
    db.close()


def test_workers_never_outnumber_the_keys_that_can_speak():
    col, db = _collector(["a", "b", "c"])
    assert workers_for(col) == 3
    col.client.keyring.disable("b", "revoked")
    assert workers_for(col) == 2
    db.close()


def test_workers_never_outnumber_the_items_either():
    col, db = _collector(["a", "b", "c", "d"])
    assert workers_for(col, 2) == 2
    db.close()


def test_a_hundred_keys_do_not_become_a_hundred_threads():
    col, db = _collector([f"k{i}" for i in range(40)])
    assert workers_for(col) == MAX_WORKERS
    db.close()


def test_items_really_are_swept_at_the_same_time():
    """The point of the exercise. Four items that each take a beat: run one
    after another they take four beats, in parallel they take about one."""
    col, db = _collector(["a", "b", "c", "d"])
    names = [f"Item {i} (Field-Tested)" for i in range(4)]
    for name in names:
        db.add_item(name)

    live = []
    peak = []
    lock = threading.Lock()

    def slow(name, item_id):
        with lock:
            live.append(name)
            peak.append(len(live))
        time.sleep(0.15)
        with lock:
            live.remove(name)
        return {"orders": {"orders": 1}, "depth": {"bands": 1}}

    col.sweep_both_sides = slow
    started = time.monotonic()
    out = sweep_items(col, names)
    elapsed = time.monotonic() - started

    assert out["workers"] == 4
    assert len(out["swept"]) == 4
    assert max(peak) > 1, "they overlapped"
    assert elapsed < 0.45, "four beats' work took about one"
    db.close()


def test_one_item_failing_does_not_take_the_others_with_it():
    """A sweep dying takes its own requests with it either way; taking the
    other workers too would waste the quota they had already spent."""
    col, db = _collector(["a", "b"])
    names = [f"Item {i} (Field-Tested)" for i in range(4)]
    for name in names:
        db.add_item(name)

    def maybe(name, item_id):
        if name.endswith("1 (Field-Tested)"):
            raise RuntimeError("limit")
        return {"orders": {"orders": 1}}

    col.sweep_both_sides = maybe
    out = sweep_items(col, names)
    assert len(out["swept"]) == 3
    assert "limit" in list(out["failed"].values())[0]
    db.close()


def test_an_untracked_name_is_reported_rather_than_swept():
    col, db = _collector(["a", "b"])
    col.sweep_both_sides = lambda n, i: {"orders": {}}
    out = sweep_items(col, ["Nothing | Here (Field-Tested)"])
    assert out["swept"] == {}
    assert out["failed"] == {"Nothing | Here (Field-Tested)": "не отслеживается"}
    db.close()


# -- where the keys come from ----------------------------------------------

def test_keys_come_from_a_file_and_comments_are_ignored():
    path = os.path.join(tempfile.mkdtemp(), "keys.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("first\n\n# a note\nsecond  # trailing\nfirst\n")
    assert read_keys(path) == ["first", "second"]


def test_a_missing_key_file_is_not_an_error():
    """Running on one key is the normal state until the rest arrive, and a
    typo in a path should not stop the collector."""
    assert read_keys("/nonexistent/keys.txt") == []
    assert read_keys(None) == []


def test_the_collector_only_rings_up_when_there_is_more_than_one_key():
    import pathlib

    source = pathlib.Path("run_collector.py").read_text(encoding="utf-8")
    assert "_attach_keyring(collector)" in source
    assert "len(keys) < 2" in source, \
        "one key is the single-clock path, not a ring of one"
