"""The balance the limits count from, and how far orders may stand over it.

Placed at ten times a typed $700, the orders stood over CSFloat's allowance as
soon as two items filled - the real balance was $630 and the allowance $6300 -
and every amend was refused for balance.
"""
import json
import logging
import os
import tempfile

import pytest

from src import profit as pf
from src.executor import Limits

logging.disable(logging.WARNING)


def test_the_balance_is_read_in_cents():
    assert pf.my_balance({"user": {"balance": 63012, "steam_id": "1"}}) == 630.12
    assert pf.my_balance({"balance": "70000"}) == 700.0
    assert pf.my_balance({"user": {"balance": 630.12}}) == 630.12
    assert pf.my_balance({"user": {}}) is None
    assert pf.my_balance({"user": {"balance": True}}) is None


def test_orders_stand_at_the_chosen_multiple_of_the_balance():
    l = Limits(total_capital=1000, balance=700, full_allowance=True, leverage=6)
    assert l.order_cap == pytest.approx(4200)
    assert l.allowance == pytest.approx(7000), "CSFloat's own is still ten"
    l = Limits(total_capital=1000, balance=700, leverage=6)
    assert l.order_cap == pytest.approx(4200)
    assert Limits(total_capital=1000, balance=700, full_allowance=True,
                  leverage=25).order_cap == pytest.approx(7000), "never past ten"


def _db():
    from src.db import Database
    return Database(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_a_fresh_reading_replaces_the_typed_balance():
    from src.db import utcnow_iso
    from src.settings import BALANCE_AT_KEY, BALANCE_KEY, limits

    db = _db()
    db.set_setting("an_balance", "700")
    assert limits(db).balance == 700 and not limits(db).balance_live
    db.set_setting(BALANCE_KEY, "630.00")
    db.set_setting(BALANCE_AT_KEY, utcnow_iso())
    got = limits(db)
    assert got.balance == 630 and got.balance_live and got.balance_typed == 700
    db.set_setting(BALANCE_AT_KEY, "2020-01-01T00:00:00+00:00")
    assert limits(db).balance == 700, "a stale reading gives way to the typed one"
    db.close()


def test_the_brake_measures_the_day_against_where_it_began():
    """A balance read off the account is already short of today's buys; the
    brake measured them against what they left would tighten with every fill."""
    from src.guard import read_trades

    trades = [{"role": "buy", "state": "verified", "price": 258.35,
               "created_at": pf.since_iso(0.1)}]
    live = Limits(balance=441.65, guard_share=0.9, balance_live=True)
    typed = Limits(balance=700, guard_share=0.9)
    assert read_trades(trades, live).reference == pytest.approx(700)
    assert read_trades(trades, typed).reference == pytest.approx(700)


def test_the_sync_reads_the_balance_off_the_account():
    from src.collector import Collector
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.placement import PLACEMENT_KEY, SUGGESTED
    from src.settings import BALANCE_KEY, live_balance

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = os.environ["CSFLOAT_DB_PATH"]
    from src.db import Database
    db = Database(cfg.db_path)
    db.set_setting(PLACEMENT_KEY, json.dumps(SUGGESTED.as_dict()))
    col = Collector(cfg, db, CSFloatClient(cfg.http, cfg.polling))

    def answer(url, headers=None, account=False):
        if url.endswith("/api/v1/me"):
            return {"user": {"balance": 63000, "steam_id": "7656"}}
        return {"data": []}

    col.client.fetch_json = answer
    col.sync_our_orders()
    assert db.get_setting(BALANCE_KEY) == "630.00"
    assert live_balance(db)[0] == 630.0
    db.close()
