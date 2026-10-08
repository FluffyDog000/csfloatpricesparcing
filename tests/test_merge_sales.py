import sqlite3

from src.db import Database
from src.merge_sales import merge


def _db(path, items, sales):
    Database(path)  # своя схема
    conn = sqlite3.connect(path)
    for name in items:
        conn.execute("INSERT INTO items (market_hash_name, added_at) VALUES (?, '2026-01-01')",
                     (name,))
    for sid, name, cents, fl, at in sales:
        item = conn.execute("SELECT id FROM items WHERE market_hash_name=?", (name,)).fetchone()
        if item is None:
            conn.execute("INSERT INTO items (market_hash_name, added_at) VALUES (?, '2026-01-01')",
                         (name,))
            item = conn.execute("SELECT id FROM items WHERE market_hash_name=?",
                                (name,)).fetchone()
        conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price_cents, price, "
            "float_value, sold_at, scraped_at) VALUES (?,?,?,?,?,?,?, '2026-01-01')",
            (sid, item[0], name, cents, cents / 100, fl, at))
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT s.sale_id, i.market_hash_name, s.price FROM sales s "
                        "JOIN items i ON i.id = s.item_id ORDER BY sale_id").fetchall()
    conn.close()
    return rows


def test_adds_only_own_items_and_skips_duplicates(tmp_path):
    own, other = str(tmp_path / "own.db"), str(tmp_path / "other.db")
    _db(own, ["AK"], [("a1", "AK", 1000, 0.15, "2026-10-01T10:00:00+00:00")])
    _db(other, ["X"], [
        ("a1", "AK", 1000, 0.15, "2026-10-01T10:00:00+00:00"),   # тот же id
        ("zz", "AK", 1000, 0.15, "2026-10-01T12:00:00+00:00"),   # та же продажа, другой id
        ("a0", "AK", 900, 0.16, "2026-09-20T10:00:00+00:00"),    # новая, раньше
        ("x1", "X", 500, 0.2, "2026-09-20T10:00:00+00:00"),      # не наш предмет
    ])

    dry = merge(own, other)
    assert dry["new_sales"] == 1 and dry["items_gaining"] == 1 and dry["inserted"] == 0
    assert dry["covered_items"] == 1
    assert _rows(own) == [("a1", "AK", 10.0)]

    done = merge(own, other, apply=True)
    assert done["inserted"] == 1
    assert _rows(own) == [("a0", "AK", 9.0), ("a1", "AK", 10.0)]
    assert merge(own, other, apply=True)["inserted"] == 0


def test_same_float_and_price_days_apart_is_a_new_sale(tmp_path):
    own, other = str(tmp_path / "own.db"), str(tmp_path / "other.db")
    _db(own, ["AK"], [("a1", "AK", 1000, 0.15, "2026-10-01T10:00:00+00:00")])
    _db(other, [], [("a2", "AK", 1000, 0.15, "2026-09-25T10:00:00+00:00")])
    assert merge(own, other, apply=True)["inserted"] == 1


def test_other_db_left_untouched(tmp_path):
    own, other = str(tmp_path / "own.db"), str(tmp_path / "other.db")
    _db(own, ["AK"], [])
    _db(other, [], [("a0", "AK", 900, 0.16, "2026-09-20T10:00:00+00:00")])
    before = open(other, "rb").read()
    merge(own, other, apply=True)
    assert open(other, "rb").read() == before


def test_sales_only_export_without_some_columns(tmp_path):
    own, full, export = (str(tmp_path / n) for n in ("own.db", "full.db", "export.db"))
    _db(own, ["AK"], [])
    _db(full, [], [("a0", "AK", 900, 0.16, "2026-09-20T10:00:00+00:00")])
    conn = sqlite3.connect(export)
    conn.execute("ATTACH DATABASE ? AS s", (full,))
    conn.execute("CREATE TABLE sales AS SELECT sale_id, market_hash_name, price, "
                 "float_value, paint_seed, sold_at FROM s.sales")
    conn.commit()
    conn.close()
    assert merge(own, export, apply=True)["inserted"] == 1
    c = sqlite3.connect(own)
    assert c.execute("SELECT price_cents, sold_at_estimated FROM sales").fetchone() == (900, 0)
