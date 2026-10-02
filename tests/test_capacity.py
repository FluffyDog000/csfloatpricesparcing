"""Что список из N предметов стоит на самом деле.

Счёт «N ÷ интервал» занижает: потолок интервала связывает только медленные
предметы, а ликвидные опрашиваются по своей скорости продаж независимо от
настройки. Оценка, которая этого не видит, обещает трафик, которого не будет,
и адреса, которых не хватит.
"""
import importlib.util
import pathlib

import pytest
from datetime import datetime, timedelta, timezone


def _tool():
    path = pathlib.Path(__file__).resolve().parent.parent / "tools" / "capacity.py"
    spec = importlib.util.spec_from_file_location("capacity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plan(item_id, per_day, tier="rest"):
    """Предмет, продающийся per_day штук в сутки, как его видит планировщик."""
    from src.schedule import ItemPlan
    rate = per_day / 24.0 if per_day else None
    return ItemPlan(item_id, tier, rate, 60.0, 0.0)


def settings(rest_hours=72.0):
    from src.schedule import Settings
    out = Settings(floor=15.0)
    out.ceilings["rest"] = rest_hours * 60.0
    return out


def test_each_group_costs_what_the_rule_says():
    """Ликвид — 15 продаж на опрос; спящий — раз в потолок."""
    tool = _tool()
    est = tool.estimate([plan(1, 48.0), plan(2, 0.1)], settings(), items=2)
    assert est["groups"]["liquid"]["per_item"] == pytest.approx(48.0 / 15.0)
    assert est["groups"]["thin"]["per_item"] == pytest.approx(24.0 / 72.0)


def test_the_list_keeps_the_mix_of_the_database():
    tool = _tool()
    plans = [plan(i, 48.0) for i in range(10)] + [plan(10 + i, 0.1) for i in range(90)]
    est = tool.estimate(plans, settings(), items=6000)
    assert est["groups"]["liquid"]["items"] == pytest.approx(600)
    assert est["groups"]["thin"]["items"] == pytest.approx(5400)


def test_a_mix_can_be_given_when_the_database_is_not_like_the_list():
    """Сейчас в базе одни перчатки; список на 6000 будет другим."""
    tool = _tool()
    est = tool.estimate([plan(1, 0.1)], settings(), items=1000,
                        mix=tool.parse_mix("10,30,60"))
    assert est["groups"]["liquid"]["items"] == pytest.approx(100)
    assert not est["groups"]["liquid"]["measured"], \
        "в базе ликвида нет — взята типичная скорость, и это сказано"


def test_items_we_hold_or_price_are_counted_as_they_are():
    tool = _tool()
    plans = [plan(1, 0.1, tier="orders"), plan(2, 0.1)]
    est = tool.estimate(plans, settings(), items=100)
    assert est["held"] == 1
    assert est["rest_items"] == 99
    assert est["demand"]["orders"] == pytest.approx(24.0), "раз в час по плану"


def test_the_mix_is_three_shares():
    tool = _tool()
    assert tool.parse_mix("1,1,2") == {"liquid": 1, "middle": 1, "thin": 2}
    with pytest.raises(ValueError):
        tool.parse_mix("10,90")


def test_the_wire_overhead_is_counted_on_top_of_the_body():
    """Прокси считает байты на проводе, а не длину JSON."""
    tool = _tool()
    assert tool.WIRE_OVERHEAD > 1.0


def test_addresses_are_sized_below_the_quota():
    """Занимать квоту адреса целиком — значит ловить 429 на первом повторе."""
    tool = _tool()
    assert 0 < tool.SAFE_UTILISATION < 1


def test_the_size_comes_from_the_latest_responses_not_a_time_window():
    """Способ замера менялся: разжатое тело -> провод, разница в 7 раз.

    Окно по времени отдаёт голос числу строк, а старых за неделю накопилось
    27 тысяч — они задавливали правильные ещё сутки после правки, и прогноз
    оставался неверным, пока измеряемое уже было верным. Счёт по последним
    строкам переворачивается за часы.
    """
    import sqlite3
    import tempfile
    import pathlib

    from src.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "t.db")
        db = Database(path)
        try:
            item = db.add_item("AK-47 | Redline (Field-Tested)")
            for _ in range(300):                      # старые, завышенные
                db.log_poll(item_id=item, market_hash_name="x", fetched_count=40,
                            new_count=0, overlap_count=40, status="ok",
                            response_bytes=120_000)
            for _ in range(60):                       # свежие, по проводу
                db.log_poll(item_id=item, market_hash_name="x", fetched_count=40,
                            new_count=0, overlap_count=40, status="ok",
                            response_bytes=17_000)

            latest = db.recent_response_size(limit=50)
            assert latest["samples"] == 50
            assert latest["avg_bytes"] == 17_000, "только свежие"

            mixed = db.recent_response_size(limit=360)
            assert mixed["avg_bytes"] > 17_000, "хватит вглубь — вернутся старые"
        finally:
            db.close()


def test_the_recent_sample_is_empty_rather_than_wrong_on_a_fresh_database():
    import pathlib
    import tempfile

    from src.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(pathlib.Path(tmp) / "t.db"))
        try:
            got = db.recent_response_size()
            assert got["samples"] == 0 and got["avg_bytes"] is None
        finally:
            db.close()
