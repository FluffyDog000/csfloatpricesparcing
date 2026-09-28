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
        min_sample=5, min_margin=0.03))[0]

    assert band.take and band.bid <= band.ceiling
    assert band.margin_worst == pytest.approx(
        (band.market * 0.98 - band.bid) / band.bid)
    assert band.margin_worst >= 0.03 - 1e-9, "the ceiling's promise"
    assert band.paid <= band.bid
    assert band.margin >= band.margin_worst, "and the ordinary case is better"


def test_a_reading_for_our_own_range_beats_one_that_merely_overlaps():
    """Asking for the float we hold returns the lots our order can actually
    buy. A 0.02 grid band overlapping it may be quoting a far worse float, and
    that is the reading that needed carrying between floats in the first
    place."""
    from src.pricing import _exit_price

    depth = [
        # Stale grid band: cheapest is a much worse float, so much cheaper.
        {"float_min": 0.15, "float_max": 0.17, "cheapest": 150.0, "listings": 8},
        # Fresh reading for exactly our range.
        {"float_min": 0.15, "float_max": 0.16, "cheapest": 190.0, "listings": 3},
    ]
    price, source = _exit_price(999.0, depth, 0.15, 0.16, "история",
                                at=0.16, slope=0.0)
    assert source == "аск"
    assert price == 189.0, f"the exact reading should set it, got {price}"


def test_an_overlapping_reading_is_still_used_when_there_is_nothing_exact():
    from src.pricing import _exit_price

    depth = [{"float_min": 0.15, "float_max": 0.17, "cheapest": 150.0,
              "listings": 8}]
    price, source = _exit_price(999.0, depth, 0.15, 0.16, "история",
                                at=0.16, slope=0.0)
    assert source == "аск" and price == 149.0
