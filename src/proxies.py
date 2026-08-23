"""Proxy pool with per-route quota tracking.

CSFloat's request quota (x-ratelimit-*) is counted per IP, so every proxy —
plus the direct connection — is its own budget. The pool keeps a small state
record per route (remaining quota, reset time, 429 cooldown, failures) and
always hands out the healthy route with the most quota left, which both
multiplies the total budget and keeps any single IP from being hammered.

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
import random
import time
from dataclasses import dataclass, field

log = logging.getLogger("csfloat.proxies")

DIRECT = "direct"                 # the server's own IP
FAIL_COOLDOWN_SECONDS = 600.0     # park a route after repeated network errors
MAX_FAILS = 3

# A rotating route is metered locally over this window instead of by headers.
ROTATING_WINDOW_SECONDS = 86400.0
ROTATING_DEFAULT_LIMIT = 500      # same as one IP's quota: deliberately modest
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

    def score(self) -> float:
        """Higher is better: prefer the route with the most quota left."""
        left = self.effective_remaining()
        if left is None:
            return float("inf")   # unknown -> assume fresh, try it
        return float(left)


class ProxyPool:
    def __init__(self, proxy_urls: list[str], use_direct: bool = True,
                 reserve: int = 15,
                 rotating_limit: int = ROTATING_DEFAULT_LIMIT):
        self.reserve = reserve
        self.rotating_limit = rotating_limit
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
        usable = [r for r in self.routes.values() if r.available(self.reserve, now)]
        if not usable:
            return None
        fixed = [r for r in usable if not r.rotating]
        usable = fixed or usable
        best = max(r.score() for r in usable)
        # Among equally-good routes prefer the least recently used one.
        top = [r for r in usable if r.score() == best]
        top.sort(key=lambda r: r.last_used)
        chosen = top[0] if len(top) == 1 else random.choice(top[:2])
        chosen.last_used = now
        chosen.note_request()     # a rotating route is metered locally
        return chosen

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

    def has_rotating(self) -> bool:
        return any(r.rotating for r in self.routes.values())

    # -- reporting -----------------------------------------------------------

    def total_remaining(self) -> int | None:
        """Sum of quota left across routes (None when nothing is known yet)."""
        known = [r.effective_remaining() for r in self.routes.values()]
        known = [n for n in known if n is not None]
        return sum(known) if known else None

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


def normalize_proxy(url: str) -> str:
    """Accept the formats proxy sellers hand out and return a requests-ready URL.

    "1.2.3.4:8080:user:pass" -> "http://user:pass@1.2.3.4:8080"
    "user:pass@1.2.3.4:8080" -> "http://user:pass@1.2.3.4:8080"
    Anything that already carries a scheme is returned untouched.
    """
    text = (url or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if "@" in text:
        return f"http://{text}"
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
