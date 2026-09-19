"""What price to bid, and when not to bid at all.

Two mistakes this covers, both found by reading the numbers against CSFloat's
own rules. Prices move on a tier grid, so "$117.01" is not an order anyone can
place - between $100 and $500 the step is a dollar, and headroom is counted in
outbids rather than cents. And bidding the least that puts you at the front of
a band fills nothing when the front of that band sits far below the market:
0.15-0.17 of Big Swell had thirty-five sales in a month, of which exactly one
came in under the top-of-book, and calling that band illiquid was wrong.
"""


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

    band = plan(sales, orders, (0.15, 0.17),
                params=Params(min_sample=5, sigma_k=2.0))[0]
    assert band.market_error > 0.04, "a split market is not a known price"
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
    assert band.market == 190.0 and band.priced_from == "аск", \
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
                params=Params(min_sample=5, bid_tolerance=0.0))[0]
    assert band.entry == 117.0, "a dollar over the book makes us first"
    assert band.bid == 122.0, "but the flow only starts well above that"
    assert band.lam > 0.3, "and that is what pays for the thinner margin"


def test_the_cheapest_price_within_reach_of_the_best_is_preferred():
    """Taking the maximum outright bid over the book for a couple of points
    of turnover. Given the choice, keep the dollars and the headroom."""
    from src.pricing import Params, plan

    # Flow barely moves above the entry: one extra sale at $89, nothing after.
    rows = [(87.0, 0.28, float(i % 27)) for i in range(5)]
    rows += [(89.0, 0.28, 5.0)]
    rows += [(110.0, 0.28, float(i % 27)) for i in range(15)]
    orders = [{"price": 86.9, "qty": 1, "float_min": 0.27, "float_max": 0.29}]
    sales = _sales(rows)

    greedy = plan(sales, orders, (0.27, 0.29),
                  params=Params(min_sample=5, bid_tolerance=0.0))[0]
    thrifty = plan(sales, orders, (0.27, 0.29),
                   params=Params(min_sample=5, bid_tolerance=0.10))[0]

    assert greedy.bid == 89.0 and thrifty.bid == 87.0
    assert thrifty.bid == thrifty.entry, "nothing above the entry earned its cost"
    assert thrifty.wars > greedy.wars, "and the headroom is kept"
    assert thrifty.monthly > greedy.monthly * 0.9, "for a couple of points"
