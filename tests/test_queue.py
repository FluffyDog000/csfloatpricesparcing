"""The placement queue: every band the model would open, in the order the
plan spends money on them - and where the money runs out.

The actions table shows only what fits the budget. With no budget it showed
nothing at all, and "in what order would I place these" had no answer on the
page.
"""
import datetime as dt
import os

from src.executor import Limits, select_portfolio
from src.pricing import Band


def band(lo, bid, margin, lam=0.1):
    return Band(float_min=lo, float_max=lo + 0.02, bid=bid, ceiling=bid * 1.1,
                margin=margin, lam=lam, take=True)


def test_the_trace_is_in_rank_order_and_says_why_the_rest_did_not_fit():
    cands = [("A", band(0.10, 100.0, 0.10)), ("B", band(0.10, 100.0, 0.30)),
             ("B", band(0.12, 100.0, 0.20)), ("C", band(0.10, 100.0, 0.05))]
    trace = []
    got = select_portfolio(cands, Limits(total_capital=350.0, max_orders=10,
                                         max_orders_per_item=1), trace=trace)
    assert [(t["item"], t["band"].margin) for t in trace] == \
        [("B", 0.30), ("B", 0.20), ("A", 0.10), ("C", 0.05)]
    assert [t["taken"] for t in trace] == [True, False, True, False]
    assert "на предмет" in trace[1]["reason"]
    assert "бюджета" in trace[3]["reason"], trace[3]["reason"]
    assert sorted(got) == ["A", "B"]


def test_with_no_budget_the_order_is_still_shown():
    trace = []
    select_portfolio([("A", band(0.10, 100.0, 0.1))], Limits(), trace=trace)
    assert trace[0]["taken"] is False
    assert trace[0]["reason"] == "бюджет не задан"


def _two_items(capital):
    from tests.test_analysis_page import _app

    market = {"A Tight | Book (Field-Tested)": 92.0,
              "B Loose | Book (Field-Tested)": 86.0}
    spread = (-12.0, -8.0, -4.0, 0.0, 4.0, 8.0, 12.0, 16.0)
    c = _app(list(market))
    import webapp
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    now = dt.datetime.now(dt.timezone.utc)
    for name, rival in market.items():
        item_id = db.get_item_id(name)
        rows = []
        for k, f in enumerate((0.155, 0.165, 0.175, 0.185)):
            worth = 104.0 - 3.0 * k
            for i in range(16):
                price = worth + spread[i % len(spread)]
                rows.append((f"{name}-{f}-{i}", item_id, name, int(price * 100),
                             price, f,
                             (now - dt.timedelta(days=(i % 13) + 0.5)).isoformat()))
        db.conn.executemany(
            "INSERT INTO sales (sale_id, item_id, market_hash_name, "
            "price_cents, price, float_value, sold_at, sold_at_estimated, "
            "scraped_at) VALUES (?,?,?,?,?,?,?,0,?)",
            [r + (r[-1],) for r in rows])
        db.replace_buy_orders(item_id, [
            {"price": rival, "qty": 1, "float_min": 0.15, "float_max": 0.19}])
        db.conn.commit()
        c.post("/api/analysis/items", json={"market_hash_name": name})
    db.close()
    c.post("/api/analysis/params",
           json={"an_total_capital": str(capital), "an_max_per_item": "4"})
    return c


def test_the_plan_carries_the_queue_numbered_with_a_running_total():
    q = _two_items(5000).get("/api/analysis/plan").get_json()["queue"]
    assert q and [r["n"] for r in q] == list(range(1, len(q) + 1))
    ranks = [r["rank"] for r in q]
    assert ranks == sorted(ranks, reverse=True), "best first"
    taken = [r for r in q if r["taken"]]
    assert taken
    running = [r["running"] for r in taken]
    assert running == sorted(running), "the total only grows"
    assert running[-1] == round(sum(r["tied_up"] for r in taken), 2)


def test_without_a_budget_the_queue_still_says_what_would_go_first():
    q = _two_items(0).get("/api/analysis/plan").get_json()["queue"]
    assert q, "the order is worth seeing before money is set"
    assert not any(r["taken"] for r in q)
    assert all(r["reason"] == "бюджет не задан" for r in q)
