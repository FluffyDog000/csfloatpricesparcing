"""HTTP client for CSFloat's undocumented "Latest Sales" endpoint.

Responsibilities:
  * build the request the browser makes (headers from .env, never hard-coded),
  * enforce a global minimum spacing between requests (~1 req/sec),
  * handle HTTP 429 with exponential backoff,
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

log = logging.getLogger("csfloat.client")


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

        Raises AuthError on 401/403, RateLimited if 429 exhausts retries."""
        url = self.sales_url(market_hash_name)
        rl = self.polling.rate_limit
        backoff = rl.base_backoff_seconds
        attempt = 0

        while True:
            self._respect_spacing()
            try:
                resp = self.session.get(url, timeout=self.http.timeout_seconds)
            except requests.RequestException as exc:
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
                raise AuthError(
                    f"HTTP {resp.status_code} for {market_hash_name} — "
                    f"session cookie/token likely expired, refresh it."
                )

            if resp.status_code == 429:
                attempt += 1
                if attempt > rl.max_retries:
                    raise RateLimited(
                        f"429 rate limited on {market_hash_name}, retries exhausted"
                    )
                # Honour Retry-After header if present.
                retry_after = resp.headers.get("Retry-After")
                wait = backoff
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                log.warning(
                    "HTTP 429 for %s (attempt %d/%d); backing off %.1fs",
                    market_hash_name, attempt, rl.max_retries, wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, rl.max_backoff_seconds)
                continue

            resp.raise_for_status()
            return resp.json()
