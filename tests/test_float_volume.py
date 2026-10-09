import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.float_volume import band_of, report


def _db(rows):
    from src.db import Database
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    now = datetime.now(timezone.utc)
    for i, (name, price, f, days_ago) in enumerate(rows):
        item = db.conn.execute("SELECT id FROM items WHERE market_hash_name=?", (name,)).fetchone()
        if item is None:
            db.conn.execute("INSERT INTO items (market_hash_name, added_at) VALUES (?, 'x')", (name,))
            item = db.conn.execute("SELECT id FROM items WHERE market_hash_name=?", (name,)).fetchone()
        db.conn.execute(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price, float_value, sold_at, "
            "scraped_at) VALUES (?,?,?,?,?,?, 'x')",
            (f"s{i}", item[0], name, price, f, (now - timedelta(days=days_ago)).isoformat()))
    db.conn.commit()
    return db


def test_float_sales_are_the_best_hundredths_sold_above_the_ordinary_price():
    rows = [("AK (FT)", 10.0, 0.20 + k / 100, 1) for k in range(6)]     # ordinary, $10
    rows += [("AK (FT)", 14.0, 0.151, 2),        # best hundredth, +40%: float sale
             ("AK (FT)", 10.2, 0.155, 2),        # best hundredth, but no premium
             ("AK (FT)", 13.0, 0.165, 3),        # second hundredth, +30%: float sale
             ("AK (FT)", 20.0, 0.150, 40)]       # float, but outside the window (sets the floor)
    db = _db(rows)
    r = report(db, days=30, best=2, premium=0.10)
    assert r["total"]["n"] == 9
    assert r["float"]["n"] == 2 and r["float"]["usd"] == 27.0 and r["float"]["over"] == 7.0
    assert r["items"][0]["base"] == 10.0 and r["bands"][0]["band"] == "$5–20"
    db.close()


def test_an_item_without_enough_ordinary_sales_is_not_judged():
    db = _db([("K (FN)", 100.0, 0.001, 1), ("K (FN)", 50.0, 0.05, 1)])
    assert report(db)["float"]["n"] == 0
    db.close()


def test_bands():
    assert band_of(3) == "$0–5" and band_of(250) == "$200–1000" and band_of(5000) == "$1000+"
