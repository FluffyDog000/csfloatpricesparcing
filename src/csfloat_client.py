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

# How long a caller waits for some key to come round before giving up. Long
# enough to ride out one key's spacing, short enough that a sweep reports a
# jam rather than hanging in it.
LEASE_WAIT_SECONDS = 120.0


class AuthError(Exception):
    """Raised on 401/403 — the session cookie/token needs manual refresh.

    Carries the response when there is one, so a diagnostic caller can show
    who actually refused: the status alone does not distinguish CSFloat
    rejecting a credential from an edge rejecting the IP."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


class RateLimited(Exception):
    """Raised when 429 persists past the configured retry budget."""


class NoRouteAvailable(RateLimited):
    """No route can be used right now — every one is parked, cooling down or
    out of quota.

    A subclass of RateLimited so existing handlers still back off, but named
    apart because the cause is ours, not CSFloat's: reporting it as "лимит
    CSFloat" sends the user to wait on a limit that was never hit."""


class VpnBlocked(Exception):
    """CSFloat refuses to serve this endpoint from a VPN or datacenter IP.

    Neither a credential problem nor a rate limit: buy orders are only visible
    from a residential-looking address, so no cookie and no waiting will help —
    only a different kind of IP."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


class EdgeBlocked(Exception):
    """Raised when Cloudflare screened the exit IP on every route we tried.

    Distinct from AuthError on purpose: the cookie is fine, the IP is not, so
    the fix is a different proxy rather than a fresh session."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


def wire_bytes(resp) -> int:
    """Bytes actually pulled over the wire, not the size of the decoded body.

    A metered proxy bills for what crossed the link. CSFloat gzips - and, once
    brotli is installed, brotli-compresses - its JSON, so `len(resp.content)`
    overstates that by an order of magnitude, and a traffic forecast built on
    it buys far more gigabytes than the job needs.

    Content-Length comes first because it is the server's own count of the
    compressed body and is simply present or not. urllib3's stream counter
    agrees with it where both exist, but on the live endpoint it came back
    zero, which silently turned this into `len(resp.content)` and made the
    traffic report identical to the figure it was meant to replace. The
    decoded length stays as the last resort - wrong, but never absent.
    """
    declared = (resp.headers or {}).get("Content-Length")
    if declared:
        try:
            return int(declared)
        except (TypeError, ValueError):
            pass
    raw = getattr(resp, "raw", None)
    try:
        counted = raw.tell() if raw is not None else 0
    except Exception:  # noqa: BLE001 - a stand-in response need not implement it
        counted = 0
    if counted:
        return int(counted)
    return len(resp.content)


class CSFloatClient:
    def __init__(self, http: HttpConfig, polling: PollingConfig,
                 keyring=None):
        self.http = http
        self.polling = polling
        # With a key ring, every key has its own clock and its own cooldown,
        # and a request leases one of its own addresses. Without one - the
        # single-key setup this has always been - the pool and the shared
        # clock below behave exactly as before.
        self.keyring = keyring
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
        # Wire size of the last successful response, so the traffic report
        # matches what a metered proxy bills for.
        self.last_response_bytes: int | None = None
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

    def _edge_block(self, resp) -> bool:
        """True when this response came from Cloudflare's edge rather than from
        CSFloat's API — i.e. the exit IP is challenged or banned, not our cookie.

        Matters for rotating proxies: a sticky session hands out a fresh exit IP
        every few minutes, and a bad one answers 403 (or a challenge page) that
        looks exactly like an expired session unless you check who replied. The
        API always answers JSON; the edge answers HTML."""
        if "cf-mitigated" in resp.headers:
            return True
        ctype = resp.headers.get("Content-Type", "").lower()
        if "json" in ctype:
            return False
        try:
            body = (resp.text or "").lstrip()[:200].lower()
        except Exception:  # noqa: BLE001
            return False
        if body.startswith(("[", "{")):
            return False
        return bool(body) and (
            body.startswith(("<!doctype", "<html"))
            or "just a moment" in body
            or "attention required" in body
            or "cloudflare" in body
        )

    def _vpn_refusal(self, resp) -> bool:
        """True for CSFloat's "Disable your VPN to view buy orders" (code 170).

        It arrives as a JSON 403, so it reads as a credential refusal unless
        the body is inspected — but the cookie is fine and the address is the
        problem."""
        try:
            body = (resp.text or "")[:300].lower()
        except Exception:  # noqa: BLE001
            return False
        return "disable your vpn" in body or '"code": 170' in body

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

    def _retry_other_route(self, route, attempt: int, rl, backoff: float,
                           name: str, reason: str):
        """Park the offending route and let the caller try another one.

        Returns an exception to raise when the retries are spent, or None to
        keep going. The route is faulted either way, so a proxy whose exit IP
        stays bad drops out of rotation instead of eating every poll."""
        self.pool.record_failure(route, reason)
        if attempt + 1 > rl.max_retries:
            return EdgeBlocked(f"{reason} for '{name}' on route {route.key}; "
                               f"gave up after {rl.max_retries} attempts")
        log.warning("%s for '%s' via %s (attempt %d/%d) — trying another route "
                    "in %.1fs", reason, name, route.key, attempt + 1,
                    rl.max_retries, backoff)
        time.sleep(backoff)
        return None

    def reset_in(self) -> float:
        """Seconds until the quota refills, by CSFloat's own header.

        `x-ratelimit-reset` comes as an epoch second on some routes and as a
        count of seconds on others, so it is read as whichever it can only be:
        a value past the epoch threshold is a moment in time, anything smaller
        is a duration. Stale by however long ago the header was seen, which is
        subtracted rather than ignored.
        """
        state = self.rate_state or {}
        reset = state.get("reset")
        if reset is None:
            return 0.0
        try:
            reset = float(reset)
        except (TypeError, ValueError):
            return 0.0
        now = time.time()
        if reset > 1_000_000_000:          # an epoch second, not a duration
            return max(0.0, reset - now)
        seen = float(state.get("seen_at") or now)
        return max(0.0, reset - (now - seen))

    def _enter_cooldown(self, retry_after: float | None = None,
                        key=None) -> float:
        """Escalating global pause after a 429: 1, 2, 4 ... minutes (capped).

        Never shorter than what CSFloat says is left on the clock. Waiting a
        minute against a quota that refills in ten spends the other nine
        collecting refusals, each one escalating the backoff that would have
        been right the first time - which is how a sweep that only needed to
        sit still once ends up giving up.
        """
        self._consecutive_429 += 1
        rl = self.polling.rate_limit
        wait = min(
            COOLDOWN_BASE_SECONDS * (2 ** (self._consecutive_429 - 1)),
            max(rl.max_backoff_seconds, COOLDOWN_MAX_SECONDS),
        )
        if retry_after:
            wait = max(wait, retry_after)
        # The header is a fact about the account; the backoff above is a guess.
        wait = max(wait, min(self.reset_in(), ACCOUNT_BLOCK_SECONDS))
        # A refusal belongs to whoever drew it. Held globally, one 429 stopped
        # every key at once, which is the whole of the throughput a hundred
        # keys were meant to buy - so with a ring the key waits and the others
        # carry on. The escalating counter stays shared on purpose: repeated
        # refusals across different keys are an account-level signal, and
        # resetting it per key would hide exactly that.
        if key is not None and self.keyring is not None:
            self.keyring.note_rate_limit(key.key, wait)
            return wait
        self._cooldown_until = time.monotonic() + wait
        return wait

    def _remember_429(self, resp) -> None:
        """What the refusal itself said, kept for the next person to ask why.

        The body is the part that names who is refusing: CSFloat's own
        account-level message reads nothing like a Cloudflare challenge, and
        the difference decides whether waiting helps at all.
        """
        try:
            self.last_429_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower().startswith(("retry-after", "x-ratelimit", "ratelimit",
                                         "x-rate-limit", "cf-ray"))
            }
        except Exception:  # noqa: BLE001 - diagnosis must not raise
            self.last_429_headers = {}
        try:
            self.last_429_body = (resp.text or "")[:300]
        except Exception:  # noqa: BLE001
            self.last_429_body = ""

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

    def _lease(self):
        """Who speaks, from where, and after what wait.

        Returns (route, key, applied_headers). Without a ring this is the old
        behaviour spelled out: the pool picks, the shared clock paces, and the
        key is whatever `.env` configured.
        """
        if self.keyring is None:
            route = self.pool.pick()
            if route is None:
                wait = self.pool.wait_seconds()
                raise NoRouteAvailable(
                    f"нет доступных маршрутов, ближайший освободится через "
                    f"{wait / 60:.0f} мин" if wait > 0 else
                    "нет доступных маршрутов")
            self._respect_spacing()
            return route, None

        # A ring paces itself: `lease` hands back only a key whose own clock
        # has come round, so there is no global gap to respect.
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while True:
            leased = self.keyring.lease()
            if leased is not None:
                state, route = leased
                return route, state
            wait = self.keyring.wait_seconds()
            if wait <= 0 or time.monotonic() + wait > deadline:
                raise NoRouteAvailable(
                    f"все ключи заняты или на паузе, ближайший освободится "
                    f"через {wait:.0f} с" if wait > 0 else
                    "ни одного рабочего ключа с адресом")
            time.sleep(min(wait, 1.0))

    def _respect_spacing(self) -> None:
        """Ensure at least `min_seconds_between_requests` between calls."""
        with self._lock:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.polling.min_seconds_between_requests - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def fetch_json(self, url: str, headers: dict[str, str] | None = None) -> object:
        """One-off GET for a small side endpoint (currently the FX rate).

        Goes through the pool and the request spacing like any other call, and
        a 429 arms the same cooldown a sales poll would — the limit belongs to
        the account, not to the endpoint. It does not retry, though: nothing
        here is worth delaying the sales polling for."""
        route, key = self._lease()
        headers = self._with_key(headers, key)
        try:
            resp = self.session.get(url, timeout=self.http.timeout_seconds,
                                    proxies=route.proxies(), headers=headers)
        except requests.RequestException as exc:
            # Fault the route like a sales poll does, so a proxy that keeps
            # dropping connections leaves rotation instead of failing forever.
            self.pool.record_failure(route, exc)
            if key is not None and self.keyring is not None:
                self.keyring.note_failure(key.key)
            raise
        return self._read(resp, route, url, key)

    def send_json(self, method: str, url: str, body: object | None = None,
                  headers: dict[str, str] | None = None) -> object:
        """A write: create, amend or take down one of our own buy orders.

        Held to the same pool, spacing and limits as a read, because CSFloat
        counts them against the same account and the same address. Writes are
        never retried: a request that may already have placed an order is not
        one to send twice on a guess."""
        route, key = self._lease()
        headers = self._with_key(headers, key)
        try:
            resp = self.session.request(
                method.upper(), url, json=body, headers=headers,
                timeout=self.http.timeout_seconds, proxies=route.proxies())
        except requests.RequestException as exc:
            self.pool.record_failure(route, exc)
            if key is not None and self.keyring is not None:
                self.keyring.note_failure(key.key)
            raise
        return self._read(resp, route, url, key)

    def _with_key(self, headers, key):
        """Send the leased key rather than the one `.env` happens to hold.

        A ring exists so a hundred keys can speak at once; sending one key's
        Authorization on another key's address would make them one client
        again, with the addresses spread over it.
        """
        if key is None:
            return headers
        out = dict(headers or {})
        out["Authorization"] = key.key
        return out

    def _read(self, resp, route, url: str, key=None) -> object:
        """Shared handling: the limits and refusals are the same either way."""
        self._capture_rate_headers(resp)
        self._feed_pool(route, resp)

        if resp.status_code == 429:
            # A 429 counts the same whichever endpoint drew it: the limit is on
            # the account and the IP, not on the path. Skipping the backoff here
            # let a sweep keep firing into a limit it had already hit.
            self._handle_account_ip_complaint(resp)
            retry_after = None
            hdr = resp.headers.get("Retry-After")
            if hdr:
                try:
                    retry_after = float(hdr)
                except ValueError:
                    retry_after = None
            # Keep what the refusal said, as the sales poll already does. Only
            # that path recorded it, so every 429 drawn by a sweep or a write
            # left nothing behind - and a refusal with no headers and no body
            # cannot be told apart from a quota that simply ran out.
            self._remember_429(resp)
            wait = self._enter_cooldown(retry_after, key)
            self.pool.record_429(route, wait)
            raise RateLimited(
                f"429 on a side request; polling paused for {wait / 60:.1f} min")

        # Check who answered BEFORE raising: a 403 from Cloudflare means the
        # exit IP was screened and another route may well work, while
        # raise_for_status would just surface it as an opaque HTTP error.
        if self._edge_block(resp):
            self.pool.record_failure(route, "edge block on a side request")
            raise EdgeBlocked(
                f"HTTP {resp.status_code}: Cloudflare screened the exit IP", resp)

        if resp.status_code in (401, 403) and self._vpn_refusal(resp):
            raise VpnBlocked(
                "CSFloat не отдаёт эти данные с IP датацентра или VPN "
                "(«Disable your VPN»)", resp)

        if resp.status_code in (401, 403):
            # CSFloat itself refusing the credentials, not the edge refusing
            # the IP: retrying elsewhere cannot help, and the raw HTTPError
            # says nothing about which credential is at fault.
            raise AuthError(
                f"HTTP {resp.status_code} — CSFloat не принял учётные данные "
                f"для {url.split('?')[0]}", resp)

        if 400 <= resp.status_code < 600:
            # The body is where CSFloat says what it disliked, and a bare
            # "400 Client Error" throws that away: six rejected orders read
            # identically whether the price was wrong, the filter was, or the
            # field was not one it knows.
            detail = (resp.text or "").strip().replace("\n", " ")[:300]
            raise requests.HTTPError(
                f"HTTP {resp.status_code} для {url.split('?')[0]}"
                + (f" — {detail}" if detail else ""), response=resp)

        self.pool.record_success(route)
        return resp.json()

    def sales_url(self, market_hash_name: str) -> str:
        # market_hash_name contains spaces, "|", "★" etc. — encode safely.
        encoded = quote(market_hash_name, safe="")
        path = self.http.sales_path_template.format(name=encoded)
        return self.http.base_url + path

    def _sales_headers(self) -> dict[str, str | None]:
        """Sales history is public, so it is read with no credentials at all.

        Tested against the live endpoint: history/{name}/sales answers 200
        anonymously, and its 500 counter belongs to the exit address — a fresh
        address already read 463 of 500, spent by whoever else shares it.

        That makes carrying a credential here pure cost. Sales polling is the
        bulk of our traffic, and sending the browser session with it replayed
        one account across every proxy in the pool, which is what drew "too
        many requests from too many IPs". None removes the session's header
        from the request rather than letting it merge in.
        """
        return {"Cookie": None, "Authorization": None}

    def fetch_latest_sales(self, market_hash_name: str) -> object:
        """Fetch and return the parsed JSON body for an item's latest sales.

        Sent anonymously (see _sales_headers). Picks the outgoing route (direct
        or a proxy) with the most quota left. Raises RateLimited when the route
        is limited; a 401/403 here is the address being refused, not us."""
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
                                        proxies=route.proxies(),
                                        headers=self._sales_headers())
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
                if self._edge_block(resp):
                    blocked = self._retry_other_route(
                        route, attempt, rl, backoff, market_hash_name,
                        f"HTTP {resp.status_code} from the edge (exit IP blocked)")
                    if blocked is not None:
                        raise blocked
                    attempt += 1
                    backoff = min(backoff * 2, rl.max_backoff_seconds)
                    continue
                # Sales go out with no credentials, so there is none to have
                # expired: at this point CSFloat is refusing the address.
                # Saying "refresh the cookie" would send the reader to renew
                # something the request never carried.
                raise AuthError(
                    f"HTTP {resp.status_code} for {market_hash_name} — "
                    f"история продаж запрашивается без учётных данных, так что "
                    f"отказ относится к адресу, а не к куке. Нужен другой IP."
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

            if self._edge_block(resp):
                # HTTP 200 with a challenge page: the exit IP is being screened.
                blocked = self._retry_other_route(
                    route, attempt, rl, backoff, market_hash_name,
                    "Cloudflare challenge instead of JSON (exit IP screened)")
                if blocked is not None:
                    raise blocked
                attempt += 1
                backoff = min(backoff * 2, rl.max_backoff_seconds)
                continue

            self.last_response_bytes = wire_bytes(resp)
            self.pool.record_success(route)
            self._consecutive_429 = 0  # healthy response clears the escalation
            return resp.json()
