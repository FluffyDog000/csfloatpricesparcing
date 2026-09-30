"""The skin's weekly trend, with the float taken out.

The trade lock is seven days and the margin floor is the only defence against
the exit moving, so a skin that fell last week has spent the floor before our
item can be listed. The trend has to be read per skin - the rung's own top
holds four sales a side - and with the float taken out, or a week that traded
worn examples reads as a fall.
"""
import pytest

from src.trend import weekly


def _sales(level_now, level_before, mix_now, mix_before):
    """(age, float, price): price = level × (2 - 2·float), so a worse float is
    cheaper whatever the level."""
    out = []
    for i, f in enumerate(mix_before):
        out.append((8.0 + i % 20, f, level_before * (2 - 2 * f)))
    for i, f in enumerate(mix_now):
        out.append((0.5 + i % 6, f, level_now * (2 - 2 * f)))
    return out


def test_a_week_of_worn_examples_is_not_a_fall():
    """Same level, but last week sold the worse floats. The raw median drops;
    the float-adjusted trend does not."""
    good = [0.15 + (i % 3) * 0.01 for i in range(30)]
    worn = [0.30 + (i % 3) * 0.01 for i in range(10)] + [0.15, 0.16, 0.17]
    both = good + [0.30, 0.31, 0.32] * 3
    t = weekly(_sales(100.0, 100.0, worn, both))
    assert t.change == pytest.approx(0.0, abs=1e-9)


def test_a_real_fall_is_measured():
    mix = [0.15 + (i % 4) * 0.01 for i in range(24)]
    t = weekly(_sales(90.0, 100.0, mix, mix))
    assert t.change == pytest.approx(-0.10, abs=0.03)
    assert t.recent == 24 and t.base == 24


def test_too_few_sales_is_no_answer_rather_than_a_guess():
    mix = [0.15, 0.15, 0.15, 0.16]
    t = weekly(_sales(50.0, 100.0, mix, mix * 5))
    assert t.change is None


def test_sales_older_than_the_horizon_are_ignored():
    mix = [0.15 + (i % 2) * 0.01 for i in range(12)]
    rows = _sales(100.0, 100.0, mix, mix)
    rows += [(60.0, 0.15, 10.0)] * 50           # a crash two months ago
    assert weekly(rows).change == pytest.approx(0.0, abs=1e-9)
