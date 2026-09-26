"""Sales history is public, so it must go out carrying nothing.

Tested against the live endpoint: history/{name}/sales answers 200 with no
credentials, and its 500 counter belongs to the exit address rather than to
any account. Sales polling is the bulk of our traffic, so sending the browser
session with it replayed one account across every proxy in the pool — the
pattern that drew "too many requests from too many IPs".
"""
import pytest

from src.config import HttpConfig, PollingConfig, RateLimitConfig
from src.csfloat_client import AuthError, CSFloatClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.headers = {}
        self._payload = payload if payload is not None else {"data": []}
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def client(status_code=200):
    http = HttpConfig(
        base_url="https://csfloat.com",
        sales_path_template="/api/v1/history/{name}/sales",
        timeout_seconds=5,
        user_agent="test",
        cookie="session=secret-cookie-value",
        authorization="Bearer secret-token",
        api_key="il1-secretkey",
        proxies=[],
        use_direct=True,
    )
    polling = PollingConfig(
        interval_min_minutes=1, interval_max_minutes=2,
        min_seconds_between_requests=0.0, gap_warning_min_overlap=1,
        rate_limit=RateLimitConfig(base_backoff_seconds=0.0,
                                   max_backoff_seconds=0.0, max_retries=0),
    )
    c = CSFloatClient(http, polling)
    sent = {}

    def fake_get(url, **kwargs):
        sent["headers"] = kwargs.get("headers")
        return FakeResponse(status_code)

    c.session.get = fake_get
    return c, sent


def test_the_sales_request_carries_no_credentials():
    c, sent = client()
    c.fetch_latest_sales("AK-47 | Redline (Field-Tested)")
    headers = sent["headers"]
    assert headers["Cookie"] is None, "the session cookie must be removed"
    assert headers["Authorization"] is None, "the key must be removed too"


def test_no_secret_leaks_into_the_sales_request():
    """Whatever else the headers carry, none of it may be a credential."""
    c, sent = client()
    c.fetch_latest_sales("AK-47 | Redline (Field-Tested)")
    headers = sent["headers"]
    assert headers is not None, (
        "sending no per-request headers lets the session's own credentials "
        "merge in, so this check would pass while the cookie still went out")
    rendered = repr(headers)
    for secret in ("secret-cookie-value", "secret-token", "il1-secretkey"):
        assert secret not in rendered


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_blames_the_address_not_the_cookie(status):
    c, _ = client(status)
    with pytest.raises(AuthError) as caught:
        c.fetch_latest_sales("AK-47 | Redline (Field-Tested)")
    message = str(caught.value)
    assert "адрес" in message, "an anonymous request has no credential to renew"
    assert "CSFLOAT_COOKIE" not in message
