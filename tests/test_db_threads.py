"""One database, several sweeps at once.

A sqlite connection belongs to the thread that opened it. The sweeps are about
to run one per API key - each reading items and writing what it found - and on
a shared connection the first crossing thread raises ProgrammingError, which
would surface as a sweep dying halfway with its requests already spent.
"""
import os
import sqlite3
import tempfile
import threading

import pytest

from src.db import Database


def _db() -> Database:
    return Database(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_a_second_thread_gets_a_connection_of_its_own():
    d = _db()
    seen = {}

    def work():
        seen["conn"] = d.conn
        seen["items"] = d.add_item("AK-47 | Redline (Field-Tested)")

    t = threading.Thread(target=work)
    t.start()
    t.join()

    assert seen["items"], "the other thread could write"
    assert seen["conn"] is not d.conn, "and it was not the main thread's"
    d.close()


def test_many_threads_write_at_once_without_losing_a_row():
    """The failure this guards is not subtle: either every row is there or the
    thread that lost died with an exception and its requests were wasted."""
    d = _db()
    names = [f"Item {i} (Field-Tested)" for i in range(8)]
    for name in names:
        d.add_item(name)
    errors: list[BaseException] = []

    def work(name: str) -> None:
        try:
            item_id = d.get_item_id(name)
            for i in range(40):
                d.conn.execute(
                    "INSERT INTO sales (sale_id, item_id, market_hash_name, "
                    "price_cents, price, float_value, sold_at, "
                    "sold_at_estimated, scraped_at) "
                    "VALUES (?,?,?,?,?,?,?,0,?)",
                    (f"{name}-{i}", item_id, name, 5000, 50.0, 0.16,
                     "2026-09-28T00:00:00+00:00", "2026-09-28T00:00:00+00:00"))
                d.conn.commit()
        except BaseException as exc:  # noqa: BLE001 - the point of the test
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    total = d.conn.execute("SELECT COUNT(*) AS c FROM sales").fetchone()["c"]
    assert total == 8 * 40
    d.close()


def test_a_writer_does_not_block_a_reader():
    """WAL is what makes the whole arrangement safe; a database opened without
    it would serialise the sweeps behind each other."""
    d = _db()
    mode = d.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert int(d.conn.execute("PRAGMA busy_timeout").fetchone()[0]) >= 1000, \
        "two writers meeting should wait, not lose the band they were storing"
    d.close()


def test_closing_takes_every_connection_with_it():
    """A worker that finished leaves its connection behind, and on a
    long-lived collector those accumulate one file handle at a time."""
    d = _db()
    made = []

    def work():
        made.append(d.conn)

    for _ in range(3):
        t = threading.Thread(target=work)
        t.start()
        t.join()

    d.close()
    for conn in made:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_the_schema_is_built_once_not_per_thread():
    """Every connection running the migrations would make opening one a write,
    and a reader thread would take the write lock to do nothing."""
    d = _db()
    calls = []
    d._init_schema = lambda: calls.append(1)   # noqa: SLF001 - that is the check

    t = threading.Thread(target=lambda: d.conn.execute("SELECT 1"))
    t.start()
    t.join()
    assert calls == []
    d.close()
