"""When each item is polled: by how fast it sells, what its last polls found,
and how much it matters.

Six thousand items do not fit a day at four polls each. Most of them sell a
few times a week, and polling those four times a day was twenty-eight polls
per sale; the liquid few, meanwhile, could roll past the 40-sale window when
everything was stretched alike.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import schedule as sch

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def test_the_rate_is_read_from_the_latest_sales_up_to_now():
    """An item that has gone quiet reads slower with every quiet day."""
    sold = [ago(h) for h in (30, 40, 50, 60, 70, 80)]
    assert sch.sales_per_hour(sold, NOW) == pytest.approx(6 / 80)
    assert sch.sales_per_hour([], NOW) is None


def test_a_liquid_items_span_is_its_rate_not_a_day():
    """Twenty sales in two hours read over a day would stretch the wait
    tenfold and let the window roll past."""
    sold = [ago(h / 10) for h in range(1, 21)]          # 20 in 2 hours
    assert sch.sales_per_hour(sold, NOW) == pytest.approx(10.0)


def test_a_burst_of_a_few_is_not_a_rate():
    sold = [ago(0.1), ago(0.15), ago(0.2)]
    assert sch.sales_per_hour(sold, NOW) == pytest.approx(3 / 24)


def test_the_wait_lets_about_fifteen_sales_gather():
    assert sch.interval_minutes(1.0, 15, 4320) == pytest.approx(15 * 60)
    assert sch.interval_minutes(100.0, 15, 4320) == 15, "never under the floor"
    assert sch.interval_minutes(0.001, 15, 4320) == 4320, "never over the ceiling"
    assert sch.interval_minutes(None, 15, 360) == 360, "no sales: the ceiling"


def test_empty_polls_stretch_the_next_one():
    base = sch.interval_minutes(0.5, 15, 100000)
    fb = sch.feedback_from([{"status": "ok", "new_count": 0, "fetched_count": 40,
                             "overlap_count": 40}] * 2)
    assert fb.empty_streak == 2
    assert sch.interval_minutes(0.5, 15, 100000, fb) == pytest.approx(base * 1.5 ** 2)


def test_a_nearly_full_window_brings_the_next_poll_to_the_floor():
    fb = sch.feedback_from([{"status": "ok", "new_count": 33, "fetched_count": 40,
                             "overlap_count": 7}])
    assert fb.crowded
    assert sch.interval_minutes(0.01, 15, 4320, fb) == 15


def test_a_poll_that_barely_overlaps_is_a_gap():
    rows = [{"status": "ok", "new_count": 20, "fetched_count": 40, "overlap_count": 1},
            {"status": "ok", "new_count": 5, "fetched_count": 40, "overlap_count": 35}]
    assert sch.feedback_from(rows).gap
    first = [{"status": "ok", "new_count": 40, "fetched_count": 40, "overlap_count": 0}]
    assert not sch.feedback_from(first).gap, "the first poll overlaps nothing"


def test_failed_polls_say_nothing_about_the_market():
    rows = [{"status": "rate_limited", "new_count": 0, "fetched_count": 0},
            {"status": "ok", "new_count": 0, "fetched_count": 40, "overlap_count": 40}]
    assert sch.feedback_from(rows).empty_streak == 1


def test_an_item_we_hold_an_order_on_is_polled_hourly_whatever_it_sells():
    s = sch.Settings(floor=15)
    plans = sch.plan_items(
        [{"id": 1}, {"id": 2}, {"id": 3}], {}, {}, with_orders={1},
        in_analysis={2}, settings=s, now=NOW)
    assert [p.tier for p in plans] == ["orders", "analysis", "rest"]
    assert [p.minutes for p in plans] == [60, 360, 4320]


def test_an_interval_set_by_hand_wins():
    s = sch.Settings(floor=15)
    (p,) = sch.plan_items([{"id": 1, "interval_min_minutes": 30,
                            "interval_max_minutes": 30}], {}, {}, set(), set(), s)
    assert p.minutes == 30


def test_when_the_day_is_short_the_rest_gives_way_first():
    demand = {"orders": 2000, "analysis": 3000, "rest": 20000}
    stretch = sch.rest_stretch(demand, capacity=20000)
    assert stretch == pytest.approx(20000 / (16000 - 5000))
    assert sch.rest_stretch({"rest": 100}, capacity=20000) == 1.0
    assert sch.rest_stretch({"orders": 30000, "rest": 100}, capacity=20000) \
        == sch.REST_STRETCH_MAX


def test_the_plan_reads_the_database_in_bulk():
    import os
    import tempfile

    from src.db import Database

    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    try:
        busy = db.add_item("AK-47 | Redline (Field-Tested)")
        quiet = db.add_item("Sticker | Crown (Foil)")
        now = datetime.now(timezone.utc)
        rows = [(f"s{i}", busy, "x", 100, 1.0, 0.2,
                 (now - timedelta(minutes=10 * i)).isoformat()) for i in range(20)]
        db.conn.executemany(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, price_cents, "
            "price, float_value, sold_at, sold_at_estimated, scraped_at) "
            "VALUES (?,?,?,?,?,?,?,0,?)", [r + (r[-1],) for r in rows])
        db.conn.commit()
        db.set_setting("ceiling_rest_minutes", "1440")
        plans, settings = sch.plan_from_db(db, 15.0)
        by_id = {p.item_id: p for p in plans}
        # Twenty in a little over three hours: fifteen gather in about 2.4.
        assert by_id[busy].minutes == pytest.approx(15 / (20 / (190 / 60)) * 60, rel=0.02)
        assert by_id[quiet].minutes == 1440, "nothing sold: the rest ceiling"
        assert settings.ceiling("rest") == 1440
    finally:
        db.close()
