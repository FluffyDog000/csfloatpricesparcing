"""HTTP client for CSFloat's undocumented "Latest Sales" endpoint.

Responsibilities:
  * build the request the browser makes (headers from .env, never hard-coded),
  * enforce a global minimum spacing between requests (~1 req/sec),
  * handle HTTP 429 with a GLOBAL cooldown (all items pause, escalating),
  * surface 401/403 as a distinct AuthError so the collector can skip the cycle
    and tell the user to refresh the cookie/token instead of crashing.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote

import requests

from .config import HttpConfig, PollingConfig
from .db import utcnow_iso
from .proxies import ProxyPool

log = logging.getLogger("csfloat.client")

# How long rotating routes stay parked after an account-level IP complaint.
ACCOUNT_BLOCK_SECONDS = 6 * 3600.0

# Global cooldown applied to ALL requests after a 429, doubling each time.
COOLDOWN_BASE_SECONDS = 60.0
COOLDOWN_MAX_SECONDS = 900.0


class AuthError(Exception):
    """Raised on 401/403 — the session cookie/token needs manual refresh."""


class RateLimited(Exception):
    """Raised when 429 persists past the configured retry budget."""


class CSFloatClient:
    def __init__(self, http: HttpConfig, polling: PollingConfig):
        self.http = http
        self.polling = polling
        self._last_request_ts = 0.0
        self._lock = threading.Lock()
        # Global 429 cooldown shared by every item: when CSFloat rate-limits us
        # we stop ALL polling for a while instead of hammering item by item.
        self._cooldown_until = 0.0
        self._consecutive_429 = 0
        # Whatever rate-limit headers CSFloat returned with the last 429.
        self.last_429_headers: dict[str, str] = {}
        self.last_429_body: str = ""
        # Latest quota snapshot from x-ratelimit-* headers.
        self.rate_state: dict[str, object] = {}
        # One budget per outgoing IP: the direct connection plus any proxies.
        self.pool = ProxyPool(list(http.proxies), use_direct=http.use_direct)
        self.last_route: str | None = None
        # Set when CSFloat complains about one account using too many IPs.
        self.account_ip_block_at: str | None = None
        self.session = requests.Session()
        self.session.headers.update(self._base_headers())

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.http.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.http.base_url + "/",
            "Origin": self.http.base_url,
        }
        if self.http.cookie:
            headers["Cookie"] = self.http.cookie
        if self.http.authorization:
            headers["Authorization"] = self.http.authorization
        return headers

    def has_credentials(self) -> bool:
        return bool(self.http.cookie or self.http.authorization)

    def restore_cooldown(self, seconds: float, consecutive: int = 0) -> None:
        """Re-arm a cooldown that was still running before a restart, so the
        collector doesn't immediately burst back into the rate limit."""
        if seconds > 0:
            self._cooldown_until = time.monotonic() + seconds
            self._consecutive_429 = max(consecutive, 1)

    def cooldown_remaining(self) -> float:
        """Seconds before polling may resume. With proxies configured this is
        per-route: as long as one route still has quota, we keep going."""
        if len(self.pool.routes) > 1:
            return self.pool.wait_seconds()
        return max(0.0, self._cooldown_until - time.monotonic())

    def _feed_pool(self, route, resp) -> None:
        def num(name: str):
            raw = resp.headers.get(name)
            try:
                return int(float(raw)) if raw is not None else None
            except (TypeError, ValueError):
                return None
        self.pool.record_headers(route, num("x-ratelimit-limit"),
                                 num("x-ratelimit-remaining"),
                                 num("x-ratelimit-reset"))

    def _account_ip_complaint(self, resp) -> bool:
        """True when the body is CSFloat's account-level 'too many requests from
        too many IPs'. That one is NOT about a single route's quota: rotating
        exit IPs are what triggers it, so those routes must stop, not slow."""
        try:
            body = (resp.text or "")[:300].lower()
        except Exception:  # noqa: BLE001
            return False
        return "too many ips" in body or "from too many" in body

    def _handle_account_ip_complaint(self, resp) -> bool:
        if not self._account_ip_complaint(resp):
            return False
        self.account_ip_block_at = utcnow_iso()
        try:
            self.last_429_body = (resp.text or "")[:300]
        except Exception:  # noqa: BLE001
            pass
        if self.pool.park_rotating(ACCOUNT_BLOCK_SECONDS):
            log.error(
                "CSFloat flagged this account for using too many IPs. Rotating "
                "routes are parked for %.0f h — switch the provider to sticky "
                "sessions (a few fixed exit IPs) before re-enabling them.",
                ACCOUNT_BLOCK_SECONDS / 3600.0,
            )
        return True

    def _enter_cooldown(self, retry_after: float | None = None) -> float:
        """Escalating global pause after a 429: 1, 2, 4 ... minutes (capped)."""
        self._consecutive_429 += 1
        rl = self.polling.rate_limit
        wait = min(
            COOLDOWN_BASE_SECONDS * (2 ** (self._consecutive_429 - 1)),
            max(rl.max_backoff_seconds, COOLDOWN_MAX_SECONDS),
        )
        if retry_after:
            wait = max(wait, retry_after)
        self._cooldown_until = time.monotonic() + wait
        return wait

    def _capture_rate_headers(self, resp) -> None:
        """CSFloat sends x-ratelimit-* on every response. Tracking them lets the
        collector plan against the real remaining quota instead of guessing."""
        def num(name: str):
            raw = resp.headers.get(name)
            if raw is None:
                return None
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return None

        limit = num("x-ratelimit-limit")
        remaining = num("x-ratelimit-remaining")
        reset = num("x-ratelimit-reset")
        if limit is None and remaining is None:
            return
        self.rate_state = {
            "limit": limit,
            "remaining": remaining,
            "reset": reset,
            "seen_at": time.time(),
        }

    def _respect_spacing(self) -> None:
        """Ensure at least `min_seconds_between_requests` between calls."""
        with self._lock:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.polling.min_seconds_between_requests - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def sales_url(self, market_hash_name: str) -> str:
        # market_hash_name contains spaces, "|", "★" etc. — encode safely.
        encoded = quote(market_hash_name, safe="")
        path = self.http.sales_path_template.format(name=encoded)
        return self.http.base_url + path

    def fetch_latest_sales(self, market_hash_name: str) -> object:
        """Fetch and return the parsed JSON body for an item's latest sales.

        Picks the outgoing route (direct or a proxy) with the most quota left.
        Raises AuthError on 401/403, RateLimited when the route is limited."""
        url = self.sales_url(market_hash_name)
        rl = self.polling.rate_limit
        backoff = rl.base_backoff_seconds
        attempt = 0

        while True:
            route = self.pool.pick()
            if route is None:
                wait = self.pool.wait_seconds()
                self._cooldown_until = time.monotonic() + min(wait, 300.0)
                raise RateLimited(
                    f"all routes spent or cooling; next free in {wait / 60:.1f} min"
                )
            self.last_route = route.key
            self._respect_spacing()
            try:
                resp = self.session.get(url, timeout=self.http.timeout_seconds,
                                        proxies=route.proxies())
            except requests.RequestException as exc:
                self.pool.record_failure(route, exc)
                attempt += 1
                if attempt > rl.max_retries:
                    raise
                log.warning(
                    "Network error for %s (attempt %d/%d): %s; retrying in %.1fs",
                    market_hash_name, attempt, rl.max_retries, exc, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, rl.max_backoff_seconds)
                continue

            if resp.status_code in (401, 403):
                if self._handle_account_ip_complaint(resp):
                    wait = self._enter_cooldown(None)
                    raise RateLimited(
                        f"account flagged for too many IPs on {market_hash_name}; "
                        f"pausing for {wait / 60:.1f} min"
                    )
                raise AuthError(
                    f"HTTP {resp.status_code} for {market_hash_name} — "
                    f"session cookie/token likely expired, refresh it."
                )

            if resp.status_code == 429:
                # Don't retry this item in place — that only deepens the limit.
                # Pause every item globally, then let the scheduler resume.
                retry_after = None
                hdr = resp.headers.get("Retry-After")
                if hdr:
                    try:
                        retry_after = float(hdr)
                    except ValueError:
                        retry_after = None
                self._capture_rate_headers(resp)
                self._feed_pool(route, resp)
                self._handle_account_ip_complaint(resp)
                wait = self._enter_cooldown(retry_after)
                self.pool.record_429(route, wait)
                self.last_429_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower().startswith(("retry-after", "x-ratelimit", "ratelimit",
                                             "x-rate-limit", "cf-ray"))
                }
                # The body tells us WHO is blocking: CSFloat's own account-level
                # message vs a Cloudflare bot/IP challenge. Vital for diagnosis.
                try:
                    self.last_429_body = (resp.text or "")[:300]
                except Exception:  # noqa: BLE001
                    self.last_429_body = ""
                log.warning("429 details: headers=%s body=%s",
                            self.last_429_headers or "{}", self.last_429_body[:200])
                raise RateLimited(
                    f"429 on {market_hash_name}; pausing all polling for "
                    f"{wait / 60:.1f} min (consecutive 429: {self._consecutive_429})"
                )

            self._capture_rate_headers(resp)
            self._feed_pool(route, resp)
            resp.raise_for_status()
            self.pool.record_success(route)
            self._consecutive_429 = 0  # healthy response clears the escalation
            return resp.json()
