"""The filter that runs before a single request is spent.

Reading one order book costs roughly six requests through the cookie and the
residential route. Three hundred items is over an hour of continuous asking,
on the path that once drew "too many requests from too many IPs". So the first
filter has to be the free one, and it has to be right: an item dropped here is
never looked at again.
"""
from src.screen import Screen, look, profile


def sales(prices, ages=None):
    ages = ages or [1.0] * len(prices)
    return [{"price": p, "age_days": a} for p, a in zip(prices, ages)]


STEADY = sales([100.0 + (i % 5) for i in range(60)], [i % 14 for i in range(60)])


def test_nothing_is_dropped_until_thresholds_are_chosen():
    """A screen that throws items away before anyone has set it up is worse
    than no screen: it reads as the market saying no."""
    assert look(STEADY, Screen()).passed
    assert look(sales([1.0]), Screen()).passed


def test_an_item_with_no_history_cannot_be_scored_at_all():
    got = look([], Screen())
    assert not got.passed and "нет истории" in got.reason


def test_price_is_bounded_at_both_ends():
    cheap = look(sales([2.0] * 30), Screen(min_price=15.0))
    assert not cheap.passed and "дешевле" in cheap.reason

    dear = look(sales([900.0] * 30), Screen(max_price=150.0))
    assert not dear.passed and "дороже" in dear.reason

    assert look(STEADY, Screen(min_price=15.0, max_price=150.0)).passed


def test_flow_is_counted_over_the_window_not_over_all_history():
    """Sixty sales in two years is not the same item as sixty in a fortnight,
    and only one of them fills an order."""
    old = sales([100.0] * 60, [200.0 + i for i in range(60)])
    got = look(old, Screen(min_flow=0.15), window_days=28.0)
    assert not got.passed and "поток" in got.reason
    assert got.flow == 0.0

    assert look(STEADY, Screen(min_flow=0.15), window_days=28.0).passed


def test_an_item_that_stopped_trading_is_dropped_however_good_the_history():
    quiet = sales([100.0] * 60, [90.0 + i for i in range(60)])
    got = look(quiet, Screen(max_quiet_days=14))
    assert not got.passed and "последняя продажа" in got.reason
    assert got.quiet_days == 90.0


def test_a_price_that_swings_cannot_be_pinned_down():
    wild = sales([50.0, 60.0, 70.0, 100.0, 140.0, 200.0, 260.0, 300.0] * 5)
    got = look(wild, Screen(max_spread=0.35))
    assert not got.passed and "разброс" in got.reason
    assert look(STEADY, Screen(max_spread=0.35)).passed


def test_an_item_with_no_room_for_a_trade_is_seen_without_asking():
    """If selling at the median, less the fee, does not clear even the cheap
    end of the market, no bid can be both low enough to fill and high enough
    to profit - and no request would have said otherwise."""
    flat = sales([100.0] * 40)
    got = look(flat, Screen(min_gap=0.08))
    assert not got.passed and "зазор" in got.reason

    spread_out = sales([70.0] * 10 + [100.0] * 30)
    assert look(spread_out, Screen(min_gap=0.08)).passed


def test_the_numbers_are_reported_whether_it_passes_or_not():
    """"Why was this dropped" is answered by the value, not by the word."""
    got = look(STEADY, Screen(min_price=1000.0))
    assert not got.passed
    assert got.median == 102.0 and got.sales == 60
    assert got.flow and got.flow > 0 and got.quiet_days == 0.0
    assert got.spread is not None and got.gap is not None


def test_a_profile_never_judges():
    p = profile(sales([1.0] * 3))
    assert p.passed, "measuring is not screening"
    assert p.spread is None and p.gap is None, "too few to say"
