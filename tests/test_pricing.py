"""What price to bid, and when not to bid at all.

Two mistakes this covers, both found by reading the numbers against CSFloat's
own rules. Prices move on a tier grid, so "$117.01" is not an order anyone can
place - between $100 and $500 the step is a dollar, and headroom is counted in
outbids rather than cents. And bidding the least that puts you at the front of
a band fills nothing when the front of that band sits far below the market:
0.15-0.17 of Big Swell had thirty-five sales in a month, of which exactly one
came in under the top-of-book, and calling that band illiquid was wrong.
"""

import pytest


def test_only_prices_on_the_tier_grid_exist():
    from src.pricing import increment, next_above, snap_down, snap_up

    assert increment(3.50) == 0.01
    assert increment(7.00) == 0.05
    assert increment(13.37) == 0.10
    assert increment(117.0) == 1.00
    assert increment(640.0) == 5.00
    assert increment(1500.0) == 10.00

    # CSFloat's own example: $13.37 is invalid in the $10-$100 tier.
    assert snap_down(13.37) == 13.30
    assert snap_up(13.37) == 13.40

    # Outbidding a glove costs a dollar, not a cent.
    assert next_above(117.0) == 118.0
    assert next_above(186.13) == 187.0
    assert next_above(3.50) == 3.51

    # A tier boundary is where a war gets ten times cheaper to fight.
    assert increment(99.0) == 0.10 and increment(101.0) == 1.00


def _sales(rows):
    return [{"price": p, "float_value": f, "age_days": a} for p, f, a in rows]


def test_a_rival_ending_where_a_band_starts_does_not_compete_in_it():
    """Bands are half-open. An order capped at 0.16 belongs to the band below
    it; counting it above would price every band off its neighbour's fight."""
    from src.pricing import _competing

    capped = [{"price": 46.90, "qty": 1, "float_min": 0.15, "float_max": 0.16}]
    assert _competing(capped, 0.15, 0.16, (0.15, 0.38)) == capped
    assert _competing(capped, 0.16, 0.18, (0.15, 0.38)) == []


def test_a_rival_scoped_to_part_of_a_band_still_competes():
    """Where a rival's edge falls inside a band that cannot be split further,
    it still takes lots from it - covering the band is not required."""
    from src.pricing import _competing

    partial = [{"price": 185.0, "qty": 1, "float_min": 0.152, "float_max": 0.155}]
    assert _competing(partial, 0.15, 0.16, (0.15, 0.38)) == partial


def test_an_unscoped_order_competes_everywhere():
    from src.pricing import Params, plan

    rows = [(200.0, f, float(i)) for i, f in enumerate([0.16, 0.36] * 10)]
    wide = [{"price": 180.0, "qty": 2, "float_min": None, "float_max": None}]
    rows_sales = _sales(rows)
    for band in plan(rows_sales, wide, (0.15, 0.38),
                     params=Params(min_sample=5)):
        if band.sample >= 5:
            assert band.top == 180.0, "a filterless order reaches every band"


def test_a_skin_whose_price_ignores_float_is_not_penalised_for_it():
    """The correction is exactly as large as the slope. Flat prices, no
    slope, nothing to correct - so the fix costs nothing where it is not
    needed, and no threshold has to be guessed at."""
    from src.pricing import Params, evaluate

    sales = [{"price": 100.0 + (i % 5), "float_value": 0.15 + (i % 23) * 0.01,
              "age_days": i % 14} for i in range(200)]
    got = evaluate(0.20, 0.24, sales, [], (0.15, 0.38), (), Params())
    assert got.market == pytest.approx(102.0, rel=0.03)


def test_an_ask_above_the_history_does_not_raise_the_exit_price():
    """Undercutting a dear ask is not evidence the lot sells for more."""
    from src.pricing import Params, plan

    rows = [(100.0, 0.16, float(i)) for i in range(20)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 500.0,
              "listings": 1}]
    band = plan(_sales(rows), [], (0.15, 0.17), depth=depth,
                params=Params(min_sample=5))[0]
    assert band.market == 100.0 and band.priced_from != "аск"


def test_a_cheaper_ask_on_a_worse_float_does_not_set_our_price():
    """Listings are read in bands of 0.02 while a band may be 0.01, so the
    cheapest ask overlapping ours can be a far worse float. It is cheaper
    because it is worse, not because it undercuts us - taking its price sells
    a 0.155 lot at a 0.169 lot's price."""
    from src.pricing import Params, plan

    # $300 at 0.15 falling to $200 at 0.17: float drives the price hard.
    sales = [{"price": 300.0 - (f := 0.15 + (i % 20) * 0.001) * 5000.0 + 750.0,
              "float_value": f, "age_days": i % 14} for i in range(120)]
    # Six listings in the band, so its cheapest is very likely the worst
    # float among them - priced for a 0.169, not for our 0.16.
    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 210.0,
              "listings": 6}]

    band = plan(sales, [], (0.15, 0.18), depth=depth,
                params=Params(min_sample=5))[0]
    assert band.float_min == 0.15 and band.float_max == 0.16
    # A 0.16 lot is worth far more than that listing's $210; the ask must not
    # drag our exit price down to it.
    assert band.market > 230.0, f"exit priced at {band.market}"


def _spread(n=460, lo=0.15, hi=0.38, base=130.0, days=21):
    """An item trading steadily across a whole wear."""
    out = []
    for i in range(n):
        f = lo + (hi - lo) * (i % 100) / 100.0
        out.append({"price": round(base * (0.85 + (i % 7) / 20.0), 2),
                    "float_value": round(f, 4),
                    "age_days": float(i % days)})
    return out


def test_the_ceiling_still_bounds_the_worst_case():
    """The ordinary case is the listing's price; the worst is our own bid, and
    the ceiling is what guarantees even that much leaves a margin."""
    from src.pricing import Params, plan

    sales = _spread()
    band = plan(sales, [], (0.15, 0.38), params=Params(
        min_sample=5, min_margin=0.03))[0]

    assert band.take and band.bid <= band.ceiling
    assert band.margin_worst == pytest.approx(
        (band.market * 0.98 - band.bid) / band.bid)
    assert band.margin_worst >= 0.03 - 1e-9, "the ceiling's promise"
    assert band.paid <= band.bid
    assert band.margin >= band.margin_worst, "and the ordinary case is better"




def test_a_band_carries_both_exit_candidates_not_only_the_winner():
    """The dashboard has to show what the queue said even when the history
    outbid it: "история" alone cannot tell a queue a cent away from binding
    from one nowhere near."""
    import datetime as dt

    from src.pricing import Params, plan

    sales = [{"price": 110.0, "float_value": 0.165, "age_days": i % 14 + 0.5}
             for i in range(40)]
    sales += [{"price": 96.0, "float_value": 0.165, "age_days": i + 0.5}
              for i in range(4)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "listings": 40,
              "cheapest": 95.0,
              "asks": [[95.0, 0.165]] + [[108.0, 0.165]] * 39}]
    bands = plan(sales, [], (0.15, 0.38), depth, Params(window_days=16.0))
    priced = [b for b in bands if b.market is not None]
    assert priced, "the fixture has to price something"
    assert any(b.queue_price is not None for b in priced), \
        "what the queue allowed is reported, win or lose"
    assert all(b.lots_cleared >= 0 for b in priced)


def test_a_band_carries_the_lot_prices_it_would_queue_behind():
    """Only lots our own item could displace: a lot with a worse float is not
    competition for the buyer who wants ours."""
    from src.pricing import Params, plan

    sales = [{"price": 110.0, "float_value": 0.155, "age_days": i % 14 + 0.5}
             for i in range(40)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "listings": 3,
              "cheapest": 95.0,
              "asks": [[95.0, 0.155], [99.0, 0.159], [101.0, 0.168]]}]
    band = next(b for b in plan(sales, [], (0.15, 0.38), depth,
                                Params(window_days=16.0))
                if abs(b.float_max - 0.16) < 1e-9)
    assert band.asks == [95.0, 99.0], "the 0.168 lot is a worse item than ours"


def test_the_price_list_is_truncated_rather_than_shipped_whole():
    """Fifty prices on every rung of every item is a payload nobody reads."""
    from src.pricing import Params, plan

    sales = [{"price": 110.0, "float_value": 0.155, "age_days": i % 14 + 0.5}
             for i in range(40)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "listings": 50,
              "cheapest": 90.0,
              "asks": [[90.0 + i, 0.155] for i in range(50)]}]
    band = next(b for b in plan(sales, [], (0.15, 0.38), depth,
                                Params(window_days=16.0))
                if abs(b.float_max - 0.16) < 1e-9)
    assert len(band.asks) == 8 and band.asks[0] == 90.0
