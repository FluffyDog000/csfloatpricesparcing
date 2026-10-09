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


def _arb(path, items, sales):
    """A cs2arb-shaped database: names in market_items, price as text."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE market_items (market_hash_name TEXT, weapon TEXT, skin TEXT, "
                 "wear TEXT, stattrak INT, souvenir INT, star INT)")
    conn.execute("CREATE TABLE sales (platform TEXT, sale_id TEXT, weapon TEXT, skin TEXT, "
                 "price TEXT, currency TEXT, wear TEXT, float_value REAL, paint_seed INT, "
                 "paint_index INT, stattrak INT, souvenir INT, star INT, sold_at TEXT, "
                 "collected_at TEXT)")
    conn.executemany("INSERT INTO market_items VALUES (?,?,?,?,?,?,?)", items)
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sales)
    conn.commit()
    conn.close()


def test_cs2arb_sales_are_named_from_market_items(tmp_path):
    own, other = str(tmp_path / "own.db"), str(tmp_path / "arb.db")
    _db(own, ["AK-47 | Redline (Field-Tested)", "StatTrak™ AK-47 | Redline (Field-Tested)",
              "★ Karambit | Doppler Phase 2 (Factory New)"], [])
    _arb(other, [
        ("AK-47 | Redline (Field-Tested)", "AK-47", "Redline", "FT", 0, 0, 0),
        ("StatTrak™ AK-47 | Redline (Field-Tested)", "AK-47", "Redline", "FT", 1, 0, 0),
        ("★ Karambit | Doppler (Factory New)", "Karambit", "Doppler", "FN", 0, 0, 1),
    ], [
        ("csfloat", "s1", "AK-47", "Redline", "30.50", "USD", "FT", 0.2, 1, 282, 0, 0, 0,
         "2026-09-28T05:00:00+00:00", "2026-09-28T06:00:00+00:00"),
        ("csfloat", "s2", "AK-47", "Redline", "45.00", "USD", "FT", 0.21, 2, 282, 1, 0, 0,
         "2026-09-28T05:00:00+00:00", "2026-09-28T06:00:00+00:00"),
        ("csfloat", "k2", "Karambit", "Doppler", "900.00", "USD", "FN", 0.01, 3, 419, 0, 0, 1,
         "2026-09-28T05:00:00+00:00", "2026-09-28T06:00:00+00:00"),
        ("csfloat", "k3", "Karambit", "Doppler", "800.00", "USD", "FN", 0.01, 4, 420, 0, 0, 1,
         "2026-09-28T05:00:00+00:00", "2026-09-28T06:00:00+00:00"),       # Phase 3: not ours
        ("buff", "b1", "AK-47", "Redline", "29.00", "USD", "FT", 0.22, 5, 282, 0, 0, 0,
         "2026-09-28T05:00:00+00:00", "2026-09-28T06:00:00+00:00"),       # other market
    ])
    dry = merge(own, other)
    assert dry["format"].startswith("cs2arb") and dry["new_sales"] == 3
    assert merge(own, other, apply=True)["inserted"] == 3
    c = sqlite3.connect(own)
    got = c.execute("SELECT s.sale_id, i.market_hash_name, s.price_cents, s.scraped_at "
                    "FROM sales s JOIN items i ON i.id = s.item_id ORDER BY sale_id").fetchall()
    assert got == [
        ("k2", "★ Karambit | Doppler Phase 2 (Factory New)", 90000, "2026-09-28T06:00:00+00:00"),
        ("s1", "AK-47 | Redline (Field-Tested)", 3050, "2026-09-28T06:00:00+00:00"),
        ("s2", "StatTrak™ AK-47 | Redline (Field-Tested)", 4500, "2026-09-28T06:00:00+00:00"),
    ]
