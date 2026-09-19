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
                        Limits(patience_days=14.0))
    assert patient[0].kind == KEEP
    assert "очередь разойдётся" in patient[0].reason

    # The same outbid on a band that trades once a month is a real delay.
    slow = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0, lam=0.03)],
                     [row(0.15, 0.17, 100.0, 110.0)], book,
                     Limits(patience_days=14.0))
    assert slow[0].kind == RAISE
    assert slow[0].price == 102.0, "one tier step over the rival"
    assert slow[0].was == 100.0


def test_a_position_bid_past_its_ceiling_is_abandoned_not_chased():
    # Answering this rival would cost $111, and $110 is the most the trade
    # is worth; the ceiling itself is still a price we would pay.
    book = [{"price": 110.0, "qty": 1, "float_min": 0.15, "float_max": 0.17}]
    acts = reconcile("Gloves", [band(0.15, 0.17, 100.0, 110.0, lam=0.03)],
                     [row(0.15, 0.17, 100.0, 110.0)], book,
                     Limits(patience_days=1.0))
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
