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
from .keyring import Quota, kind_of, reset_epoch
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

# How many keys one request may go through before giving up. A key that is out
# of quota or refused is the key's problem, not the request's: the next key
# answers it, and the band is not lost.
RING_TRIES = 4

# A ring key's 429 that names no reset: hold it this long.
KEY_COOLDOWN_DEFAULT = 60.0

KIND_LABELS = {"listings": "листинги (200/час на ключ)",
               "book": "стакан ордеров (20/мин на ключ)",
               "me": "чтение своего аккаунта",
               "create": "создание новых ордеров (200 в сутки)",
               "write": "поднятие цены и снятие ордеров",
               "other": "запросы"}

# How many addresses the main key may speak from. CSFloat's support allowed
# 2-4 per key. Left to the pool, the main key went out through each of 42
# proxies in turn and the account drew "too many requests from too many IPs",
# locked for most of a day.
MAIN_KEY_ROUTES = 3

# A main-key limit that resets within this long is waited out rather than
# reported. Placing orders runs on a window of a minute: refusing the rest of
# a plan the moment it filled turned 66 orders into one placed and 65
# "limit, resets in 59 s".
MAIN_KEY_WAIT_MAX = 180.0

# How many times one write is sent when CSFloat answers 429.
WRITE_TRIES = 3

# The longest a main-key refusal holds that kind of request back, whatever the
# header says.
MAIN_KEY_HOLD_MAX = 24 * 3600.0


def _measured(resp, how) -> int:
    """Bytes on the wire, or 0 when a response cannot say - counting must
    never be what fails a request."""
    import urllib3
    try:
        return int(how(resp) or 0)
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        # The body broke off mid-read: the same network failure the request
        # itself would have raised unstreamed, so it is handled as one.
        raise requests.ConnectionError(str(exc)) from exc
    except Exception:  # noqa: BLE001 - a stand-in response has no wire to read
        return 0


def _ring_kind(kind: str) -> str:
    """A keyring kind as a traffic kind: the two the sweeps spend on, and
    everything else on the analysis keys together."""
    return kind if kind in ("book", "listings") else "ring"


def account_kind(method: str, url: str) -> str:
    """Which of the main key's counters a request is counted against.

    Creating an order has a counter of its own - 200 a day from the first
    one - apart from amending and taking down, which share the account's
    50,000. Held together, a spent day of creating also stopped the defence
    raising prices and the brake taking orders down, for twenty hours.
    """
    if method.upper() == "POST":
        return "create"
    if method.upper() != "GET":
        return "write"
    if "/api/v1/me" in url.split("?", 1)[0]:
        return "me"
    return kind_of(url)


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


class KeyRejected(AuthError):
    """CSFloat refused one of the analysis keys from keys.txt.

    An AuthError, so existing handlers stop the same way, but named apart: the
    message used to blame CSFLOAT_API_KEY in .env, which was never sent, and
    left the operator nothing to find the bad key by."""


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


def decompress(body: bytes, encoding: str) -> bytes:
    """Undo the codec the server named. Unknown or absent: hand it back as is."""
    enc = (encoding or "").strip().lower()
    if enc == "gzip":
        import gzip
        return gzip.decompress(body)
    if enc == "deflate":
        import zlib
        try:
            return zlib.decompress(body)
        except zlib.error:                      # raw deflate, no zlib wrapper
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if enc == "br":
        import brotli
        return brotli.decompress(body)
    if enc == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompress(body)
    return body


def absorb(resp) -> int:
    """Read a streamed body into the response, and report its size on the wire.

    Only way to know what the link carried. CSFloat answers chunked, and
    urllib3's byte counter does not count chunked responses - it stays at zero,
    which is how a traffic report meant to replace `len(resp.content)` ended up
    printing exactly that. Reading the stream undecoded is the measurement.

    Afterwards the response behaves like any other: `.text`, `.json()` and the
    Cloudflare check all read the body we just put back, so nothing downstream
    knows this happened. A body that will not decode is stored as it came -
    the JSON parse then fails, which is the existing error path, rather than
    this helper deciding a poll is lost.
    """
    raw = getattr(resp, "raw", None)
    if raw is None or not hasattr(raw, "read"):
        # A stand-in response in the tests, or one already consumed. Nothing to
        # read back, so fall through to whatever the headers admit to.
        return wire_bytes(resp)
    packed = raw.read(decode_content=False)
    try:
        plain = decompress(packed, resp.headers.get("Content-Encoding", ""))
    except Exception:  # noqa: BLE001 - a bad body is the parser's to report
        plain = packed
    resp._content = plain
    resp._content_consumed = True
    return len(packed)


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
        # Addresses for the analysis keys only, set on the load page. Empty:
        # the keys share the main pool, as before. Kept apart, a sweep's burst
        # cannot spend the addresses the sales polls and our own orders use.
        self.key_pool: ProxyPool | None = None
        # The main key's own counters, per kind of request. Its limits belong
        # to the key: written onto the addresses it spoke from, one refusal
        # for placing too many orders read as every address spent for
        # seventeen hours - and stopped the anonymous sales polls with it.
        self.main_quota: dict[str, Quota] = {}
        # The main key's own addresses, set on the load page. None: it takes
        # MAIN_KEY_ROUTES fixed addresses from the main pool.
        self.main_pool: ProxyPool | None = None
        self.main_cooldown: dict[str, float] = {}   # monotonic
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
        from .traffic import Traffic
        # Every response, by kind - see traffic.py. Written out by the collector.
        self.traffic = Traffic()
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

    def _main_seed(self) -> str:
        return (getattr(self.http, "api_key", None) or self.http.authorization
                or self.http.cookie or "session")

    def account_routes(self) -> list:
        """The main key's own addresses: the list set for it on the load page
        when there is one, else MAIN_KEY_ROUTES fixed ones from the main pool.
        Rotating proxies never: each request through one is another IP."""
        own = self.main_pool
        if own is not None and own.routes:
            return [r for r in own.routes.values() if not r.rotating]
        return self.pool.account_routes(self._main_seed(), MAIN_KEY_ROUTES)

    def _account_route(self):
        """One of the main key's own addresses, taking turns between them.

        Never another one: a burst through the wider pool is exactly what
        CSFloat reads as one account touring the internet. When all of them
        are down, the request waits for one rather than borrowing.
        """
        now = time.monotonic()
        mine = self.account_routes()
        if not mine:
            raise NoRouteAvailable(
                "нет ни одного постоянного прокси для главного ключа — добавь "
                "их на «Нагрузке» (ротационные для него не годятся)")
        usable = [r for r in mine if r.reachable(now)]
        if not usable:
            wait = min(r.reach_wait(now) for r in mine)
            raise NoRouteAvailable(
                f"адреса главного ключа недоступны, ближайший через "
                f"{wait / 60:.0f} мин")
        # Buy orders are refused from datacenter addresses; prefer the others.
        clean = [r for r in usable if not r.vpn_blocked]
        usable = clean or usable
        usable.sort(key=lambda r: r.last_used)
        route = usable[0]
        route.last_used = now
        route.note_request()
        self.pool.last_picked = route
        self.last_route = route.key
        return route

    def main_key_wait(self, kind: str) -> float:
        """Seconds until the main key may make this kind of request."""
        q = self.main_quota.get(kind)
        quota = q.wait(time.time()) if q else 0.0
        cool = self.main_cooldown.get(kind, 0.0) - time.monotonic()
        return max(quota, cool, 0.0)

    def main_key_snapshot(self) -> list[dict]:
        """The main key's counters, for the load page."""
        out = []
        for kind in sorted(set(self.main_quota) | set(self.main_cooldown)):
            q = self.main_quota.get(kind) or Quota()
            out.append({"kind": kind, "label": KIND_LABELS.get(kind, kind),
                        "limit": q.limit, "remaining": q.remaining,
                        "reset": q.reset,
                        "wait_sec": round(self.main_key_wait(kind))})
        return out

    def _lease(self, account: bool = False, kind: str = "other"):
        """Who speaks, from where, and after what wait.

        Returns (route, key). Without a ring - or for a request about our own
        account, which the ring must never carry - this is the old behaviour
        spelled out: the main pool picks (honouring a pinned route), the shared
        clock paces, and the credential is the one `.env` configured.
        """
        if self.keyring is None or account:
            held = self.main_key_wait(kind)
            deadline = time.monotonic() + MAIN_KEY_WAIT_MAX
            while 0 < held and time.monotonic() + held <= deadline:
                q = self.main_quota.get(kind)
                log.info("Main key: %s limit (%s of %s left), waiting %.0f s "
                         "for the reset", KIND_LABELS.get(kind, kind),
                         q.remaining if q else "?", q.limit if q else "?", held)
                time.sleep(held + 0.5)
                held = self.main_key_wait(kind)
            if held > 0:
                minutes = held / 60.0
                raise NoRouteAvailable(
                    f"главный ключ (.env): лимит CSFloat на "
                    f"{KIND_LABELS.get(kind, kind)}, сброс через "
                    + (f"{minutes:.0f} мин" if minutes >= 1 else f"{held:.0f} с"))
            route = self._account_route()
            self._respect_spacing()
            return route, None

        # A ring paces itself: `lease` hands back only a key whose own clock
        # has come round, so there is no global gap to respect.
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while True:
            leased = self.keyring.lease(kind)
            if leased is not None:
                state, route = leased
                return route, state
            wait = self.keyring.wait_seconds(kind)
            if wait == float("inf"):
                if not self.keyring.live():
                    raise NoRouteAvailable(
                        "все ключи из keys.txt отклонены CSFloat — список на "
                        "вкладке «Нагрузка»")
                raise NoRouteAvailable(
                    "ни у одного ключа нет адреса из текущего списка прокси — "
                    "проверь список на вкладке «Нагрузка»")
            if time.monotonic() + wait > deadline:
                minutes = wait / 60.0
                raise NoRouteAvailable(
                    f"у всех ключей кончился лимит: {KIND_LABELS.get(kind, kind)}; "
                    "ближайший освободится через "
                    + (f"{minutes:.0f} мин" if minutes >= 1 else f"{wait:.0f} с"))
            if time.monotonic() >= deadline:
                raise NoRouteAvailable("все ключи заняты другими потоками обхода")
            # Zero means one is ready by every measure and another thread took
            # it first: try again shortly rather than give up on a race.
            time.sleep(min(max(wait, 0.2), 1.0))

    def _respect_spacing(self) -> None:
        """Ensure at least `min_seconds_between_requests` between calls."""
        with self._lock:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.polling.min_seconds_between_requests - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def fetch_json(self, url: str, headers: dict[str, str] | None = None,
                   account: bool = False) -> object:
        """One-off GET for a small side endpoint (currently the FX rate).

        Goes through the pool and the request spacing like any other call, and
        a 429 arms the same cooldown a sales poll would — the limit belongs to
        the account, not to the endpoint. It does not retry, though: nothing
        here is worth delaying the sales polling for.

        `account` marks a read about OUR account - its own buy orders - which
        has to go out on the main key, never one from the analysis ring.
        """
        if self.keyring is not None and not account:
            return self._fetch_on_ring(url, headers)
        kind = account_kind("GET", url)
        # A read is safe to send again, so a dead address costs a retry on the
        # next of the main key's own few rather than the whole request: one
        # proxy answering "host unreachable" read as "CSFloat has no trades".
        tries = max(len(self.account_routes()), 1)
        for attempt in range(tries):
            route, key = self._lease(account, kind)
            sent = (self._account_headers(headers) if account
                    else self._with_key(headers, key))
            try:
                resp = self.session.get(url, timeout=self.http.timeout_seconds,
                                        proxies=route.proxies(), headers=sent,
                                        stream=True)
                self.traffic.add("account" if account else _ring_kind(kind_of(url)),
                                 _measured(resp, absorb))
            except requests.RequestException as exc:
                # Fault the route like a sales poll does, so a proxy that
                # keeps dropping connections leaves rotation.
                self.pool.record_failure(route, exc)
                log.warning("Read through %s failed: %s", route.key, exc)
                if attempt + 1 >= tries:
                    raise
                continue
            return self._read(resp, route, url, key, kind)
        raise RuntimeError("unreachable")

    def _fetch_on_ring(self, url: str, headers=None) -> object:
        """A read on one of the analysis keys, moving on to the next key when
        this one is out of quota or refused.

        Either way it is the key that failed, not the request: with a hundred
        keys the next one answers it. Giving up on the first 429 lost the band,
        and with it half the item - "стакан прочитан, листинги нет".
        """
        kind = kind_of(url)
        last: Exception | None = None
        for _ in range(RING_TRIES):
            try:
                route, key = self._lease(kind=kind)
            except NoRouteAvailable:
                # The refusal that just took out the last key says more than
                # "no keys left" does: it names the key.
                if isinstance(last, KeyRejected):
                    raise last
                raise
            try:
                resp = self.session.get(url, timeout=self.http.timeout_seconds,
                                        proxies=route.proxies(),
                                        headers=self._with_key(headers, key),
                                        stream=True)
                self.traffic.add(_ring_kind(kind), _measured(resp, absorb))
            except requests.RequestException as exc:
                self.pool.record_failure(route, exc)
                self.keyring.note_failure(key.key, f"{type(exc).__name__}: {exc}")
                raise
            try:
                return self._read(resp, route, url, key, kind)
            except (KeyRejected, RateLimited) as exc:
                last = exc
                continue
        assert last is not None
        raise last

    def send_json(self, method: str, url: str, body: object | None = None,
                  headers: dict[str, str] | None = None) -> object:
        """A write: create, amend or take down one of our own buy orders.

        Held to the same pool, spacing and limits as a read, because CSFloat
        counts them against the same account and the same address. Writes are
        never retried on a guess: a request that may already have placed an
        order is not one to send twice. The exception is a 429 - "too many,
        slow down" means the request was not carried out - which is sent again
        once the window it names has passed. CSFloat sends no counter with
        order placement, only the refusal, so without this every refusal cost
        an order: a plan of 66 lost one a minute while the bot waited.

        Always on the main account (see `_account_headers`). With a key ring
        attached this used to take whichever ring key was free, and an order
        goes to the account of the key that placed it.
        """
        kind = account_kind(method, url)
        headers = self._account_headers(headers)
        for attempt in range(WRITE_TRIES):
            # Waits out a short window itself; a long one is raised from here.
            route, key = self._lease(account=True, kind=kind)
            try:
                resp = self.session.request(
                    method.upper(), url, json=body, headers=headers,
                    timeout=self.http.timeout_seconds, proxies=route.proxies())
                self.traffic.add("account", _measured(resp, wire_bytes))
            except requests.RequestException as exc:
                self.pool.record_failure(route, exc)
                raise
            try:
                return self._read(resp, route, url, key, kind)
            except NoRouteAvailable:
                raise
            except RateLimited:
                if resp.status_code != 429 or attempt + 1 >= WRITE_TRIES:
                    raise
                log.info("Write refused with 429; sending it again after the "
                         "window (%d/%d)", attempt + 2, WRITE_TRIES)
        raise RateLimited("unreachable")

    def _account_headers(self, headers):
        """The main account's credentials: CSFLOAT_API_KEY from .env, when it
        is set, over whatever Authorization the session carries. The keys in
        keys.txt are for reading the market; which account an order lands on
        is decided here and nowhere else."""
        key = getattr(self.http, "api_key", None)
        if not key:
            return headers
        out = dict(headers or {})
        out["Authorization"] = key
        return out

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

    def _feed_key(self, key, kind: str, resp) -> None:
        """A ring key's counters are the key's own: kept on the key, apart
        from the address's and from the account-wide snapshot the dashboard
        reads, which the anonymous sales polls describe."""
        h = resp.headers or {}
        self.keyring.note_quota(key.key, kind, h.get("x-ratelimit-limit"),
                                h.get("x-ratelimit-remaining"),
                                h.get("x-ratelimit-reset"))

    def _feed_main(self, kind: str, resp) -> None:
        h = resp.headers or {}

        def num(name):
            try:
                return int(float(h.get(name))) if h.get(name) is not None else None
            except (TypeError, ValueError):
                return None
        limit, remaining = num("x-ratelimit-limit"), num("x-ratelimit-remaining")
        if limit is None and remaining is None:
            return
        q = self.main_quota.setdefault(kind, Quota())
        if limit is not None:
            q.limit = limit
        if remaining is not None:
            q.remaining = remaining
        when = reset_epoch(h.get("x-ratelimit-reset"))
        if when is not None:
            q.reset = when

    def _main_cooldown(self, kind: str, resp, retry_after) -> float:
        """Hold the main key back from this kind of request, and only this
        kind: a limit on placing orders says nothing about reading the
        account, and nothing at all about the anonymous sales polls."""
        wait = retry_after or 0.0
        when = reset_epoch((resp.headers or {}).get("x-ratelimit-reset"))
        if when is not None:
            wait = max(wait, when - time.time())
        if wait <= 0:
            wait = KEY_COOLDOWN_DEFAULT
        wait = min(wait, MAIN_KEY_HOLD_MAX)
        self.main_cooldown[kind] = max(self.main_cooldown.get(kind, 0.0),
                                       time.monotonic() + wait)
        log.warning("Main key refused for %s; held back %.0f min",
                    KIND_LABELS.get(kind, kind), wait / 60.0)
        return wait

    def _key_cooldown(self, key, kind: str, resp, retry_after) -> float:
        """Hold one key back from one kind of request until CSFloat's own
        reset for it - not the escalating guess, which would park a key whose
        order-book window refills in a minute for a quarter of an hour."""
        wait = retry_after or 0.0
        when = reset_epoch((resp.headers or {}).get("x-ratelimit-reset"))
        if when is not None:
            wait = max(wait, when - time.time())
        if wait <= 0:
            wait = KEY_COOLDOWN_DEFAULT
        wait = min(wait, ACCOUNT_BLOCK_SECONDS)
        return self.keyring.note_rate_limit(key.key, wait, kind)

    def _read(self, resp, route, url: str, key=None,
              kind: str = "other") -> object:
        """Shared handling: the limits and refusals are the same either way."""
        ring = key is not None and self.keyring is not None
        if ring:
            self._feed_key(key, kind, resp)
        else:
            # Everything through here carries a credential - the main key or
            # the session - so its counters are the key's, not the address's.
            self._feed_main(kind, resp)

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
            if ring:
                wait = self._key_cooldown(key, kind, resp, retry_after)
                raise RateLimited(
                    f"ключ {key.tail}: лимит — {KIND_LABELS.get(kind, kind)}, "
                    f"ждёт {wait / 60:.1f} мин")
            wait = self._main_cooldown(kind, resp, retry_after)
            raise RateLimited(
                f"главный ключ (.env): лимит CSFloat на "
                f"{KIND_LABELS.get(kind, kind)}, сброс через {wait / 60:.0f} мин")

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

        if resp.status_code in (401, 403) and ring:
            # One key out of keys.txt, refused: take it out so it stops eating
            # bands, and name it by its last characters so it can be found.
            detail = (resp.text or "").strip().replace("\n", " ")[:120]
            reason = (f"CSFloat отклонил ключ (HTTP {resp.status_code})"
                      + (f": {detail}" if detail else ""))
            self.keyring.disable(key.key, reason)
            raise KeyRejected(
                f"ключ {key.tail} из keys.txt отклонён CSFloat "
                f"(HTTP {resp.status_code}) — убран из работы", resp)

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
        # A Doppler phase is the item's real name plus its paint index (see
        # src/phases.py); asked for by the phase's own name, CSFloat has none.
        from .phases import split

        base, index = split(market_hash_name)
        encoded = quote(base, safe="")
        path = self.http.sales_path_template.format(name=encoded)
        if index is not None:
            path += ("&" if "?" in path else "?") + f"paint_index={index}"
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
                # stream=True leaves the body unread, which is the only
                # moment its size on the wire can be taken; absorb() puts it
                # straight back, so everything below reads it as before.
                resp = self.session.get(url, timeout=self.http.timeout_seconds,
                                        proxies=route.proxies(),
                                        headers=self._sales_headers(),
                                        stream=True)
                measured = absorb(resp)
                self.traffic.add("history", measured)
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

            self.last_response_bytes = measured
            self.pool.record_success(route)
            self._consecutive_429 = 0  # healthy response clears the escalation
            return resp.json()
