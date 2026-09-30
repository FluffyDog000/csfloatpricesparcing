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


class FakeDb:
    """Ровно те два запроса, которые делает оценка."""

    def __init__(self, items, rates):
        self._items = items
        self._rates = rates

    def get_active_items(self):
        return self._items

    def sales_rates(self, since_iso):
        return self._rates


def item(item_id, lo=None, hi=None):
    return {"id": item_id, "market_hash_name": f"item-{item_id}",
            "interval_min_minutes": lo, "interval_max_minutes": hi}


def selling(per_day, days=14):
    """История предмета, продающегося per_day штук в сутки."""
    first = datetime.now(timezone.utc) - timedelta(days=days)
    return (per_day * days, first.isoformat())


def test_without_items_everything_is_counted_at_the_ceiling():
    tool = _tool()
    avg, seen, measured = tool.polls_per_day(
        FakeDb([], {}), ceiling_minutes=360.0, floor_minutes=15.0,
        plain_minutes=60.0)
    assert avg == 4.0, "шесть часов — четыре опроса в сутки"
    assert (seen, measured) == (0, 0)


def test_a_sleepy_item_sits_at_the_ceiling():
    tool = _tool()
    db = FakeDb([item(1)], {1: selling(1)})
    avg, _, measured = tool.polls_per_day(db, 360.0, 15.0, 60.0)
    assert avg == 4.0
    assert measured == 1, "истории хватило — интервал назвало правило"


def test_a_liquid_item_ignores_the_ceiling_and_costs_more():
    """Окно в 40 продаж прокрутится, поэтому правило не даёт ждать 6 часов."""
    tool = _tool()
    db = FakeDb([item(1)], {1: selling(240)})
    avg, _, _ = tool.polls_per_day(db, 360.0, 15.0, 60.0)
    assert avg > 4.0, "ликвидный предмет опрашивается чаще потолка"
    assert avg == pytest.approx(24.0), "10 продаж на опрос при 10 в час — раз в час"


def test_the_liquid_tail_pulls_the_average_above_the_ceiling():
    """Ради чего оценка вообще смотрит в историю, а не делит N на интервал."""
    tool = _tool()
    items = [item(i) for i in range(1, 11)]
    rates = {i: selling(1) for i in range(1, 10)}
    rates[10] = selling(240)
    avg, seen, _ = tool.polls_per_day(FakeDb(items, rates), 360.0, 15.0, 60.0)
    assert seen == 10
    assert avg > 4.0, "один ликвидный предмет из десяти уже виден в среднем"
    assert avg == pytest.approx((9 * 4.0 + 24.0) / 10)


def test_a_per_item_override_wins_over_the_ceiling():
    tool = _tool()
    db = FakeDb([item(1, lo=30, hi=30)], {1: selling(1)})
    avg, _, measured = tool.polls_per_day(db, 360.0, 15.0, 60.0)
    assert avg == 48.0, "полчаса вручную — 48 опросов в сутки"
    assert measured == 0, "правило к такому предмету не применялось"


def test_thin_history_falls_back_to_the_plain_interval():
    """Меньше MIN_SALES_FOR_RATE продаж — судить не о чем."""
    tool = _tool()
    db = FakeDb([item(1)], {1: (2, (datetime.now(timezone.utc)
                                    - timedelta(days=14)).isoformat())})
    avg, _, measured = tool.polls_per_day(db, 360.0, 15.0, 60.0)
    assert measured == 0
    assert avg == 24.0, "взят обычный интервал в 60 минут"


def test_the_plain_interval_never_beats_the_ceiling():
    """Потолок ниже настроенного интервала — значит, опрашиваем реже, не чаще."""
    tool = _tool()
    db = FakeDb([item(1)], {1: (0, None)})
    avg, _, _ = tool.polls_per_day(db, 60.0, 15.0, 600.0)
    assert avg == 24.0


def test_the_wire_overhead_is_counted_on_top_of_the_body():
    """Прокси считает байты на проводе, а не длину JSON."""
    tool = _tool()
    assert tool.WIRE_OVERHEAD > 1.0


def test_addresses_are_sized_below_the_quota():
    """Занимать квоту адреса целиком — значит ловить 429 на первом повторе."""
    tool = _tool()
    assert 0 < tool.SAFE_UTILISATION < 1
