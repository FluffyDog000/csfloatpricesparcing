"""Pair each API key with a small, fixed set of exit addresses.

CSFloat granted this account a hundred API keys and its support advised
running 2-4 different IPs per key. That advice sets the shape of this module,
and it relaxes exactly one existing rule: RouteState.drain_key spends the
proxy pool one address at a time, because an even spread once showed CSFloat
a dozen addresses for one account in ninety seconds and drew "too many
requests from too many IPs". The caution was right while the rule was
unknown; now the rule has a number. A key with three addresses of its own is
inside it, and a hundred such keys are a hundred ordinary-looking clients
rather than one account touring the internet.

The pairing has to be STABLE. A key that speaks from three addresses today
and three others tomorrow is the touring account again, just slower, so the
binding is derived from the key itself rather than from load or from the
order keys happen to sit in a file: the same key comes back to the same
addresses across restarts, pool edits and reorderings.

Spacing is per key for the same reason it used to be global — it paces one
client — but a hundred clients that share one clock are one client. Each key
keeps its own, which is what lets a sweep run several books at once.
"""
from __future__ import annotations

import logging
import hashlib
import threading
import time
from dataclasses import dataclass, field

from .proxies import ProxyPool, RouteState

# Support's range. Below it a key looks like a script pinned to one address;
# above it the account starts tripping the too-many-IPs check again.
MIN_ROUTES_PER_KEY = 2
MAX_ROUTES_PER_KEY = 4
DEFAULT_ROUTES_PER_KEY = 3

log = logging.getLogger("csfloat.keyring")


def fingerprint(key: str) -> str:
    """Name a key in logs and dashboards without disclosing it."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


# How much of a key is shown so the operator can find it in keys.txt. A digest
# is safe and useless for that: nobody can grep a file for "3f9a01c2".
TAIL_CHARS = 10


def tail(key: str) -> str:
    """The end of a key, enough to find it in keys.txt.

    Never more than half of it: a short key printed in full would be the key.
    """
    shown = min(TAIL_CHARS, len(key) // 2)
    return "…" + key[-shown:] if shown else "…"


# What each request is counted against. CSFloat keeps separate counters per
# key for these, with very different windows: the listings endpoint allows 200
# an hour, the order book 20 a minute. One counter for both - overwritten by
# whichever answered last - read a key as spent after five book requests and
# as fresh again after the next listing.
KINDS = ("listings", "book", "other")


def kind_of(url: str) -> str:
    path = url.split("?", 1)[0]
    if path.endswith("/buy-orders"):
        return "book"
    if "/api/v1/listings" in path:
        return "listings"
    return "other"


def reserve_for(limit: int | None) -> int:
    """Requests to leave unspent: five percent, at least one.

    A flat fifteen was sized for a daily 500 and stopped the order book - 20 a
    minute - after its fifth request.
    """
    return max(1, int(limit or 0) // 20)


@dataclass
class Quota:
    """One of a key's counters, as CSFloat last reported it."""
    limit: int | None = None
    remaining: int | None = None
    reset: float | None = None       # epoch seconds

    def wait(self, now: float) -> float:
        """Seconds until this counter allows a request; 0 if it does now."""
        if self.remaining is None or self.reset is None:
            return 0.0
        if self.remaining > reserve_for(self.limit):
            return 0.0
        return max(self.reset - now, 0.0)


def reset_epoch(raw, now: float | None = None) -> float | None:
    """x-ratelimit-reset as an epoch second: CSFloat sends one, but a small
    number can only be a duration."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return value if value > 1_000_000_000 else now + value


def _offset(key: str, span: int) -> int:
    """Where this key's slice of the route list starts.

    Taken from the key's own digest so the binding survives a restart and does
    not depend on the order keys were listed in."""
    if span <= 0:
        return 0
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % span


@dataclass
class KeyState:
    """One API key, its addresses, and its own clock."""
    key: str
    routes: tuple[str, ...] = ()
    last_request: float = 0.0     # monotonic
    requests: int = 0
    failures: int = 0
    # Set when CSFloat refuses this key outright (revoked, or not entitled).
    disabled_reason: str | None = None
    disabled_at: float | None = None   # epoch
    # A 429 belongs to whoever drew it. Held globally, one refusal stopped
    # every key at once - which is the whole of the throughput a hundred keys
    # were meant to buy. And it belongs to the counter that drew it: a key
    # out of listings for forty minutes can still read order books.
    cooldowns: dict[str, float] = field(default_factory=dict)   # monotonic
    quota: dict[str, Quota] = field(default_factory=dict)
    rate_limits: int = 0
    last_error: str = ""

    @property
    def name(self) -> str:
        return fingerprint(self.key)

    @property
    def tail(self) -> str:
        return tail(self.key)

    @property
    def cooldown_until(self) -> float:
        return max(self.cooldowns.values(), default=0.0)

    def ready_at(self, spacing: float, kind: str = "other") -> float:
        """Monotonic time this key may speak again, for this kind of request."""
        return max(self.last_request + spacing, self.cooldowns.get(kind, 0.0))

    def quota_wait(self, kind: str, now_epoch: float | None = None) -> float:
        q = self.quota.get(kind)
        return q.wait(time.time() if now_epoch is None else now_epoch) if q else 0.0

    def enter_cooldown(self, seconds: float, now: float,
                       kind: str = "other") -> float:
        """Hold this key back, without touching any other."""
        self.rate_limits += 1
        until = max(self.cooldowns.get(kind, 0.0), now + max(0.0, seconds))
        self.cooldowns[kind] = until
        return until - now

    def note_request(self, now: float) -> None:
        self.last_request = now
        self.requests += 1


class KeyRing:
    """The keys, each bound to its own 2-4 addresses.

    Holds no secrets beyond the keys themselves and never logs one: callers
    that report progress use KeyState.name, which is a truncated digest.
    """

    def __init__(self, keys: list[str], pool: ProxyPool,
                 routes_per_key: int = DEFAULT_ROUTES_PER_KEY,
                 spacing: float = 2.5):
        if not keys:
            raise ValueError("a key ring needs at least one key")
        self.pool = pool
        self.spacing = spacing
        self.routes_per_key = max(MIN_ROUTES_PER_KEY,
                                  min(MAX_ROUTES_PER_KEY, routes_per_key))
        self._lock = threading.Lock()
        # Deduplicate: the same key listed twice would get two clocks and
        # quietly halve its own spacing.
        seen: set[str] = set()
        self.keys: list[KeyState] = []
        for key in keys:
            cleaned = key.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                self.keys.append(KeyState(key=cleaned))
        self.bind()

    # -- binding -------------------------------------------------------------

    def bind(self) -> None:
        """Give every key its slice of the pool.

        Called again after the proxy list changes; a key keeps whichever of its
        addresses survived, because the slice is a function of the key and the
        route names rather than of anything accumulated."""
        names = sorted(self.pool.routes)
        with self._lock:
            if not names:
                for state in self.keys:
                    state.routes = ()
                return
            width = min(self.routes_per_key, len(names))
            for state in self.keys:
                start = _offset(state.key, len(names))
                state.routes = tuple(names[(start + i) % len(names)]
                                     for i in range(width))

    def routes_for(self, key: str) -> tuple[str, ...]:
        for state in self.keys:
            if state.key == key:
                return state.routes
        return ()

    # -- leasing -------------------------------------------------------------

    def lease(self, kind: str = "other") -> tuple[KeyState, RouteState] | None:
        """A key that may speak now, with one of its own addresses.

        Returns None when every key is either still inside its spacing, out of
        this kind of request, out of addresses, or disabled — the caller
        decides whether to wait, and wait_seconds() says how long it would be.
        """
        now = time.monotonic()
        epoch = time.time()
        with self._lock:
            ready = [s for s in self.keys
                     if s.disabled_reason is None
                     and s.routes
                     and s.ready_at(self.spacing, kind) <= now
                     and s.quota_wait(kind, epoch) <= 0]
            # Least recently used first: the keys take turns instead of one
            # carrying the sweep while the rest idle.
            ready.sort(key=lambda s: s.last_request)
            for state in ready:
                route = self._route_for(state, now)
                if route is not None:
                    state.note_request(now)
                    return state, route
        return None

    def _route_for(self, state: KeyState, now: float) -> RouteState | None:
        """The healthiest of this key's own addresses, or None if all are spent.

        Deliberately does NOT fall back to the wider pool: borrowing an address
        from another key is what turns a hundred tidy clients back into one
        account speaking from a hundred places.
        """
        candidates = []
        for name in state.routes:
            route = self.pool.routes.get(name)
            if route is None or not route.reachable(now):
                continue
            candidates.append(route)
        if not candidates:
            return None
        # The quota is the key's, not the address's, so an address only has to
        # be up. The least recently used goes, so a key's addresses take turns.
        candidates.sort(key=lambda r: r.last_used)
        chosen = candidates[0]
        chosen.last_used = now
        chosen.note_request()
        return chosen

    def wait_seconds(self, kind: str = "other") -> float:
        """How long until some key could speak again; 0 if one can now.

        A key speaks when its own clock has come round AND one of its own
        addresses is usable. Counting the clock alone said "0, go now" for a
        key whose three addresses were all out of quota or cooling down, and
        the caller gave up at once with "no working key" - over a wait that was
        minutes, not forever.

        Infinite when no live key has a single address in the pool at all:
        that is not a wait, it is a configuration to fix.
        """
        now = time.monotonic()
        epoch = time.time()
        waits = []
        with self._lock:
            for s in self.keys:
                if s.disabled_reason is not None:
                    continue
                routes = [self.pool.routes[n] for n in s.routes
                          if n in self.pool.routes]
                if not routes:
                    continue
                address = min(r.reach_wait(now) for r in routes)
                waits.append(max(s.ready_at(self.spacing, kind) - now,
                                 s.quota_wait(kind, epoch), address, 0.0))
        return min(waits) if waits else float("inf")

    def note_quota(self, key: str, kind: str, limit, remaining, reset) -> None:
        """What CSFloat said about this key's counter for this kind."""
        if limit is None and remaining is None:
            return
        with self._lock:
            state = self._find(key)
            if state is None:
                return
            q = state.quota.setdefault(kind, Quota())
            if limit is not None:
                q.limit = int(limit)
            if remaining is not None:
                q.remaining = int(remaining)
            when = reset_epoch(reset)
            if when is not None:
                q.reset = when

    def _find(self, key: str) -> KeyState | None:
        for state in self.keys:
            if state.key == key:
                return state
        return None

    # -- health --------------------------------------------------------------

    def note_rate_limit(self, key: str, seconds: float,
                        kind: str = "other") -> float:
        """One key drew a 429; the rest keep working."""
        now = time.monotonic()
        with self._lock:
            state = self._find(key)
            if state is not None:
                return state.enter_cooldown(seconds, now, kind)
        return 0.0

    def cooling(self) -> list[KeyState]:
        """Keys currently held back by a refusal of their own."""
        now = time.monotonic()
        with self._lock:
            return [s for s in self.keys if s.cooldown_until > now]

    def disable(self, key: str, reason: str) -> None:
        """Take a key out of rotation: revoked, or refused by CSFloat."""
        with self._lock:
            state = self._find(key)
            if state is not None and state.disabled_reason is None:
                state.disabled_reason = reason
                state.disabled_at = time.time()
                log.error("Key %s taken out of rotation: %s", state.tail, reason)

    def disabled(self) -> list[KeyState]:
        with self._lock:
            return [s for s in self.keys if s.disabled_reason is not None]

    def note_failure(self, key: str, error: str = "") -> None:
        with self._lock:
            state = self._find(key)
            if state is not None:
                state.failures += 1
                if error:
                    state.last_error = error[:200]

    def live(self) -> list[KeyState]:
        with self._lock:
            return [s for s in self.keys if s.disabled_reason is None]

    def concurrency(self) -> int:
        """How many requests may sensibly be in flight at once.

        Bounded by the keys that can actually speak, never by the thread pool
        alone: more workers than keys would simply queue on the spacing.
        """
        return max(1, len(self.live()))

    def snapshot(self) -> list[dict]:
        """Dashboard rows. Carries a fingerprint and the key's last few
        characters - enough to find it in keys.txt - never the key itself."""
        now = time.monotonic()
        epoch = time.time()
        with self._lock:
            out = []
            for s in self.keys:
                quota = {kind: {"limit": q.limit, "remaining": q.remaining,
                                "reset": q.reset}
                         for kind, q in s.quota.items()}
                cooling = {kind: round(until - now)
                           for kind, until in s.cooldowns.items() if until > now}
                out.append({"key": s.name,
                            "tail": s.tail,
                            "routes": list(s.routes),
                            "requests": s.requests,
                            "failures": s.failures,
                            "rate_limits": s.rate_limits,
                            "last_error": s.last_error,
                            "disabled": s.disabled_reason,
                            "disabled_at": s.disabled_at,
                            "quota": quota,
                            "cooling": cooling,
                            "seen_at": epoch})
            return out


def read_keys(path: str | None) -> list[str]:
    """API keys from a file, one per line; blanks and # comments ignored.

    A file rather than the database or the dashboard: the database is exported
    to Telegram for backup and the dashboard is a web page, and a hundred keys
    are a hundred credentials. Nothing here logs a key - callers that report
    progress use `fingerprint`.

    A missing or unreadable file yields nothing rather than raising: running
    on one key is the normal state until the rest arrive, and a typo in a path
    should not stop the collector.
    """
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        log.warning("Could not read the key file %s: %s", path, exc)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("#", 1)[0].strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
