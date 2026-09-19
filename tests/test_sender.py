"""Carrying out one action, and refusing to carry it out twice.

This is the layer that spends money, so what it will not do matters more than
what it will: it does not retry, it does not act unarmed, and one refusal does
not abandon the rest of a plan half-applied.
"""
import pytest

from src.executor import CANCEL, KEEP, PLACE, RAISE, Action
from src.placement import SUGGESTED, Spec
from src.sender import Sender


def act(kind, price=159.0, remote_id=None, was=None):
    return Action(kind, "★ Gloves | Fade (Field-Tested)", 0.32, 0.38, price,
                  170.0, "потому что", remote_id=remote_id, was=was)


class Recorder:
    def __init__(self, reply=None, fail=None):
        self.calls = []
        self.reply = reply or {"id": "abc", "price": 15900, "qty": 1,
                               "hybrid_properties": {}, "bought_item_count": 0}
        self.fail = fail

    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body))
        if self.fail:
            raise self.fail
        return self.reply


def test_a_dry_run_sends_nothing_but_shows_the_request():
    rec = Recorder()
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=True)
    got = s.perform(act(PLACE))

    assert got.ok and rec.calls == [], "nothing left the machine"
    assert "вхолостую" in got.detail
    assert "/api/v1/buy-orders" in got.detail and "15900" in got.detail


def test_placing_for_real_sends_cents_and_the_float_band():
    rec = Recorder()
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(PLACE))

    method, url, body = rec.calls[0]
    assert (method, url) == ("POST", "https://csfloat.com/api/v1/buy-orders")
    assert body["max_price"] == 15900, \
        "the request names it max_price - the reply calls it price"
    assert body["hybrid_properties"] == {"min_float": 0.32, "max_float": 0.38}
    assert got.ok and got.remote_id == "abc"


def test_answering_an_outbid_amends_rather_than_replaces():
    rec = Recorder()
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(RAISE, price=161.0, remote_id="xyz", was=159.0))

    method, url, body = rec.calls[0]
    assert method == "PATCH"
    assert url.endswith("/api/v1/buy-orders/xyz"), "the order keeps its place"
    assert body == {"max_price": 16100}
    assert got.ok


def test_cancelling_uses_the_order_id():
    rec = Recorder()
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(CANCEL, remote_id="xyz"))

    method, url, body = rec.calls[0]
    assert (method, body) == ("DELETE", None)
    assert url.endswith("/api/v1/buy-orders/xyz")
    assert got.ok


def test_cancelling_something_never_placed_sends_nothing():
    """Our row exists, the site's does not: dropping ours is right, and a
    request with no id to put in it would be a guess."""
    rec = Recorder()
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(CANCEL, remote_id=None))

    assert got.ok and rec.calls == []
    assert "не стоял на сайте" in got.detail


def test_a_write_is_never_retried():
    """A create that timed out may already have placed the order. Asking again
    is how an account ends up holding two of them."""
    rec = Recorder(fail=TimeoutError("read timed out"))
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(PLACE))

    assert not got.ok and "TimeoutError" in got.detail
    assert len(rec.calls) == 1, "one attempt, whatever happened to it"


def test_one_refusal_does_not_abandon_the_rest():
    rec = Recorder(fail=RuntimeError("no"))
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    results = [s.perform(a) for a in (act(PLACE), act(KEEP), act(CANCEL))]

    assert [r.ok for r in results] == [False, True, True]


def test_an_unconfigured_operation_is_reported_not_attempted():
    rec = Recorder()
    half = Spec(create_method="POST", create_path="/api/v1/buy-orders",
                create_body=SUGGESTED.create_body)
    s = Sender("https://csfloat.com", half, rec, dry_run=False)

    assert s.perform(act(PLACE)).ok
    took_down = s.perform(act(CANCEL, remote_id="xyz"))
    assert not took_down.ok and "отмены не настроен" in took_down.detail


def test_a_reply_without_an_id_is_not_treated_as_a_placement():
    """Storing a placement we cannot later amend or cancel is worse than
    reporting the failure."""
    rec = Recorder(reply={"error": "nope"})
    s = Sender("https://csfloat.com", SUGGESTED, rec, dry_run=False)
    got = s.perform(act(PLACE))

    assert not got.ok and got.remote_id is None


def test_a_refusal_carries_what_the_server_said():
    """Six rejected orders read identically - "400 Bad Request" - whether the
    price was wrong, the filter was, or the field was not one CSFloat knows.
    The body is where it says which, and throwing it away costs a round trip
    through a person to find out."""
    import requests

    class Resp:
        status_code = 400
        text = '{"message":"invalid float range"}'
        headers = {}

        def json(self):
            import json
            return json.loads(self.text)

    def refuse(method, url, body=None, headers=None):
        raise requests.HTTPError(
            f"HTTP 400 для {url} — {Resp.text}", response=Resp())

    s = Sender("https://csfloat.com", SUGGESTED, refuse, dry_run=False)
    got = s.perform(act(PLACE))

    assert not got.ok
    assert "invalid float range" in got.detail, \
        "what the server objected to survives into the report"


def test_a_refusal_reports_the_request_that_drew_it():
    """Six creates came back "orders must have a max price above 0" while the
    code already sent max_price - because the saved request still said price.
    From the outside those two look identical, so the failure carries the body
    that actually went out."""
    from src.executor import PLACE, Action
    from src.placement import Spec
    from src.sender import Sender

    def refuse(method, url, body, headers):
        raise RuntimeError('HTTP 400 — {"code":5,"message":'
                           '"orders must have a max price above 0"}')

    stale = Spec(create_path="/api/v1/buy-orders",
                 create_body='{"market_hash_name": "{name}", '
                             '"price": {price_cents}}')
    sender = Sender("https://csfloat.com", stale, refuse, dry_run=False)
    action = Action(PLACE, "★ Hand Wraps | Duct Tape (Field-Tested)",
                    0.1501, 0.158, 49.0, 52.0, "проба")

    result = sender.perform(action)
    assert not result.ok
    assert "max price above 0" in result.detail
    assert '"price": 4900' in result.detail, "what went out, not what we meant"
    assert result.action.sent == {
        "market_hash_name": "★ Hand Wraps | Duct Tape (Field-Tested)",
        "price": 4900}
    assert result.as_dict()["action"]["sent"]["price"] == 4900


def test_a_send_that_never_happened_reports_no_body():
    """A dry run, or a refusal before the request was built, must not claim a
    request went out."""
    from src.executor import PLACE, Action
    from src.placement import SUGGESTED
    from src.sender import Sender

    def unreachable(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("dry run sent a request")

    sender = Sender("https://csfloat.com", SUGGESTED, unreachable, dry_run=True)
    action = Action(PLACE, "x", 0.1, 0.2, 49.0, 52.0, "проба")
    result = sender.perform(action)
    assert result.ok and action.sent is None
    assert "отправлено" not in result.detail
