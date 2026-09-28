"""Every lot's price, not just the cheapest one.

The listings arrive in full on a request we already make, twice a sweep, and
were reduced to a count and a minimum before being stored. That minimum is
what a single seller undercutting the rest moves, and moving it rejected whole
bands: on a live item the queue was priced off one $52.09 lot with forty-nine
others behind it that nobody had recorded.
"""
import json
import os
import tempfile

import pytest

from src.depth import depth_profile
from src.ladder import Params, evaluate, queue_price


def lot(price, f, created="2026-09-01T00:00:00+00:00"):
    return {"id": f"L{price}-{f}", "price": price, "float": f,
            "created_at": created, "type": "buy_now", "min_offer_price": None}


def db():
    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    from src.db import Database

    return Database(os.environ["CSFLOAT_DB_PATH"])


# -- collection ------------------------------------------------------------

def test_the_profile_keeps_every_lot_cheapest_first():
    rows = [lot(55.0, 0.16), lot(52.09, 0.165), lot(53.4, 0.168)]
    band = depth_profile(rows, (0.15, 0.17), step=0.02)[0]
    assert band["asks"] == [[52.09, 0.165], [53.4, 0.168], [55.0, 0.16]]
    assert band["cheapest"] == 52.09, "still the first of them"


def test_each_price_carries_the_float_it_belongs_to():
    """A band is 0.02 wide while an order's top moves by 0.01, so the pricing
    has to know which lots are worse items than the one it would sell."""
    rows = [lot(55.0, 0.161), lot(52.09, 0.169)]
    band = depth_profile(rows, (0.15, 0.17), step=0.02)[0]
    assert dict((f, p) for p, f in band["asks"])[0.169] == 52.09


def test_an_empty_band_carries_an_empty_list_not_a_null():
    band = depth_profile([lot(55.0, 0.30)], (0.15, 0.32), step=0.02)[0]
    assert band["asks"] == []
    assert band["cheapest"] is None


# -- storage ---------------------------------------------------------------

def test_the_prices_survive_a_round_trip_through_the_database():
    d = db()
    item = d.add_item("AWP | Printstream (Field-Tested)")
    rows = [lot(52.09, 0.165), lot(53.4, 0.168), lot(55.0, 0.169)]
    d.record_listing_depth(item, depth_profile(rows, (0.15, 0.17), step=0.02))

    back = d.listing_depth(item)[0]
    assert back["asks"] == [[52.09, 0.165], [53.4, 0.168], [55.0, 0.169]]
    d.close()


def test_a_band_written_before_the_column_existed_reads_as_an_empty_list():
    """Pricing must see a list it can iterate, not a null to guess about."""
    d = db()
    item = d.add_item("AWP | Printstream (Field-Tested)")
    d.conn.execute(
        "INSERT INTO listing_depth (item_id, fetched_at, float_min, float_max,"
        " listings, cheapest) VALUES (?,?,?,?,?,?)",
        (item, "2026-09-01T00:00:00+00:00", 0.15, 0.17, 50, 52.09))
    d.conn.commit()
    assert d.listing_depth(item)[0]["asks"] == []
    d.close()


def test_the_migration_adds_the_columns_to_an_existing_database():
    d = db()
    path = d.path
    d.close()

    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("ALTER TABLE listing_depth DROP COLUMN asks")
    con.commit()
    con.close()

    from src.db import Database
    again = Database(path)
    cols = {r[1] for r in again.conn.execute(
        "PRAGMA table_info(listing_depth)").fetchall()}
    assert "asks" in cols
    again.close()


# -- what it buys ----------------------------------------------------------

def test_a_lone_discounter_no_longer_prices_the_whole_queue():
    """With only the minimum stored, one lot at $95 set the exit for a band
    whose other lots were all above $100 - and the rung was rejected."""
    lone = queue_price([95.0], lots=1, cleared=7.0, step=0.10)
    assert lone is None, "he is waited out"

    # The same band as it used to be read: count and minimum only.
    blind = queue_price([95.0], lots=15, cleared=7.0, step=0.10)
    assert blind == pytest.approx(94.90)

    # And as it reads now, with the prices behind him.
    seeing = queue_price([95.0] + [101.0] * 14, lots=15, cleared=7.0,
                         step=0.10)
    assert seeing == pytest.approx(100.90), "the eighth lot, not the first"


def test_the_exit_follows_the_queue_rather_than_its_minimum():
    """Forty lots and only the cheapest recorded: the exit sits under that one
    lot. With all forty on hand it sits under the survivor of the lock."""
    sales = [{"price": 110.0, "float_value": 0.165} for _ in range(40)]
    sales += [{"price": 96.0, "float_value": 0.165} for _ in range(4)]
    p = Params(window_days=16.0)

    blind = evaluate(0.17, sales, [], (0.15, 0.38), lots=40,
                     asks=[[95.0, 0.165]], params=p)
    seeing = evaluate(0.17, sales, [], (0.15, 0.38), lots=40,
                      asks=[[95.0, 0.165]] + [[108.0, 0.165]] * 39, params=p)

    # The grid step at this price is a dollar, not a dime.
    assert blind.queue_price == pytest.approx(94.0), "the lone lot priced it"
    assert seeing.queue_price == pytest.approx(107.0), "the real survivor did"
    assert seeing.ceiling > blind.ceiling, "and we may pay more for it"


def test_a_partial_record_never_reads_as_an_empty_queue():
    """Forty lots with one price stored must not become a queue of one: that
    is the optimistic direction, and it would let us bid up on no evidence."""
    sales = [{"price": 110.0, "float_value": 0.165} for _ in range(40)]
    sales += [{"price": 96.0, "float_value": 0.165} for _ in range(4)]
    r = evaluate(0.17, sales, [], (0.15, 0.38), lots=40,
                 asks=[[95.0, 0.165]], params=Params(window_days=16.0))
    assert r.lots == 40, "the recorded count stands"
    assert r.queue_price is not None


# -- a band read to the endpoint's limit ------------------------------------

def price_band(prices, lo=0.15, hi=0.17, f=0.165):
    return {"float_min": lo, "float_max": hi, "listings": len(prices),
            "cheapest": min(prices), "asks": [[p, f] for p in prices]}


def test_a_full_page_with_the_median_above_it_is_marked_as_a_floor():
    """Fifty is the endpoint's maximum, so a band that returns fifty was cut.
    While the median sits under the dearest lot stored, everything unseen is
    dearer still and would never have joined the queue. Once the median clears
    that price, the lots never returned could be under it."""
    from src.ladder import Params, evaluate

    # Sales rich enough to put the median above every stored lot.
    sales = [{"price": 200.0, "float_value": 0.165, "age_days": i % 14 + 0.5}
             for i in range(20)]
    asks = [[90.0 + i, 0.165] for i in range(50)]        # dearest is $139
    rung = evaluate(0.17, sales, [], (0.15, 0.38), lots=50, asks=asks,
                    params=Params(window_days=16.0))
    assert rung.market == 200.0
    assert rung.lots_capped, "the median is above every lot the page returned"


def test_a_full_page_whose_dearest_lot_beats_the_median_is_not_marked():
    """The cut is real but harmless: no unseen lot can be cheaper than the
    ones we have, so none of them would have joined the queue."""
    from src.ladder import Params, evaluate

    sales = [{"price": 100.0, "float_value": 0.165, "age_days": i % 14 + 0.5}
             for i in range(20)]
    asks = [[90.0 + i, 0.165] for i in range(50)]        # dearest is $139
    rung = evaluate(0.17, sales, [], (0.15, 0.38), lots=50, asks=asks,
                    params=Params(window_days=16.0))
    assert not rung.lots_capped


def test_a_band_short_of_the_page_limit_was_not_cut_at_all():
    from src.ladder import Params, evaluate

    sales = [{"price": 200.0, "float_value": 0.165, "age_days": i % 14 + 0.5}
             for i in range(20)]
    asks = [[90.0 + i, 0.165] for i in range(12)]
    rung = evaluate(0.17, sales, [], (0.15, 0.38), lots=12, asks=asks,
                    params=Params(window_days=16.0))
    assert not rung.lots_capped, "twelve of fifty is the whole band"


def test_the_page_carries_the_mark_through_to_the_band():
    from src.pricing import Params as PricingParams
    from src.pricing import plan

    sales = [{"price": 200.0, "float_value": 0.155, "age_days": i % 14 + 0.5}
             for i in range(20)]
    depth = [price_band([90.0 + i for i in range(50)], f=0.155)]
    band = next(b for b in plan(sales, [], (0.15, 0.38), depth,
                                PricingParams(window_days=16.0))
                if abs(b.float_max - 0.16) < 1e-9)
    assert band.queue_capped is True
