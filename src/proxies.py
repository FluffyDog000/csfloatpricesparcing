"""Proxy pool with per-route quota tracking.

CSFloat's request quota (x-ratelimit-*) is counted per IP, so every proxy —
plus the direct connection — is its own budget. The pool keeps a small state
record per route (remaining quota, reset time, 429 cooldown, failures) and
always hands out the healthy route with the most quota left, which both
multiplies the total budget and keeps any single IP from being hammered.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field

log = logging.getLogger("csfloat.proxies")

DIRECT = "direct"                 # the server's own IP
FAIL_COOLDOWN_SECONDS = 600.0     # park a route after repeated network errors
MAX_FAILS = 3


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

    def proxies(self) -> dict[str, str] | None:
        if not self.url:
            return None
        return {"http": self.url, "https": self.url}

    def quota_exhausted(self, reserve: int) -> bool:
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
        if self.remaining is None:
            return float("inf")   # unknown -> assume fresh, try it
        return float(self.remaining)


class ProxyPool:
    def __init__(self, proxy_urls: list[str], use_direct: bool = True,
                 reserve: int = 15):
        self.reserve = reserve
        self.routes: dict[str, RouteState] = {}
        if use_direct:
            self.routes[DIRECT] = RouteState(key=DIRECT, url=None)
        for url in proxy_urls:
            key = mask_proxy(url)
            self.routes[key] = RouteState(key=key, url=url)

    # -- selection -----------------------------------------------------------

    def pick(self) -> RouteState | None:
        """Best available route, or None when everything is spent/parked."""
        now = time.monotonic()
        usable = [r for r in self.routes.values() if r.available(self.reserve, now)]
        if not usable:
            return None
        best = max(r.score() for r in usable)
        # Among equally-good routes prefer the least recently used one.
        top = [r for r in usable if r.score() == best]
        top.sort(key=lambda r: r.last_used)
        chosen = top[0] if len(top) == 1 else random.choice(top[:2])
        chosen.last_used = now
        return chosen

    def wait_seconds(self) -> float:
        """How long until any route becomes usable again (0 if one is ready)."""
        now = time.monotonic()
        if any(r.available(self.reserve, now) for r in self.routes.values()):
            return 0.0
        waits = []
        for r in self.routes.values():
            candidates = [r.cooldown_until - now, r.parked_until - now]
            if r.quota_exhausted(self.reserve) and r.reset:
                candidates.append(r.reset - time.time())
            waits.append(max(candidates or [0.0]))
        return max(min(waits), 0.0) if waits else 0.0

    # -- feedback ------------------------------------------------------------

    def record_headers(self, route: RouteState, limit, remaining, reset) -> None:
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

    # -- reporting -----------------------------------------------------------

    def total_remaining(self) -> int | None:
        """Sum of quota left across routes (None when nothing is known yet)."""
        known = [r.remaining for r in self.routes.values() if r.remaining is not None]
        return sum(known) if known else None

    def earliest_reset(self) -> int | None:
        resets = [r.reset for r in self.routes.values() if r.reset]
        return min(resets) if resets else None

    def snapshot(self) -> list[dict]:
        now_mono = time.monotonic()
        out = []
        for r in self.routes.values():
            out.append({
                "key": r.key,
                "direct": r.url is None,
                "limit": r.limit,
                "remaining": r.remaining,
                "reset": r.reset,
                "cooldown_sec": max(0, round(r.cooldown_until - now_mono)),
                "parked_sec": max(0, round(r.parked_until - now_mono)),
                "available": r.available(self.reserve, now_mono),
            })
        out.sort(key=lambda d: (not d["direct"], d["key"]))
        return out

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False)


def mask_proxy(url: str) -> str:
    """Readable, credential-free label for a proxy URL (never log passwords)."""
    try:
        scheme, rest = url.split("://", 1)
    except ValueError:
        scheme, rest = "http", url
    host = rest.rsplit("@", 1)[-1]        # strip user:pass@
    return f"{scheme}://{host}"


def parse_proxy_list(raw: str | None) -> list[str]:
    """Proxies from .env: comma, semicolon or newline separated."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.replace(";", ",").splitlines()
             for p in chunk.split(",")]
    return [p for p in parts if p]
