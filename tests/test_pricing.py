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


def test_a_band_bid_below_the_market_is_not_an_illiquid_band():
    """The regression. The top of this book sits well under what the band
    actually trades at, so the cheapest price that makes us first catches
    almost nothing - while a few dollars more reaches real flow."""
    from src.pricing import Params, plan

    # Twenty sales spread $250-$290, one straggler at $240.
    rows = [(240.0, 0.16, 5.0)]
    rows += [(250.0 + i * 2, 0.16, float(i)) for i in range(20)]
    orders = [{"price": 244.0, "qty": 1, "float_min": 0.15, "float_max": 0.18}]

    band = [r for r in plan(_sales(rows), orders, (0.15, 0.17),
                            params=Params(min_sample=5))][0]
    assert band.entry == 245.0, "a dollar above the top of the book"
    assert band.take, "the band trades plenty; the cheap entry was the problem"
    assert band.bid > band.entry, \
        "the bid has to climb until real sellers are in reach"
    assert band.lam >= Params().min_lambda


def test_a_band_bid_up_past_its_worth_is_refused():
    from src.pricing import Params, plan

    rows = [(100.0 + i, 0.16, float(i)) for i in range(20)]
    # Someone is bidding above what the band resells for, net of fee.
    orders = [{"price": 140.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]

    band = plan(_sales(rows), orders, (0.15, 0.17), params=Params(min_sample=5))[0]
    assert not band.take
    assert "выше потолка" in band.reason
    assert band.ceiling < band.entry


def test_the_ceiling_is_the_last_price_that_still_pays_the_margin():
    from src.pricing import Params, plan

    rows = [(200.0, 0.16, float(i)) for i in range(20)]
    band = plan(_sales(rows), [], (0.15, 0.17),
                params=Params(min_sample=5, fee=0.02, min_margin=0.03))[0]

    # 200 sells, 2% fee leaves 196, and 3% of margin caps the bid at 190.
    assert band.market == 200.0
    assert band.ceiling == 190.0
    assert (196.0 - band.ceiling) / band.ceiling >= 0.03
    assert (196.0 - (band.ceiling + band.step)) / (band.ceiling + band.step) < 0.03


def test_headroom_is_counted_in_outbids_not_dollars():
    from src.pricing import Params, plan

    dear = [(184.0, 0.16, 1.0), (185.0, 0.16, 2.0), (186.0, 0.16, 3.0)]
    dear += [(200.0, 0.16, float(i % 27)) for i in range(20)]
    orders = [{"price": 180.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    band = plan(_sales(dear), orders, (0.15, 0.17),
                params=Params(min_sample=5, min_wars=2))[0]

    assert band.take and band.step == 1.0
    assert band.wars == int(round((band.ceiling - band.bid) / band.step))
    assert band.wars >= 2, "a position we cannot defend twice is not taken"

    # The same shape priced under $100 sits on a ten-times finer grid, so it
    # buys far more defence - which is why the unit is outbids, not dollars.
    cheap = [(88.0, 0.16, 1.0), (88.1, 0.16, 2.0), (88.2, 0.16, 3.0)]
    cheap += [(95.0, 0.16, float(i % 27)) for i in range(20)]
    rival = [{"price": 87.9, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    low = plan(_sales(cheap), rival, (0.15, 0.17),
               params=Params(min_sample=5))[0]
    assert low.take and low.step == 0.10
    assert low.wars > band.wars * 3


def test_a_margin_inside_the_price_error_is_not_a_trade():
    """At 5.6% margin on an exit price read off past sales, a three-percent
    error halves the profit; at 9.9% it takes a fifth. The scan valued both
    the same, so it would spend margin on speed exactly where the price was
    least trustworthy. A band's median carries its own error, and the margin
    has to clear it."""
    from src.pricing import Params, plan

    # Prices in two camps: the median is somewhere in the gap, and poorly
    # pinned down wherever that is.
    rows = [(p, 0.16, float(i % 27)) for i, p in
            enumerate([170.0, 175.0, 180.0, 185.0, 190.0] * 2
                      + [215.0, 220.0, 225.0, 230.0, 235.0] * 2)]
    orders = [{"price": 186.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    sales = _sales(rows)

    # The multiple is what the rule is: a margin has to clear the error by it.
    # With fills costing the listing's price rather than ours the margin is
    # bigger than it used to look, so the bar has to be set against that.
    loose_enough = plan(sales, orders, (0.15, 0.17),
                        params=Params(min_sample=5, sigma_k=2.0))[0]
    assert loose_enough.market_error > 0.04, "a split market is not a known price"
    assert loose_enough.margin > 2.0 * loose_enough.market_error
    assert loose_enough.take, "a margin well clear of the error is a trade"

    band = plan(sales, orders, (0.15, 0.17),
                params=Params(min_sample=5, sigma_k=6.0))[0]
    assert not band.take
    assert "погрешность" in band.reason

    # Told to trust the number, the same band is tradeable again.
    loose = plan(sales, orders, (0.15, 0.17),
                 params=Params(min_sample=5, sigma_k=0.0))[0]
    assert loose.take


def test_a_band_is_cut_where_the_competition_changes():
    """The regression the book made obvious: every bid over $46 was scoped to
    0.15-0.16, so pricing a 0.15-0.17 band against the whole book asked for
    $48.30 - while a lot at 0.165 had one rival at $44.40 and $44.50 took it.
    Bands have to break where other people's orders end."""
    from src.pricing import Params, plan, _bands

    orders = [
        {"price": 47.00, "qty": 1, "float_min": 0.15, "float_max": 0.158},
        {"price": 46.90, "qty": 1, "float_min": 0.15, "float_max": 0.16},
        {"price": 44.40, "qty": 1, "float_min": 0.15, "float_max": 0.18},
    ]
    edges = _bands((0.15, 0.38), 0.02, orders)
    assert (0.15, 0.158) in edges and (0.158, 0.16) in edges
    assert (0.16, 0.18) in edges, "the cheap stretch is its own band"

    rows = [(50.0 + (i % 5), 0.165, float(i)) for i in range(20)]
    bands = {(b.float_min, b.float_max): b
             for b in plan(_sales(rows), orders, (0.15, 0.38),
                           params=Params(min_sample=5))}
    free = bands[(0.16, 0.18)]
    assert free.top == 44.40, "only the order that reaches past 0.16 competes"
    assert free.entry == 44.50, "one tier step above it, not above the book"


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
                     params=Params(min_sample=5, band_step=0.02)):
        if band.sample >= 5:
            assert band.top == 180.0, "a filterless order reaches every band"


def test_rejected_bands_come_back_with_their_reason():
    """"Why not this one" is the question these numbers get read for."""
    from src.pricing import Params, plan

    rows = [(200.0, 0.16, float(i)) for i in range(20)]
    rows += [(200.0, 0.36, float(i)) for i in range(3)]      # too few to judge
    bands = plan(_sales(rows), [], (0.15, 0.38), params=Params(min_sample=5))

    assert len(bands) == 12, "every band reported, taken or not"
    thin = [b for b in bands if b.float_min == 0.35][0]
    assert not thin.take and "мало данных" in thin.reason


def test_the_live_book_prices_the_exit_when_it_is_cheaper():
    from src.pricing import Params, plan

    rows = [(200.0, 0.16, float(i)) for i in range(20)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 190.0,
              "listings": 4}]
    band = plan(_sales(rows), [], (0.15, 0.17), depth=depth,
                params=Params(min_sample=5))[0]
    # $189, not $190: matching the cheapest ask does not make us the cheapest,
    # it puts us level with it and so behind it. Selling first means going
    # under, by the one step the grid allows.
    assert band.market == 189.0 and band.priced_from == "аск", \
        "we undercut the cheapest ask to sell, whatever history says"


def test_being_first_is_not_the_goal_getting_filled_is():
    """The cheapest price that leads a band can sit under what sellers will
    take, and then it fills nothing. On one real band the minimum to lead was
    $117 with three qualifying sales a month, while $122 caught sixteen -
    five dollars more cut the wait from nine days to two."""
    from src.pricing import Params, plan

    rows = [(117.0, 0.28, 3.0), (118.0, 0.28, 9.0), (119.0, 0.28, 15.0)]
    rows += [(p, 0.28, float(i % 27)) for i, p in enumerate([121.0, 122.0] * 3)]
    rows += [(135.0, 0.28, float(i % 27)) for i in range(14)]
    orders = [{"price": 116.0, "qty": 1, "float_min": 0.27, "float_max": 0.29}]

    band = plan(_sales(rows), orders, (0.27, 0.29),
                params=Params(min_sample=5, min_lambda=0.3))[0]
    assert band.entry == 117.0, "a dollar over the book makes us first"
    assert band.bid == 122.0, "but the flow only starts well above that"
    assert band.lam > 0.3, "and that is what pays for the thinner margin"


def test_the_scan_pays_no_more_than_the_filters_require():
    """It used to take whichever price scored the best annualised return,
    which bid dollars over the book for a couple of points of turnover - on
    one glove $48.10 where $44.50 already led. Margin falls as the bid rises,
    so taking the cheapest qualifying price is both the fattest margin and the
    most headroom kept. How high it has to go at all is decided by the flow
    the filters insist on, not by an appetite for turnover."""
    from src.pricing import Params, plan

    # Flow barely moves above the entry: one extra sale at $89, nothing after.
    rows = [(87.0, 0.28, float(i % 27)) for i in range(5)]
    rows += [(89.0, 0.28, 5.0)]
    rows += [(110.0, 0.28, float(i % 27)) for i in range(15)]
    orders = [{"price": 86.9, "qty": 1, "float_min": 0.27, "float_max": 0.29}]
    sales = _sales(rows)

    band = plan(sales, orders, (0.27, 0.29), params=Params(min_sample=5))[0]
    assert band.bid == 87.0 == band.entry, "nothing above the entry was needed"

    # Insist on more flow and the scan has to climb - but only as far as that
    # insistence reaches, and no further.
    faster = plan(sales, orders, (0.27, 0.29),
                  params=Params(min_sample=5, min_lambda=0.21))[0]
    assert faster.bid == 89.0
    # Raising the bid does not raise what the cheap fills cost, so the margin
    # barely moves - the headroom is what actually gets spent.
    assert faster.margin <= band.margin
    assert band.wars > faster.wars, "headroom is what speed is bought with"


def test_a_thin_band_borrows_from_its_neighbours_instead_of_being_dropped():
    """Eight sales in a 0.02 slice of a wear is a lot to ask, and "мало
    данных" was the commonest reason a band was dropped. Price moves with
    float smoothly, so the sales either side say a great deal about the ones
    inside - throwing them away to honour an edge we invented throws away most
    of the evidence."""
    from src.pricing import Params, evaluate

    # Priced from 200 down to 100 across the wear, two sales per 0.02 band -
    # never enough for a band to stand on its own.
    sales = [{"price": 200.0 - (f := 0.15 + i * 0.01) * 333.3, "float_value": f,
              "age_days": i % 14}
             for i in range(30)]
    book = [{"price": 100.0, "qty": 1, "float_min": 0.15, "float_max": 0.45}]

    got = evaluate(0.30, 0.32, sales, book, (0.15, 0.38), (),
                   Params(min_sample=8, max_reach=0.05))
    assert got.sample < 8, "it really does not have its own"
    assert got.borrowed >= 8 and got.reach is not None
    assert got.priced_from == "соседи"
    # Priced at 0.32, the band's high-float edge, not at its middle: that is
    # the lot the filter will actually be handed.
    assert got.market == pytest.approx(200.0 - 0.32 * 333.3, rel=0.02)


def test_borrowing_stops_where_the_neighbours_stop_being_the_same_thing():
    from src.pricing import Params, evaluate

    sales = [{"price": 100.0, "float_value": 0.15 + i * 0.001,
              "age_days": 1} for i in range(40)]
    got = evaluate(0.40, 0.42, sales, [], (0.15, 0.38), (),
                   Params(min_sample=8, max_reach=0.05))
    assert not got.take and "за" in got.reason and "float" in got.reason


def test_a_borrowed_price_carries_more_doubt_than_a_measured_one():
    """Reaching is itself a doubt. Sales lying exactly on a line say the line
    fits, not that it keeps holding a dozen bands further out."""
    from src.pricing import neighbourhood

    tight = [{"price": 200.0 - i * 0.333, "float_value": 0.15 + i * 0.001}
             for i in range(300)]
    close = neighbourhood(tight, 0.30, 0.32, 12)
    sparse = [{"price": 200.0 - i * 6.0, "float_value": 0.15 + i * 0.02}
              for i in range(16)]
    far = neighbourhood(sparse, 0.30, 0.32, 12)

    assert far[2] > close[2], "the sparse one had to reach further"
    assert far[1] > close[1], "and says so in its error"


def test_a_band_with_its_own_sales_does_not_borrow():
    from src.pricing import Params, evaluate

    sales = [{"price": 100.0 + (i % 5), "float_value": 0.305 + (i % 10) * 0.001,
              "age_days": i % 14} for i in range(40)]
    got = evaluate(0.30, 0.32, sales, [], (0.15, 0.38), (),
                   Params(min_sample=8))
    assert got.sample == 40 and got.borrowed == 0
    assert got.priced_from == "история"


def test_the_doubt_in_a_borrowed_price_does_not_depend_on_the_band_step():
    """How confidently a price extrapolates depends on how far it reached, not
    on how finely we chose to slice. Counting the reach in band widths made
    narrowing the step - a setting, not a fact about the market - inflate the
    doubt on its own, and the ranking reads that doubt."""
    from src.pricing import neighbourhood

    sparse = [{"float_value": 0.15 + i * 0.02, "price": 200.0 - i * 6.0}
              for i in range(16)]
    wide = neighbourhood(sparse, 0.30, 0.32, 12)
    narrow = neighbourhood(sparse, 0.305, 0.315, 12)

    assert wide[2] == narrow[2], "both reached the same distance"
    assert abs(wide[1] - narrow[1]) < 0.01, \
        f"halving the band step changed the error: {wide[1]} vs {narrow[1]}"


def test_reaching_further_still_costs_more():
    from src.pricing import neighbourhood

    dense = [{"float_value": 0.15 + i * 0.001, "price": 200.0 - i * 0.3}
             for i in range(300)]
    sparse = [{"float_value": 0.15 + i * 0.02, "price": 200.0 - i * 6.0}
              for i in range(16)]
    assert neighbourhood(sparse, 0.30, 0.32, 12)[1] > \
        neighbourhood(dense, 0.30, 0.32, 12)[1]


def test_a_band_is_priced_at_the_lot_it_will_actually_be_handed():
    """An order filters on a float range, and the sellers who take it are the
    ones your bid suits - the cheap end of the range, which for float means
    the high end. Pricing the band at its median is pricing an item you will
    not receive, and on a float-sensitive skin that gap is the whole margin."""
    from src.pricing import Params, evaluate

    # 0.15 -> $200, 0.38 -> $100: float drives the price hard, as on gloves.
    sales = [{"price": 200.0 - (f := 0.15 + i * 0.0023) * 434.8 + 65.2,
              "float_value": f, "age_days": i % 14} for i in range(100)]
    got = evaluate(0.20, 0.24, sales, [], (0.15, 0.38), (), Params())

    centre = 200.0 - (0.22 * 434.8 - 65.2)
    edge = 200.0 - (0.24 * 434.8 - 65.2)
    assert got.market == pytest.approx(edge, rel=0.03)
    assert got.market < centre * 0.98, "the middle would have flattered it"


def test_a_skin_whose_price_ignores_float_is_not_penalised_for_it():
    """The correction is exactly as large as the slope. Flat prices, no
    slope, nothing to correct - so the fix costs nothing where it is not
    needed, and no threshold has to be guessed at."""
    from src.pricing import Params, evaluate

    sales = [{"price": 100.0 + (i % 5), "float_value": 0.15 + (i % 23) * 0.01,
              "age_days": i % 14} for i in range(200)]
    got = evaluate(0.20, 0.24, sales, [], (0.15, 0.38), (), Params())
    assert got.market == pytest.approx(102.0, rel=0.03)


def test_a_wide_band_is_priced_lower_than_a_narrow_one_inside_it():
    """Which is the real cost of a wide band: one price for a range means the
    price of the worst thing in the range."""
    from src.pricing import Params, evaluate

    sales = [{"price": 200.0 - (f := 0.15 + i * 0.0023) * 434.8 + 65.2,
              "float_value": f, "age_days": i % 14} for i in range(100)]
    wide = evaluate(0.20, 0.28, sales, [], (0.15, 0.38), (), Params())
    narrow = evaluate(0.20, 0.22, sales, [], (0.15, 0.38), (), Params())
    assert wide.market < narrow.market


def test_matching_the_cheapest_ask_is_not_being_the_cheapest():
    """The whole return is figured on getting out at the exit price. Taking
    the ask itself claimed the front of the queue while standing second in
    it - and the second listing sells after the first, not with it."""
    from src.pricing import Params, plan

    rows = [(200.0, 0.16, float(i)) for i in range(20)]
    for ask, expect in ((190.0, 189.0),      # $10-100 tier is $0.10; this is $1
                        (60.0, 59.9),
                        (9.0, 8.95)):
        depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": ask,
                  "listings": 4}]
        band = plan([{"price": p, "float_value": f, "age_days": a}
                     for p, f, a in [(ask * 1.5, 0.16, float(i))
                                     for i in range(20)]],
                    [], (0.15, 0.17), depth=depth,
                    params=Params(min_sample=5))[0]
        assert band.market == expect, f"ask {ask} -> {band.market}, want {expect}"


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
                params=Params(band_step=0.01, min_sample=5))[0]
    assert band.float_min == 0.15 and band.float_max == 0.16
    # A 0.16 lot is worth far more than that listing's $210; the ask must not
    # drag our exit price down to it.
    assert band.market > 230.0, f"exit priced at {band.market}"


def test_a_genuinely_cheaper_ask_still_prices_the_exit():
    """The carry is a correction, not a way to ignore the book."""
    from src.pricing import Params, plan

    sales = [{"price": 300.0, "float_value": 0.155 + (i % 5) * 0.001,
              "age_days": i % 14} for i in range(60)]
    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 250.0,
              "listings": 3}]
    band = plan(sales, [], (0.15, 0.18), depth=depth,
                params=Params(band_step=0.01, min_sample=5))[0]
    assert band.priced_from == "аск" and band.market < 250.0


def test_a_wild_slope_cannot_reprice_a_listing_out_of_recognition():
    from src.pricing import _exit_price

    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 100.0}]
    high = _exit_price(1000.0, depth, 0.15, 0.16, "история",
                       at=0.155, slope=-50000.0)[0]
    low = _exit_price(1000.0, depth, 0.15, 0.16, "история",
                      at=0.155, slope=50000.0)[0]
    assert 40.0 <= low <= 210.0 and 40.0 <= high <= 210.0


def test_one_listing_says_nothing_about_where_in_its_band_it_sits():
    """With a single lot there is no reason to think it is at either edge, so
    the middle it is. Inventing an edge would be inventing a fact in whichever
    direction flattered the answer."""
    from src.pricing import _exit_price

    depth_one = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 200.0,
                  "listings": 1}]
    depth_many = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 200.0,
                   "listings": 20}]
    slope = -5000.0     # $50 per 0.01 of float

    one = _exit_price(999.0, depth_one, 0.15, 0.16, "история",
                      at=0.16, slope=slope)[0]
    many = _exit_price(999.0, depth_many, 0.15, 0.16, "история",
                       at=0.16, slope=slope)[0]

    # $200 sits in the $100-500 tier, where the grid step is a dollar.
    assert one == 199.0, "middle of the band is our own float: no carry"
    assert many > one, "twenty lots: its cheapest is near the worst float"


def test_the_scan_takes_the_cheapest_price_that_clears_the_filters():
    """Margin falls as the bid rises, so the cheapest qualifying price is the
    fattest margin: the two rules are one rule. What decides how high it has
    to go is the flow the filters insist on."""
    from src.pricing import Params, plan

    rows = [(90.0, 0.28, float(i % 20)) for i in range(6)]
    rows += [(95.0, 0.28, float(i % 20)) for i in range(6)]
    rows += [(130.0, 0.28, float(i % 20)) for i in range(20)]
    orders = [{"price": 89.0, "qty": 1, "float_min": 0.27, "float_max": 0.29}]
    sales = _sales(rows)

    slow = plan(sales, orders, (0.27, 0.29),
                params=Params(min_sample=5, min_lambda=0.01))[0]
    fast = plan(sales, orders, (0.27, 0.29),
                params=Params(min_sample=5, min_lambda=0.3))[0]

    assert slow.bid < fast.bid, "insisting on flow is what raises the bid"
    assert slow.margin > fast.margin, "and margin is what pays for it"


def test_nothing_in_the_scoring_reads_an_annualised_return_any_more():
    """It decided which price to bid and which band to hold, and it was a
    margin divided by a cycle built from a measured flow, an assumed sale rate
    and a trade lock. The number is still reported; it no longer chooses."""
    import inspect

    from src import executor, pricing

    # The bid is chosen on price. `entry_monthly` is still carried on the row
    # afterwards, which is reporting, not choosing - so the selection line is
    # what gets asserted rather than the whole block.
    assert "min(found, key=lambda b: b.bid)" in inspect.getsource(pricing.evaluate)
    assert "band.monthly" not in inspect.getsource(executor.rank)
    assert "margin" in inspect.getsource(executor.rank)


def _spread(n=460, lo=0.15, hi=0.38, base=130.0, days=21):
    """An item trading steadily across a whole wear."""
    out = []
    for i in range(n):
        f = lo + (hi - lo) * (i % 100) / 100.0
        out.append({"price": round(base * (0.85 + (i % 7) / 20.0), 2),
                    "float_value": round(f, 4),
                    "age_days": float(i % days)})
    return out


def test_a_narrow_band_is_no_longer_starved_of_flow():
    """A 0.01 slice of Field-Tested is a twenty-third of it, so counting only
    the sales inside gave one or two in three weeks - from which no rate can
    be measured. Zero sales in a slice is not evidence of zero flow, and that
    was the evidence bands were being rejected on."""
    from src.pricing import Params, band_flow

    sales = _spread()
    inside = [s for s in sales
              if 0.20 <= s["float_value"] < 0.21 and s["age_days"] <= 21]
    flow = band_flow(sales, 0.20, 0.21, 21.0, reach=0.03, slope=0.0, at=0.21)

    assert flow.observed > len(inside) * 3, \
        "the window holds far more than the slice"
    assert flow.rate > 0, "and turns into a rate rather than a coin toss"


def test_the_rate_is_scaled_to_the_band_not_to_the_window():
    """Borrowing neighbours to measure a rate would otherwise credit a 0.01
    band with a 0.06 window's worth of trade."""
    from src.pricing import band_flow

    sales = _spread()
    narrow = band_flow(sales, 0.20, 0.21, 21.0, reach=0.03, slope=0.0, at=0.21)
    wide = band_flow(sales, 0.20, 0.26, 21.0, reach=0.03, slope=0.0, at=0.26)
    assert wide.rate > narrow.rate * 3, \
        f"a six times wider band should trade far more: {wide.rate} vs {narrow.rate}"


def test_flow_at_a_bid_is_the_share_of_prices_under_it():
    from src.pricing import band_flow

    sales = _spread()
    flow = band_flow(sales, 0.20, 0.21, 21.0, reach=0.03, slope=0.0, at=0.21)
    assert flow.at(0.0) == 0.0
    assert flow.at(10_000.0) == pytest.approx(flow.rate)
    assert 0 < flow.at(130.0) < flow.rate


def test_an_uncontested_band_may_be_bid_below_the_old_floor():
    """Nobody bidding means nothing forces a floor: any price leads a band of
    one. Starting at a flat 85% of market simply forbade the cheaper half."""
    from src.pricing import Params, plan

    sales = _spread()
    band = plan(sales, [], (0.15, 0.38), params=Params(
        band_step=0.02, min_sample=5, min_lambda=0.05, min_wars=0,
        sigma_k=0.0))[0]

    assert band.take, band.reason
    assert band.entry < band.market * 0.85, \
        f"entry {band.entry} was floored at 85% of {band.market}"
    assert band.bid >= band.entry


def test_a_contested_band_still_starts_above_the_book():
    """Where there is competition the floor is real: below the top bid we are
    not first, and strict price priority means we do not fill."""
    from src.pricing import Params, next_above, plan

    sales = _spread()
    book = [{"price": 120.0, "qty": 1, "float_min": 0.15, "float_max": 0.38}]
    band = plan(sales, book, (0.15, 0.38), params=Params(
        band_step=0.02, min_sample=5, min_lambda=0.05, min_wars=0))[0]
    assert band.entry == next_above(120.0)


def test_borrowing_neighbours_does_not_invent_flow_that_is_not_there():
    """The estimate has to be honest in both directions. A slow item sliced
    thinly really does trade rarely, and the fix is meant to measure that -
    not to make every band look busy enough to pass."""
    from src.pricing import band_flow

    # One sale a day across a whole wear: a 0.01 slice gets a twenty-third.
    sales = [{"price": 130.0, "float_value": round(0.15 + 0.23 * (i % 100) / 100, 4),
              "age_days": float(i % 21)} for i in range(21)]
    flow = band_flow(sales, 0.20, 0.21, 21.0, reach=0.03, slope=0.0, at=0.21)

    assert flow.observed >= 3, "the window sees more than the slice"
    assert flow.rate < 0.1, f"but a slow item stays slow: {flow.rate}"
    assert 1 / flow.rate > 10, "one sale every week or two, and it says so"


def test_a_fill_costs_the_listing_price_not_our_bid():
    """A buy order does not wait for someone who means to sell to it: it takes
    any listing that appears at or under it, and CSFloat charges the listing's
    price. The orders that pay are the ones that catch a seller who priced
    their lot as an ordinary example of the skin without noticing what its
    float is worth - and that seller's price is the one we pay."""
    from src.pricing import Flow

    flow = Flow(rate=0.8, observed=9,
                prices=(155.0, 158.0, 161.0, 164.0, 167.0,
                        170.0, 176.0, 181.0, 188.0))
    assert flow.paid(163.0) == 158.0
    assert flow.paid(175.0) == 162.5
    assert flow.paid(100.0) is None, "nothing fills, nothing is paid"


def test_raising_the_bid_does_not_raise_what_the_cheap_lots_cost():
    """Which is the whole point: a higher order catches more mispriced
    listings without paying more for the ones already caught."""
    from src.pricing import Flow

    flow = Flow(rate=0.8, observed=9,
                prices=(155.0, 158.0, 161.0, 164.0, 167.0,
                        170.0, 176.0, 181.0, 188.0))
    net = 181.0
    cheap = (net - flow.paid(163.0)) / flow.paid(163.0)
    dear = (net - flow.paid(175.0)) / flow.paid(175.0)
    assert dear > cheap * 0.7, \
        f"costing every fill at the bid would have shown a collapse: {cheap} -> {dear}"


def test_the_ceiling_still_bounds_the_worst_case():
    """The ordinary case is the listing's price; the worst is our own bid, and
    the ceiling is what guarantees even that much leaves a margin."""
    from src.pricing import Params, plan

    sales = _spread()
    band = plan(sales, [], (0.15, 0.38), params=Params(
        band_step=0.02, min_sample=5, min_lambda=0.05, min_wars=0,
        sigma_k=0.0, min_margin=0.03))[0]

    assert band.take and band.bid <= band.ceiling
    assert band.margin_worst == pytest.approx(
        (band.market * 0.98 - band.bid) / band.bid)
    assert band.margin_worst >= 0.03 - 1e-9, "the ceiling's promise"
    assert band.paid <= band.bid
    assert band.margin >= band.margin_worst, "and the ordinary case is better"
