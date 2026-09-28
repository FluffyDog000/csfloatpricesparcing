"""The ladder, checked against the cases the design was argued from.

Every test here encodes a conclusion reached by measuring a live item rather
than by reasoning: the worked examples are the specification, and if the code
stops agreeing with them it has lost the argument, not the argument the code.
"""
import pytest

from src.ladder import (LOCK_DAYS, Params, Rung, evaluate, fills_at,
                        in_range, ladder, median, price_step,
                        queue_price, rival_bid, sale_rate, snap_down,
                        top_rival)


def sale(price, f):
    return {"price": price, "float_value": f}


def order(price, lo=None, hi=None):
    return {"price": price, "float_min": lo, "float_max": hi}


# -- the pieces ------------------------------------------------------------

def test_an_order_without_float_bounds_accepts_anything():
    """The large unrestricted orders set a floor under the whole wear range."""
    book = [order(40.80, None, None)]
    assert rival_bid(book, 0.16) == 40.80
    assert rival_bid(book, 0.37) == 40.80


def test_a_rival_covering_part_of_our_range_still_counts():
    book = [order(56.80, 0.15, 0.16), order(48.30, 0.15, 0.17)]
    assert rival_bid(book, 0.155) == 56.80
    assert rival_bid(book, 0.165) == 48.30
    assert top_rival(book, 0.15, 0.17) == 56.80


def test_the_bid_is_snapped_down_never_up():
    """Rounding up would spend the margin the floor exists to protect."""
    assert snap_down(48.57, 0.10) == pytest.approx(48.50)
    assert snap_down(48.50, 0.10) == pytest.approx(48.50)


def test_the_price_grid_widens_with_the_price():
    assert price_step(48.0) == 0.10
    assert price_step(150.0) == 1.00


# -- the sell queue --------------------------------------------------------

def test_a_queue_that_clears_during_the_lock_imposes_nothing():
    """Seven lots at one sale a day: all gone before the lock lifts."""
    assert queue_price([92, 93, 95, 96, 97, 98, 99], lots=7,
                       cleared=7.0, step=0.10) is None


def test_one_cheap_lot_is_waited_out_rather_than_undercut():
    """The lone discounter that used to kill a whole band.

    Undercutting him priced the exit at $94.90 and the rung was rejected;
    waiting him out leaves the history median standing.
    """
    assert queue_price([95.0], lots=1, cleared=7.0, step=0.10) is None


def test_lots_still_standing_after_the_lock_set_the_price():
    """Nine lots, seven clear: we undercut the eighth cheapest."""
    asks = [92, 93, 95, 95, 96, 96, 97, 97, 98]
    assert queue_price(asks, lots=9, cleared=7.0,
                       step=0.10) == pytest.approx(96.90)


def test_a_wall_of_cheap_lots_drags_the_exit_right_down():
    asks = [92.0] * 8 + [93.0] * 7
    assert queue_price(asks, lots=15, cleared=7.0,
                       step=0.10) == pytest.approx(91.90)


def test_without_the_price_list_the_cheapest_is_used_as_the_survivor():
    """Only `cheapest` is stored today; the answer degrades, it does not lie."""
    assert queue_price([52.09], lots=50, cleared=38.0,
                       step=0.10) == pytest.approx(51.99)


def test_but_a_clearing_queue_needs_no_prices_at_all():
    """With the count alone we can still tell the lots will be gone."""
    assert queue_price([], lots=5, cleared=38.0, step=0.10) is None


# -- the fill rate ---------------------------------------------------------

def test_the_flow_and_our_fill_rate_are_different_numbers():
    """On a live item they differed by a factor of forty: the flow was 5.4 a
    day, our own fills 0.125. Confusing them is a mistake in multiples."""
    sales = [sale(54.0, 0.165) for _ in range(80)] + [sale(48.0, 0.165)]
    book = [order(48.30, 0.15, 0.17)]
    assert sale_rate(sales, 0.15, 0.17, 16.0) == pytest.approx(81 / 16)
    assert len(fills_at(sales, book, 0.15, 0.17, 48.50)) == 1


def test_a_seller_who_got_more_elsewhere_is_not_ours():
    sales = [sale(54.62, 0.165)]
    assert fills_at(sales, [], 0.15, 0.17, 48.50) == []


def test_a_float_where_a_rival_outbids_us_is_not_ours_either():
    sales = [sale(47.70, 0.155)]                 # cheap, but in his range
    book = [order(56.80, 0.15, 0.16)]
    assert fills_at(sales, book, 0.15, 0.17, 48.50) == []


def test_identical_sales_are_counted_separately():
    """Matching rows by value would let one rung swallow another's claim."""
    sales = [sale(48.0, 0.165), sale(48.0, 0.165)]
    assert fills_at(sales, [], 0.15, 0.17, 48.50) == [0, 1]


# -- one rung --------------------------------------------------------------

def item(n_at=54.62, cheap=(47.70, 48.40), top=0.17):
    """A band like the live one: plenty of sales, two of them cheap."""
    sales = [sale(n_at, 0.165) for _ in range(80)]
    sales += [sale(p, 0.165) for p in cheap]
    return sales


def test_a_rung_prices_at_the_margin_floor():
    p = Params(min_margin=0.05, window_days=16.0)
    r = evaluate(0.17, item(), [order(48.30, 0.15, 0.17)], (0.15, 0.38),
                 lots=0, ask_prices=[], params=p)
    assert r.take
    assert r.bid == pytest.approx(50.90, abs=0.01)
    assert r.margin >= 0.05


def test_the_median_is_used_not_a_low_quantile():
    """A low quantile understated the exit and lost rungs that cleared the
    floor honestly; the top-of-range estimate already handles the bad float."""
    p = Params()
    r = evaluate(0.17, item(), [], (0.15, 0.38), lots=0, ask_prices=[],
                 params=p)
    assert r.market == pytest.approx(54.62)


def test_a_thin_sample_is_refused_rather_than_guessed():
    p = Params(min_sample=8)
    sales = [sale(54.0, 0.165) for _ in range(3)]
    r = evaluate(0.17, sales, [], (0.15, 0.38), lots=0, ask_prices=[],
                 params=p)
    assert not r.take and "мало продаж" in r.reason


def test_a_rung_whose_flow_rivals_take_says_which_it_was():
    """Rejected for a reason a reader can act on: the cheap sales existed,
    they just went to a dearer bid."""
    p = Params()
    r = evaluate(0.17, item(), [order(60.0, 0.15, 0.17)], (0.15, 0.38),
                 lots=0, ask_prices=[], params=p)
    assert not r.take
    assert "конкуренты перебивают" in r.reason and "2" in r.reason


def test_a_rival_scoped_to_a_sliver_does_not_veto_the_rung():
    """One order covering 0.15-0.16 rejected all 23 tops of a live item when
    the gate was the range's strongest bid. It takes a sliver of the flow."""
    p = Params()
    book = [order(56.80, 0.15, 0.16), order(48.30, 0.15, 0.17)]
    r = evaluate(0.17, item(), book, (0.15, 0.38), lots=0, ask_prices=[],
                 params=p)
    assert r.take, r.reason
    assert r.rival == pytest.approx(48.30), "report the bid for what we get"


def test_a_rung_nobody_sells_into_is_refused():
    """Outbidding the book buys nothing if every seller wants more."""
    p = Params()
    sales = [sale(54.62, 0.165) for _ in range(80)]
    r = evaluate(0.17, sales, [], (0.15, 0.38), lots=0, ask_prices=[],
                 params=p)
    assert not r.take and "никто не продавал" in r.reason


def test_a_queue_too_deep_for_the_lock_binds_instead_of_the_history():
    """82 sales in 16 days clear about 36 lots during the lock, so 200 leave
    survivors, and the survivors set the price rather than the median."""
    p = Params()
    asks = [50.0] * 200
    r = evaluate(0.17, item(), [], (0.15, 0.38), lots=200, ask_prices=asks,
                 params=p)
    assert r.priced_from == "очередь"
    assert r.exit_net == pytest.approx(49.90 * (1 - p.fee))


def test_the_same_queue_costs_nothing_when_the_item_moves_fast_enough():
    """The mirror image: identical lots, but the flow disposes of them."""
    p = Params()
    r = evaluate(0.17, item(), [], (0.15, 0.38), lots=20,
                 ask_prices=[50.0] * 20, params=p)
    assert r.priced_from == "история"
    assert r.market == pytest.approx(54.62)


# -- the ladder ------------------------------------------------------------

def spread():
    """Sales across the wear range, cheaper as the float rises."""
    out = []
    for i in range(24):
        f = round(0.16 + i * 0.01, 4)
        price = 55.0 - i * 0.5
        out += [sale(price, f) for _ in range(10)]
        out.append(sale(price * 0.85, f))       # one bargain per band
    return out


def test_every_rung_starts_at_the_wear_minimum():
    """The bottom costs nothing: the top sets what the order is worth."""
    rungs = ladder(spread(), [], (0.15, 0.38))
    assert rungs and all(r.low == 0.15 for r in rungs)


def test_rejected_rungs_are_returned_with_their_reason():
    rungs = ladder(spread(), [order(80.0, None, None)], (0.15, 0.38))
    assert rungs
    assert all(not r.take for r in rungs)
    assert all(r.reason for r in rungs)


def test_a_sale_is_credited_to_one_rung_only():
    """Crediting each rung with the whole range would count sales twice and
    flatter the wider rungs."""
    rungs = ladder(spread(), [], (0.15, 0.38))
    taken = [r for r in rungs if r.take]
    assert taken
    assert sum(r.fills for r in taken) <= len(spread())


def test_the_ladder_is_ordered_by_rank():
    rungs = [r for r in ladder(spread(), [], (0.15, 0.38)) if r.take]
    assert rungs == sorted(rungs, key=lambda r: -r.rank)


def test_rank_is_return_per_dollar_per_day():
    """Ranking on money alone would let dear orders crowd out better ones."""
    rungs = [r for r in ladder(spread(), [], (0.15, 0.38)) if r.take]
    for r in rungs:
        assert r.rank == pytest.approx(r.lam * r.margin)


def test_the_lock_is_a_fact_not_a_setting():
    assert Params().lock_days == LOCK_DAYS == 7.0


def test_no_span_means_no_rungs():
    assert ladder(spread(), [], None) == []


# -- principles carried over from the model this replaced ------------------

def test_a_rung_is_priced_at_the_lot_it_will_actually_be_handed():
    """A seller keeps the good float and hands over the bad one, so the price
    comes from the sales at the top, not from the whole range."""
    sales = [sale(80.0, 0.155) for _ in range(20)]     # pristine, never ours
    sales += [sale(50.0, 0.168) for _ in range(20)]    # what we will get
    r = evaluate(0.17, sales, [], (0.15, 0.38), lots=0, ask_prices=[],
                 params=Params())
    assert r.market == pytest.approx(50.0), "the top prices it, not the range"


def test_a_wider_rung_is_priced_lower_than_a_tighter_one_inside_it():
    """The whole reason the ladder differs by its top: reaching further up the
    wear range means accepting a worse lot, and that is worth less."""
    sales = []
    for i in range(6):
        f = round(0.16 + i * 0.01, 4)
        sales += [sale(55.0 - i * 3, f) for _ in range(12)]
    p = Params()
    tight = evaluate(0.17, sales, [], (0.15, 0.38), 0, [], p)
    wide = evaluate(0.21, sales, [], (0.15, 0.38), 0, [], p)
    assert wide.market < tight.market
    assert wide.bid < tight.bid


def test_a_rival_bidding_above_what_the_rung_resells_for_takes_it_all():
    """Outbidding is pointless above the exit: there is no price at which the
    trade still pays, so the flow belongs to whoever is overpaying."""
    p = Params()
    book = [order(80.0, 0.15, 0.17)]
    r = evaluate(0.17, item(), book, (0.15, 0.38), 0, [], p)
    assert not r.take
    assert r.bid < r.rival


def test_nothing_in_the_scoring_reads_an_annualised_return():
    """Turning a margin into a rate needs the trade lock and the payout wait,
    a fortnight nothing here measures. The rank is per day of holding, and
    that is a ranking number, not a yield."""
    from dataclasses import fields

    names = {f.name for f in fields(Rung)} | {f.name for f in fields(Params)}
    for banned in ("monthly", "annual", "apr", "yield", "cycle"):
        assert not any(banned in n for n in names), banned
