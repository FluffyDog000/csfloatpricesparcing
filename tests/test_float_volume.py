import os
import tempfile
from datetime import datetime, timedelta, timezone

from src.float_volume import band_of, report, zone_edge


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


def test_zone_runs_while_the_hundredths_sell_above_the_ordinary_price():
    floor, base = 0.15, 10.0
    rows = ([(14.0, 0.152)] * 3 + [(12.0, 0.163)] * 3 + [(11.0, 0.177)] * 3
            + [(13.0, 0.185)] * 2           # too thin: neither ends nor extends
            + [(10.6, 0.195)] * 3           # +6%: still the zone
            + [(10.1, 0.205)] * 3           # +1%: ends it
            + [(15.0, 0.215)] * 3)          # past the end, never reached
    assert round(zone_edge(rows, floor, base, 0.05), 4) == 0.2
    # At +15% the bar is $11.50: $14 and $12 clear it, $11 ends the zone.
    assert round(zone_edge(rows, floor, base, 0.15), 4) == 0.17
    assert round(zone_edge(rows, floor, base, 0.05, width=3), 4) == 0.18


def test_report_counts_every_sale_in_the_zone():
    name = "AK (Field-Tested)"                       # wear 0.15-0.38
    rows = [(name, 10.0, 0.30 + k / 100, 1) for k in range(6)]          # ordinary $10
    rows += [(name, 14.0, 0.152, 2)] * 1 + [(name, 13.0, 0.153, 2), (name, 15.0, 0.151, 2)]
    rows += [(name, 12.0, 0.161, 3), (name, 11.0, 0.162, 3), (name, 11.5, 0.165, 3)]
    rows += [(name, 10.0, 0.171, 3), (name, 9.8, 0.172, 3), (name, 10.1, 0.175, 3)]
    rows += [(name, 30.0, 0.150, 40)]               # outside the window: sets the floor
    db = _db(rows)
    r = report(db, days=30, premium=0.05)
    it = r["items"][0]
    assert it["edge"] == 0.17 and it["n"] == 6 and it["base"] == 10.0
    assert r["float"]["usd"] == 76.5 and r["float"]["over"] == 16.5
    assert r["total"]["n"] == 15
    db.close()


def test_an_item_without_enough_ordinary_sales_is_not_judged():
    db = _db([("K (Factory New)", 100.0, 0.001, 1), ("K (Factory New)", 50.0, 0.05, 1)])
    assert report(db)["float"]["n"] == 0
    db.close()


def test_bands():
    assert band_of(3) == "$0–5" and band_of(250) == "$200–1000" and band_of(5000) == "$1000+"
