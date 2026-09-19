"""Proxy pool with per-route quota tracking.

CSFloat's request quota (x-ratelimit-*) is counted per IP, so every proxy —
plus the direct connection — is its own budget. The pool keeps a small state
record per route (remaining quota, reset time, 429 cooldown, failures) and
drains them one at a time: a route is used until its daily quota is nearly
spent, then the next takes over. Spreading requests evenly would multiply the
budget just the same and cost far more — CSFloat counts the addresses one
account speaks from, and an even spread shows it every address at once.

A rotating proxy breaks that accounting: every request leaves from a different
exit IP, so the x-ratelimit-* headers it returns describe a stranger's budget,
not ours, and would read as "quota never runs out". Such a route is marked
`rotating` and gated on a LOCAL request budget instead (see RouteState), which
also caps how many distinct IPs CSFloat sees for one account.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("csfloat.proxies")

DIRECT = "direct"                 # the server's own IP
FAIL_COOLDOWN_SECONDS = 600.0     # park a route after repeated network errors
MAX_FAILS = 3

# What one IP is worth per window, when CSFloat has not told us yet: the
# x-ratelimit-limit it reports has been 500 on every address we have seen.
ASSUMED_IP_LIMIT = 500

# What an order sweep needs from the address it starts on: up to 25 bands plus
# the listing lookups. Starting one on a route with less than this guarantees
# it either stalls halfway or moves to a second address — an extra IP for the
# account, which is the one thing the pool is arranged to avoid.
ORDER_SWEEP_BUDGET = 30

# A rotating route is metered locally over this window instead of by headers.
ROTATING_WINDOW_SECONDS = 86400.0
ROTATING_DEFAULT_LIMIT = ASSUMED_IP_LIMIT   # deliberately modest: one IP's worth
# Markers that tag a proxy line as rotating, e.g. "http://gate:7000 #rotating".
ROTATING_MARKERS = ("#rotating", "#rotate", "#rot", "#ротация", "#ротационный")


@dataclass
class RouteState:
    key: str
    url: str | None = None        # None => direct connection
    remaining: int | None = None
    limit: int | None = None
    reset: int | None = None      # epoch seconds
    cooldown_until: float = 0.0   # monotonic
    fails: int = 0
    parked_until: float = 0.0     # monotonic; set after repeated failures
    last_used: float = 0.0        # monotonic
    # Rotating routes: headers describe the current exit IP, not a budget we
    # own, so they are metered locally over ROTATING_WINDOW_SECONDS.
    rotating: bool = False
    window_used: int = 0
    window_start: float = 0.0     # epoch seconds
    window_limit: int = ROTATING_DEFAULT_LIMIT
    # CSFloat refuses buy orders from addresses it reads as datacenter or VPN
    # ("Disable your VPN", code 170). That is a property of this exit address,
    # not of our credentials, and it says nothing about sales history — which
    # the same route keeps serving. Learned at runtime, never persisted: the
    # address behind a route changes.
    vpn_blocked: bool = False

    def proxies(self) -> dict[str, str] | None:
        if not self.url:
            return None
        return {"http": self.url, "https": self.url}

    def roll_window(self, now: float | None = None) -> None:
        """Start a fresh local budget window once the old one has elapsed."""
        now = time.time() if now is None else now
        if not self.window_start:
            self.window_start = now
        elif now - self.window_start >= ROTATING_WINDOW_SECONDS:
            self.window_start = now
            self.window_used = 0

    def note_request(self) -> None:
        self.roll_window()
        self.window_used += 1

    def effective_remaining(self) -> int | None:
        """Quota left: local counter for a rotating route, CSFloat's header
        otherwise (a rotating route's header belongs to a random exit IP)."""
        if self.rotating:
            self.roll_window()
            return max(self.window_limit - self.window_used, 0)
        return self.remaining

    def effective_reset(self) -> int | None:
        if self.rotating:
            self.roll_window()
            return int(self.window_start + ROTATING_WINDOW_SECONDS)
        return self.reset

    def quota_exhausted(self, reserve: int) -> bool:
        if self.rotating:
            # Local budget: the reserve does not apply, we count every request.
            return self.effective_remaining() <= 0
        if self.remaining is None or self.reset is None:
            return False
        if self.remaining > reserve:
            return False
        return time.time() < self.reset      # spent, and not reset yet

    def available(self, reserve: int, now_mono: float) -> bool:
        return (self.cooldown_until <= now_mono
                and self.parked_until <= now_mono
                and not self.quota_exhausted(reserve))

    def drain_key(self) -> tuple[int, float]:
        """Sort key for draining: the route closest to its ceiling goes first.

        Spreading requests across every healthy route is the obvious policy and
        the wrong one against CSFloat, which counts how many addresses an
        account speaks from: an even spread over a dozen routes showed it a
        dozen IPs an hour and drew "too many requests from too many IPs", with
        every rotating route parked for six hours. Drained one at a time, a
        pool of any size shows one or two addresses a day and keeps the rest in
        reserve — and the daily 500 is a quota, not a rate, so nothing is lost
        by spending it from a single address.

        A route whose budget is still unknown sorts last: it is an address
        nobody has shown CSFloat yet, and opening one early is the whole thing
        we are avoiding."""
        left = self.effective_remaining()
        return (1, 0.0) if left is None else (0, float(left))

    def budget_remaining(self) -> int:
        """Quota left for budgeting, counting an unopened route as a full window.

        effective_remaining() answers "what did CSFloat last tell us about this
        address", which is None for one we have never used — right for picking
        a route, wrong for adding up a budget. Draining leaves most of the pool
        deliberately unopened, so summing it the strict way reported forty
        fresh proxies as nothing: the dashboard read "0 доступно сейчас" beside
        twenty thousand requests held in reserve, and the pacing maths stretched
        every interval to fit a budget that was not the real one."""
        left = self.effective_remaining()
        if left is not None:
            return int(left)
        return int(self.window_limit if self.rotating
                   else (self.limit or ASSUMED_IP_LIMIT))


class ProxyPool:
    def __init__(self, proxy_urls: list[str], use_direct: bool = True,
                 reserve: int = 15,
                 rotating_limit: int = ROTATING_DEFAULT_LIMIT):
        self.reserve = reserve
        self.rotating_limit = rotating_limit
        self._pinned: RouteState | None = None
        self._orders_mode = False
        self.last_picked: RouteState | None = None
        self.routes: dict[str, RouteState] = {}
        if use_direct:
            self.routes[DIRECT] = RouteState(key=DIRECT, url=None)
        for line in proxy_urls:
            url, rotating = split_proxy_flags(line)
            key = mask_proxy(url)
            self.routes[key] = RouteState(key=key, url=url, rotating=rotating,
                                          window_limit=rotating_limit)

    def replace(self, proxy_urls: list[str], use_direct: bool = True,
                rotating_limit: int | None = None) -> bool:
        """Swap in a new proxy list, preserving the quota/cooldown state (and a
        rotating route's spent local budget) of any route that is still
        present. Returns True if anything changed."""
        if rotating_limit is not None:
            self.rotating_limit = rotating_limit
        wanted: dict[str, tuple[str | None, bool]] = {}
        if use_direct:
            wanted[DIRECT] = (None, False)
        for line in proxy_urls:
            url, rotating = split_proxy_flags(line)
            wanted[mask_proxy(url)] = (url, rotating)

        changed = set(wanted) != set(self.routes)
        kept = {k: v for k, v in self.routes.items() if k in wanted}
        for key, (url, rotating) in wanted.items():
            route = kept.get(key)
            if route is None:
                kept[key] = RouteState(key=key, url=url, rotating=rotating,
                                       window_limit=self.rotating_limit)
                continue
            route.url = url                        # credentials may have changed
            if route.rotating != rotating:
                changed = True
                route.rotating = rotating
                route.window_used = 0              # budgets are not comparable
                route.window_start = 0.0
            route.window_limit = self.rotating_limit
        self.routes = kept
        # A pin points at a route object; after a rebuild it may no longer be
        # in the pool at all.
        self._pinned = None
        if changed:
            log.info("Proxy pool updated: %d route(s) — %s",
                     len(self.routes), ", ".join(sorted(self.routes)))
        return changed

    # -- selection -----------------------------------------------------------

    def pick(self) -> RouteState | None:
        """Best available route, or None when everything is spent/parked.

        Fixed routes are always drained first and a rotating one is used only
        as overflow: every rotating request shows CSFloat another IP for the
        same account, which is exactly what its "too many IPs" check counts.
        """
        now = time.monotonic()
        if self._pinned is not None and self._pinned.available(self.reserve, now):
            chosen = self._pinned
        else:
            chosen = self._choose(now)
        if chosen is None:
            return None
        chosen.last_used = now
        chosen.note_request()     # a rotating route is metered locally
        self.last_picked = chosen
        return chosen

    def _choose(self, now: float) -> RouteState | None:
        """Selection only, no metering — so pinning a route does not book a
        request that has not been made."""
        usable = [r for r in self.routes.values() if r.available(self.reserve, now)]
        if self._orders_mode:
            # Buy orders only: an address CSFloat has refused as datacenter or
            # VPN answers every band the same way, so it is out for this sweep
            # even though it still serves sales history perfectly well.
            usable = [r for r in usable if not r.vpn_blocked]
            # The server's own address is a datacenter one, so the bands would
            # be refused there anyway; spending the listing lookups from it
            # only burns its quota. It stays a candidate when it is all there
            # is — a collector running from home has no such problem.
            proxied = [r for r in usable if r.url is not None]
            usable = proxied or usable
            # And start where there is budget to finish: draining picks the
            # most spent address, which is exactly the one a twenty-request
            # sweep should not begin on.
            deep = [r for r in usable if r.budget_remaining() >= ORDER_SWEEP_BUDGET]
            usable = deep or usable
        if not usable:
            return None
        fixed = [r for r in usable if not r.rotating]
        usable = fixed or usable
        # Drain, don't alternate — see RouteState.drain_key.
        usable.sort(key=lambda r: (r.drain_key(), r.key))
        return usable[0]

    def pin(self, for_orders: bool = False) -> RouteState | None:
        """Hold one route for a burst of requests, and return it.

        `for_orders` also keeps the burst off addresses CSFloat has refused as
        datacenter or VPN — those serve sales history fine, so they stay in the
        pool, but a buy-order sweep must not be handed one.

        Hopping to whichever route has the most quota left is right for polls
        spread over hours and wrong for a burst: an order sweep fires a dozen
        requests inside ninety seconds, and hopping showed CSFloat a dozen IPs
        for one account in that window — precisely what its "too many requests
        from too many IPs" check counts, and what put every rotating route in
        quarantine. Pinned, the whole sweep leaves from one address.

        A pinned route that goes on cooldown or runs out of quota stops being
        honoured, so a pin can never wedge the pool."""
        self._orders_mode = for_orders
        self._pinned = self._choose(time.monotonic())
        return self._pinned

    def unpin(self) -> None:
        self._pinned = None
        self._orders_mode = False

    def mark_vpn_blocked(self, route: RouteState | None = None) -> RouteState | None:
        """Remember that this address is refused for buy orders.

        Defaults to the route that served the last request, which is the one
        that drew the refusal. The flag lives only in memory: the address
        behind a route changes, and a restart is the cheapest way to re-test."""
        # A sweep pins its route up front, so the pin is the address that drew
        # the refusal even when the request never reached pick() to record it.
        # Without this fallback nothing was flagged, pin() handed back the same
        # refused address, and the sweep walked every band into the same wall.
        route = route or self.last_picked or self._pinned
        if route is None or route.vpn_blocked:
            return route
        route.vpn_blocked = True
        log.warning("Route %s refused for buy orders (datacenter/VPN); it stays "
                    "in the pool for sales history", route.key)
        return route

    def has_order_route(self) -> bool:
        """Is any route still allowed to read buy orders?"""
        now = time.monotonic()
        return any(r.available(self.reserve, now) and not r.vpn_blocked
                   for r in self.routes.values())

    def wait_seconds(self) -> float:
        """How long until any route becomes usable again (0 if one is ready)."""
        now = time.monotonic()
        if any(r.available(self.reserve, now) for r in self.routes.values()):
            return 0.0
        waits = []
        for r in self.routes.values():
            candidates = [r.cooldown_until - now, r.parked_until - now]
            reset = r.effective_reset()
            if r.quota_exhausted(self.reserve) and reset:
                candidates.append(reset - time.time())
            waits.append(max(candidates or [0.0]))
        return max(min(waits), 0.0) if waits else 0.0

    # -- feedback ------------------------------------------------------------

    def record_headers(self, route: RouteState, limit, remaining, reset) -> None:
        if route.rotating:
            # Kept only for display: these belong to whatever exit IP served
            # this request, so they say nothing about our own budget.
            route.limit, route.remaining, route.reset = limit, remaining, reset
            return
        if limit is not None:
            route.limit = limit
        if remaining is not None:
            route.remaining = remaining
        if reset is not None:
            route.reset = reset

    def record_success(self, route: RouteState) -> None:
        route.fails = 0
        route.cooldown_until = 0.0

    def record_429(self, route: RouteState, wait_seconds: float) -> None:
        route.cooldown_until = time.monotonic() + wait_seconds
        log.warning("Route %s rate-limited; parked for %.1f min",
                    route.key, wait_seconds / 60.0)

    def record_failure(self, route: RouteState, exc: object) -> None:
        route.fails += 1
        if route.fails >= MAX_FAILS:
            route.parked_until = time.monotonic() + FAIL_COOLDOWN_SECONDS
            route.fails = 0
            log.warning("Route %s failed repeatedly (%s); parked for %.0f min",
                        route.key, exc, FAIL_COOLDOWN_SECONDS / 60.0)

    def park_rotating(self, seconds: float) -> int:
        """CSFloat complained about too many IPs for one account — stop using
        every rotating route for a while. Fixed routes keep working."""
        parked = 0
        for r in self.routes.values():
            if r.rotating:
                r.parked_until = max(r.parked_until, time.monotonic() + seconds)
                parked += 1
        if parked:
            log.error("Account-level IP complaint from CSFloat: parked %d "
                      "rotating route(s) for %.1f h", parked, seconds / 3600.0)
        return parked

    def unpark_rotating(self) -> int:
        """Lift the account-IP quarantine early, at the operator's decision.

        Only clears the park this pool applied — a route in a cooldown from its
        own 429, or with its quota spent, stays unavailable on its own terms."""
        lifted = 0
        for r in self.routes.values():
            if r.rotating and r.parked_until > time.monotonic():
                r.parked_until = 0.0
                lifted += 1
        if lifted:
            log.warning("Account-IP quarantine lifted manually for %d route(s)",
                        lifted)
        return lifted

    def has_rotating(self) -> bool:
        return any(r.rotating for r in self.routes.values())

    # -- reporting -----------------------------------------------------------

    def total_remaining(self) -> int | None:
        """Sum of quota left across routes — an unopened one counts as a full
        window, since that is what it will report the moment it is used."""
        if not self.routes:
            return None
        return sum(r.budget_remaining() for r in self.routes.values())

    def total_limit(self) -> int | None:
        """Sum of the routes' quota ceilings, to pair with total_remaining.

        Reporting a summed remaining against a single IP's limit produced
        nonsense on the dashboard — "8500 из 500 на окно"."""
        if not self.routes:
            return None
        return sum(int(r.window_limit if r.rotating else (r.limit or ASSUMED_IP_LIMIT))
                   for r in self.routes.values())

    def usable_remaining(self) -> int:
        """Quota that can actually be spent right now — parked routes hold
        budget nobody can use, and summing it reads as "plenty left"."""
        now = time.monotonic()
        return sum(r.budget_remaining() for r in self.routes.values()
                   if r.available(self.reserve, now))

    def earliest_reset(self) -> int | None:
        resets = [r.effective_reset() for r in self.routes.values()]
        resets = [n for n in resets if n]
        return min(resets) if resets else None

    def usage_snapshot(self) -> dict[str, list[float]]:
        """Local budget counters, so a restart does not hand a rotating route a
        fresh 500 requests it has already spent."""
        return {r.key: [r.window_used, r.window_start]
                for r in self.routes.values() if r.rotating and r.window_start}

    def restore_usage(self, data: dict) -> None:
        for key, pair in (data or {}).items():
            route = self.routes.get(key)
            if route is None or not route.rotating:
                continue
            try:
                used, start = int(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if time.time() - start < ROTATING_WINDOW_SECONDS:
                route.window_used, route.window_start = used, start

    def snapshot(self) -> list[dict]:
        now_mono = time.monotonic()
        out = []
        for r in self.routes.values():
            out.append({
                "key": r.key,
                "direct": r.url is None,
                "rotating": r.rotating,
                "limit": r.window_limit if r.rotating else r.limit,
                "remaining": r.effective_remaining(),
                "reset": r.effective_reset(),
                "cooldown_sec": max(0, round(r.cooldown_until - now_mono)),
                "parked_sec": max(0, round(r.parked_until - now_mono)),
                "available": r.available(self.reserve, now_mono),
                "vpn_blocked": r.vpn_blocked,
            })
        out.sort(key=lambda d: (not d["direct"], d["key"]))
        return out

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False)


def split_proxy_flags(line: str) -> tuple[str, bool]:
    """Split a stored proxy line into (url, rotating).

    A trailing marker tags the line as a rotating endpoint:
        "http://gate.provider.com:7000 #rotating" -> (url, True)
    """
    text = (line or "").strip()
    rotating = False
    lowered = text.lower()
    for marker in ROTATING_MARKERS:
        idx = lowered.rfind(marker)
        if idx != -1 and text[idx:].lower().strip() == marker:
            text = text[:idx].strip()
            rotating = True
            break
    return normalize_proxy(text), rotating


def _looks_like_host(text: str) -> bool:
    """A hostname or IP, as opposed to a login or a password."""
    return "." in text or text.lower() == "localhost"


def normalize_proxy(url: str) -> str:
    """Accept the formats proxy sellers hand out and return a requests-ready URL.

    Providers emit the same four fields in either order, so both are accepted:

    "1.2.3.4:8080:user:pass" -> "http://user:pass@1.2.3.4:8080"
    "user:pass:1.2.3.4:8080" -> "http://user:pass@1.2.3.4:8080"
    "user:pass@1.2.3.4:8080" -> "http://user:pass@1.2.3.4:8080"

    Anything that already carries a scheme is returned untouched.
    """
    text = (url or "").strip()
    if not text or "://" in text:
        return text
    if "@" in text:
        return f"http://{text}"
    parts = text.split(":")
    if len(parts) == 4:
        head_is_host = parts[1].isdigit() and _looks_like_host(parts[0])
        tail_is_host = parts[3].isdigit() and _looks_like_host(parts[2])
        # When both halves could be the host, trust the one that also parses as
        # a hostname; a numeric password would otherwise flip the whole line.
        if head_is_host and not tail_is_host:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        if tail_is_host:
            user, password, host, port = parts
            return f"http://{user}:{password}@{host}:{port}"
        if parts[1].isdigit():
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        if parts[3].isdigit():
            user, password, host, port = parts
            return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{text}"
    return text


def mask_proxy(url: str) -> str:
    """Readable, credential-free label for a proxy URL (never log passwords).

    Sticky sessions of one provider differ only by the login (…-sid-1-ttl-30),
    so host:port alone would collapse them into a single route. A short digest
    of the credentials keeps them apart without revealing anything.
    """
    try:
        scheme, rest = url.split("://", 1)
    except ValueError:
        scheme, rest = "http", url
    creds, sep, host = rest.rpartition("@")
    if not sep:                           # no credentials in the URL
        return f"{scheme}://{rest}"
    digest = hashlib.sha1(creds.encode("utf-8")).hexdigest()[:4]
    return f"{scheme}://{host}#{digest}"


ALLOWED_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://")


def validate_proxy(line: str) -> tuple[bool, str]:
    """(ok, message) — a proxy line must be scheme://[user:pass@]host:port,
    optionally tagged "#rotating"."""
    url, _ = split_proxy_flags(line)
    if not url:
        return False, "пустой адрес"
    if "://" not in url:
        return False, "нужна схема: http://, https:// или socks5://"
    if not url.lower().startswith(ALLOWED_SCHEMES):
        return False, "поддерживаются только http, https, socks5, socks4"
    host = url.split("://", 1)[1].rsplit("@", 1)[-1]
    if not host or host.startswith(":"):
        return False, "не указан хост"
    if ":" not in host:
        return False, "не указан порт (например http://host:8080)"
    hostname, port = host.rsplit(":", 1)
    if not hostname:
        return False, "не указан хост"
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        return False, f"некорректный порт «{port}»"
    return True, "ok"


def parse_proxy_list(raw: str | None) -> list[str]:
    """Proxies from .env: comma, semicolon or newline separated."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.replace(";", ",").splitlines()
             for p in chunk.split(",")]
    return [p for p in parts if p]
