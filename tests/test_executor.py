"""Turning a plan into actions, without touching the network.

This is the part that would spend money, so every rule it encodes is one we
argued out against a real order book: withdraw at the ceiling rather than
chase, answer an outbid only when the queue it creates is worse than the wait
we accept, and cap one item's share before the portfolio is assembled.
"""
from src.executor import CANCEL, KEEP, PLACE, RAISE, Action, Limits, reconcile, select
from src.pricing import Band


def band(lo, hi, bid, ceiling, monthly=0.5, lam=1.0, wars=5):
    return Band(float_min=lo, float_max=hi, bid=bid, ceiling=ceiling,
                monthly=monthly, lam=lam, wars=wars, take=True)


def row(lo, hi, price, ceiling, oid=1):
    return {"id": oid, "float_min": lo, "float_max": hi, "price": price,
            "ceiling": ceiling, "remote_id": f"r{oid}", "state": "live"}


def test_nothing_is_placed_without_a_budget():
    """The default is zero, so an unconfigured bot cannot spend."""
    got = select([band(0.15, 0.17, 100.0, 110.0)], Limits())
    assert got == []


def test_one_item_cannot_crowd_out_the_rest():
    bands = [band(round(0.15 + i / 100, 4), round(0.16 + i / 100, 4),
                  100.0, 110.0, monthly=1 - i / 10) for i in range(6)]
    got = select(bands, Limits(total_capital=1000.0, max_orders_per_item=3))
    assert len(got) == 3, "the per-item cap binds before the money does"
    assert [b.float_min for b in got] == [0.15, 0.16, 0.17], "best first"

    tight = select(bands, Limits(total_capital=250.0, max_orders_per_item=3))
    assert len(tight) == 2, "and the money binds when it is the smaller cap"


def test_capital_already_committed_reduces_what_is_left():
    bands = [band(0.15, 0.17, 100.0, 110.0)]
    assert select(bands, Limits(total_capital=150.0), spent=100.0) == []


def test_an_order_nobody_is_bidding_against_is_left_alone():
    acts = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0)],
                     [row(0.15, 0.17, 100.0, 110.0)], [], Limits())
    assert [a.kind for a in acts] == [KEEP]


def test_being_outbid_is_not_by_itself_a_reason_to_answer():
    """Whoever went above us is filled first, and then we lead again for
    nothing. What costs money is the wait, so that is what decides."""
    book = [{"price": 101.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    patient = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0, lam=1.0)],
                        [row(0.15, 0.17, 100.0, 110.0)], book,
                        Limits(patience_minutes=20160.0))
    assert patient[0].kind == KEEP
    assert "очередь разойдётся" in patient[0].reason

    # The same outbid on a band that trades once a month is a real delay.
    slow = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0, lam=0.03)],
                     [row(0.15, 0.17, 100.0, 110.0)], book,
                     Limits(patience_minutes=20160.0))
    assert slow[0].kind == RAISE
    assert slow[0].price == 102.0, "one tier step over the rival"
    assert slow[0].was == 100.0


def test_a_position_bid_past_its_ceiling_is_abandoned_not_chased():
    # Answering this rival would cost $111, and $110 is the most the trade
    # is worth; the ceiling itself is still a price we would pay.
    book = [{"price": 110.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    acts = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0, lam=0.03)],
                     [row(0.15, 0.17, 100.0, 110.0)], book,
                     Limits(patience_minutes=1440.0))
    assert acts[0].kind == CANCEL
    assert "выше потолка" in acts[0].reason


def test_an_order_whose_ceiling_has_fallen_under_it_is_pulled():
    """The market moved down; what we are bidding is no longer worth paying."""
    acts = reconcile("Gloves", [band(0.15, 0.17, 95.0, 96.0)],
                     [row(0.15, 0.17, 100.0, 110.0)], [], Limits())
    assert acts[0].kind == CANCEL and "выше потолка" in acts[0].reason


def test_a_band_that_stopped_qualifying_is_cancelled():
    acts = reconcile("Gloves", [], [row(0.15, 0.17, 100.0, 110.0)], [], Limits())
    assert acts[0].kind == CANCEL
    assert "не проходит" in acts[0].reason
    assert acts[0].remote_id == "r1", "the site's id travels with the action"


def test_a_wanted_band_with_no_order_is_placed():
    acts = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0)], [], [], Limits())
    assert [a.kind for a in acts] == [PLACE]
    assert acts[0].price == 100.0 and acts[0].ceiling == 110.0


def test_cancels_come_before_places():
    """Freeing capital first is what lets the placements fit inside it."""
    acts = reconcile("Gloves", [band(0.20, 0.22, 90.0, 99.0)],
                     [row(0.15, 0.17, 100.0, 110.0)], [], Limits())
    assert [a.kind for a in acts] == [CANCEL, PLACE]


def test_exposure_follows_the_actions():
    from src.executor import exposure

    acts = [Action(PLACE, "A", 0.15, 0.17, 100.0, 110.0, ""),
            Action(CANCEL, "A", 0.20, 0.22, 40.0, 44.0, ""),
            Action(RAISE, "B", 0.15, 0.17, 55.0, 60.0, "", was=50.0)]
    assert exposure(acts, {"A": 140.0, "B": 50.0}) == {"A": 200.0, "B": 55.0}


def test_patience_is_counted_in_minutes():
    """The defence re-reads the book every hour or faster, so the question it
    asks about a queue is "will this clear before I look again". A threshold in
    days is coarser than the look itself: it answers "wait" to everything, and
    an order sits outbid for a fortnight by design."""
    from src.executor import Limits, human_wait, reconcile

    band = Band(float_min=0.15, float_max=0.17, bid=10.0, ceiling=12.0,
                step=0.10, lam=96.0, take=True)  # liquid: four fills an hour
    held = [{"id": 1, "float_min": 0.15, "float_max": 0.17, "price": 10.0,
             "ceiling": 12.0, "remote_id": "r1"}]
    book = [{"price": 10.5, "qty": 1, "float_min": 0.15, "float_max": 0.17}]

    # One rival ahead of us, four fills an hour: the queue clears in half an
    # hour. Under the old day-scale threshold that was "wait" either way.
    waiting = reconcile("x", [band], held, book, Limits(patience_minutes=60))
    assert waiting[0].kind == KEEP and "мин" in waiting[0].reason

    answering = reconcile("x", [band], held, book, Limits(patience_minutes=10))
    assert answering[0].kind == RAISE
    assert answering[0].price > 10.5, "a raise has to clear the rival"


def test_a_wait_is_reported_in_the_unit_a_person_would_say_it_in():
    from src.executor import human_wait

    assert human_wait(1 / 48) == "30 мин"          # half an hour
    assert human_wait(0.25) == "6.0 ч"
    assert human_wait(9.0) == "9.0 дн"
    assert human_wait(float("inf")) == "никогда"


def test_the_plan_stays_under_what_the_balance_allows():
    """CSFloat lets outstanding orders run to ten times the balance. The
    allowance is real but it is not money: an order whose turn comes while the
    balance is short is removed, not queued. So it caps the plan."""
    from src.executor import LEVERAGE, Limits

    l = Limits(total_capital=20000.0, balance=960.0)
    assert l.allowance == 9600.0 == 960.0 * LEVERAGE
    assert l.budget == 9600.0, "their ceiling, not ours"
    assert l.capped_by_balance

    under = Limits(total_capital=2000.0, balance=960.0)
    assert under.budget == 2000.0 and not under.capped_by_balance

    # Not told the balance: only our own limit applies.
    unknown = Limits(total_capital=2000.0)
    assert unknown.allowance == float("inf") and unknown.budget == 2000.0
    assert unknown.as_dict()["allowance"] is None


def test_selection_spends_the_allowed_budget_not_the_asked_one():
    bands = [Band(float_min=0.0 + i / 100, float_max=0.01 + i / 100,
                  bid=100.0, monthly=1.0 - i / 100, take=True)
             for i in range(10)]
    # Asked for $5000, balance only covers $300 of outstanding orders.
    got = select(bands, Limits(total_capital=5000.0, balance=30.0,
                               max_orders=10, max_orders_per_item=10))
    assert sum(b.bid for b in got) <= 300.0
    assert len(got) == 3


def _cand(item, lo, bid, margin, error=0.0):
    return (item, Band(float_min=lo, float_max=lo + 0.02, bid=bid,
                       ceiling=bid * 1.2, step=0.10, margin=margin,
                       market_error=error, take=True))


def test_the_budget_goes_to_the_best_bands_not_the_first_item_listed():
    """The bug three hundred items exposes. At twenty orders and three per
    item, the first seven names took everything and the rest were scored for
    nothing - a band returning 40%/month losing to one returning 4% because it
    was typed in earlier."""
    from src.executor import select_portfolio

    poor = [_cand("A", 0.10 + i / 100, 100.0, 0.04) for i in range(3)]
    rich = [_cand("Z", 0.10 + i / 100, 100.0, 0.40) for i in range(3)]
    limits = Limits(total_capital=300.0, max_orders=3, max_orders_per_item=3)

    got = select_portfolio(poor + rich, limits)
    assert list(got) == ["Z"], "the better item wins whatever order it arrived in"
    assert len(got["Z"]) == 3


def test_the_per_item_cap_still_holds_across_the_whole_portfolio():
    from src.executor import select_portfolio

    one = [_cand("A", 0.10 + i / 100, 100.0, 0.40 - i / 100) for i in range(5)]
    two = [_cand("B", 0.10 + i / 100, 100.0, 0.30 - i / 100) for i in range(5)]
    got = select_portfolio(one + two, Limits(total_capital=10000.0,
                                             max_orders=8,
                                             max_orders_per_item=2))
    assert len(got["A"]) == 2 and len(got["B"]) == 2, \
        "concentration is a cap, not something the ranking may override"


def test_a_standing_order_is_not_dropped_for_a_marginally_better_one():
    """Churn is a real expense: a cancel, a replacement, and the queue
    position that came with it - and the replacement may not fill."""
    from src.executor import select_portfolio

    mine = _cand("A", 0.10, 100.0, 0.20)
    better = _cand("B", 0.10, 100.0, 0.22)
    limits = Limits(total_capital=100.0, max_orders=1, max_orders_per_item=1)

    without = select_portfolio([mine, better], limits)
    assert list(without) == ["B"], "with nothing held, the better one wins"

    with_held = select_portfolio([mine, better], limits,
                                 held=[("A", 0.10, 0.12)])
    assert list(with_held) == ["A"], "holding it is worth more than 2%"


def test_a_band_that_stopped_qualifying_is_not_kept_just_because_it_is_held():
    from src.executor import select_portfolio

    dead = ("A", Band(float_min=0.10, float_max=0.12, bid=100.0, take=False,
                      reason="полоса больше не проходит"))
    got = select_portfolio([dead], Limits(total_capital=1000.0),
                           held=[("A", 0.10, 0.12)])
    assert got == {}, "seeding the held is not a reason to hold a bad band"


def test_ranking_discounts_a_margin_resting_on_a_shaky_price():
    """Testing thousands of bands means the top of the list is selected for
    luck as much as for margin."""
    from src.executor import rank, select_portfolio

    solid = _cand("solid", 0.10, 100.0, 0.20, error=0.02)
    shaky = _cand("shaky", 0.10, 100.0, 0.30, error=0.50)
    assert rank(solid[1]) > rank(shaky[1]), \
        "30% give or take half of it is worth less than a measured 20%"

    got = select_portfolio([shaky, solid],
                           Limits(total_capital=100.0, max_orders=1,
                                  max_orders_per_item=1))
    assert list(got) == ["solid"]


def test_bands_are_ranked_by_margin_not_by_how_fast_they_turn_over():
    """An annualised return divides a margin we trust by a cycle time built
    from a measured flow, an assumed sale rate and a trade lock. Dividing a
    number we trust by one we do not is how a band filling in two days beat
    one paying twice as much."""
    from src.executor import rank

    fat_and_slow = Band(float_min=0.1, float_max=0.12, bid=100.0, take=True,
                        margin=0.20, monthly=0.30, market_error=0.0)
    thin_and_fast = Band(float_min=0.2, float_max=0.22, bid=100.0, take=True,
                         margin=0.05, monthly=0.90, market_error=0.0)
    assert rank(fat_and_slow) > rank(thin_and_fast)
