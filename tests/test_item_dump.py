"""One item's tables, fetched from the phone.

The whole-database export is 69MB against Telegram's ~50MB ceiling, so it
fails; and it carries the settings table, where collector.py writes the proxy
list with its passwords. A per-item dump is small and holds no credentials,
which is the only version safe to send anywhere.
"""
import os
import sqlite3
import tempfile

import pytest

from src import tgcommands
from src.item_dump import ItemNotFound, render, resolve, safe_filename

NAME = "AWP | Printstream (Field-Tested)"
SECRET_PROXY = "http://user:sup3rsecret@proxy.example.com:8000"
SECRET_COOKIE = "session=deadbeefcookie"


def db():
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    from src.db import Database

    d = Database(os.environ["CSFLOAT_DB_PATH"])
    item_id = d.add_item(NAME)
    d.add_item("★ Karambit | Doppler (Factory New)")
    con = d.conn
    for n, (price, fl) in enumerate([(141.5, 0.2312), (138.0, 0.2688),
                                     (152.0, 0.1701)]):
        con.execute(
            "INSERT INTO sales (sale_id,item_id,market_hash_name,price,"
            "float_value,sold_at,scraped_at) VALUES (?,?,?,?,?,?,?)",
            (f"s{n}", item_id, NAME, price, fl,
             "2026-09-25T10:00:00+00:00", "2026-09-25T10:00:00+00:00"))
    con.execute("INSERT INTO buy_orders (item_id,price,qty,float_min,float_max,"
                "position,fetched_at) VALUES (?,?,?,?,?,?,?)",
                (item_id, 130.0, 3, 0.15, 0.25, 0, "2026-09-25T10:00:00+00:00"))
    con.execute("INSERT INTO listing_depth (item_id,fetched_at,float_min,"
                "float_max,listings,cheapest) VALUES (?,?,?,?,?,?)",
                (item_id, "2026-09-25T10:00:00+00:00", 0.22, 0.24, 5, 148.0))
    # The credentials the export must never carry.
    d.set_setting("proxies", SECRET_PROXY)
    d.set_setting("cookie", SECRET_COOKIE)
    con.commit()
    return d, item_id


def test_the_dump_carries_no_credentials():
    d, item_id = db()
    body = render(d.conn, item_id, NAME, days=3650)
    assert "sup3rsecret" not in body
    assert SECRET_PROXY not in body
    assert SECRET_COOKIE not in body
    d.close()


def test_the_dump_holds_all_three_sides():
    d, item_id = db()
    body = render(d.conn, item_id, NAME, days=3650)
    assert "ПРОДАЖИ" in body and "141.5" in body
    assert "СТАКАН ОРДЕРОВ" in body and "130" in body
    assert "ЛИСТИНГИ НА ПРОДАЖУ" in body and "148" in body
    d.close()


def test_sales_outside_the_window_are_left_out():
    d, item_id = db()
    body = render(d.conn, item_id, NAME, days=1)
    assert "141.5" not in body, "a stale sale would pad the history silently"
    d.close()


def test_a_loosely_typed_name_still_resolves():
    """Typed on a phone, the star and the spacing are the first casualties."""
    d, _ = db()
    assert resolve(d.conn, "Printstream (Field")[1] == NAME
    assert resolve(d.conn, "  AWP | Printstream (Field-Tested)  ")[1] == NAME
    d.close()


def test_an_unknown_name_offers_what_there_is():
    d, _ = db()
    with pytest.raises(ItemNotFound) as caught:
        resolve(d.conn, "Butterfly Knife")
    assert NAME in caught.value.candidates
    d.close()


def test_an_ambiguous_name_does_not_pick_one_silently():
    d, _ = db()
    d.add_item("AWP | Printstream (Minimal Wear)")
    d.conn.commit()
    with pytest.raises(ItemNotFound) as caught:
        resolve(d.conn, "Printstream")
    assert len(caught.value.candidates) == 2
    d.close()


def test_the_filename_survives_a_name_with_pipes_and_stars():
    made = safe_filename("★ Karambit | Doppler (Factory New)", 60)
    assert "|" not in made and "/" not in made and " " not in made
    assert made.endswith("-60d.txt")


@pytest.mark.parametrize("text,expected", [
    ("/dump AWP | Printstream (Field-Tested)", "AWP | Printstream (Field-Tested)"),
    ("/dump@mybot Printstream", "Printstream"),
    ("/dump", ""),
    ("/dump   ", ""),
])
def test_the_command_argument_is_read_whole(text, expected):
    assert tgcommands.command(text) == "dump"
    assert tgcommands.argument(text) == expected


def test_dump_is_a_known_command_but_answers_with_no_text():
    """The transport sends the file; answer() returning text would double up."""
    d, _ = db()
    assert tgcommands.known("/dump Printstream")
    assert tgcommands.answer("/dump Printstream", d) is None
    assert "/dump" in tgcommands.HELP
    d.close()


def test_the_dump_is_small_enough_to_send():
    d, item_id = db()
    body = render(d.conn, item_id, NAME, days=3650)
    assert len(body.encode()) < 50 * 1024 * 1024, (
        "the whole-database export already fails on this ceiling")
    d.close()
