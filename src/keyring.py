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


def fingerprint(key: str) -> str:
    """Name a key in logs and dashboards without disclosing it."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


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

    @property
    def name(self) -> str:
        return fingerprint(self.key)

    def ready_at(self, spacing: float) -> float:
        """Monotonic time this key may speak again."""
        return self.last_request + spacing

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

    def lease(self) -> tuple[KeyState, RouteState] | None:
        """A key that may speak now, with one of its own addresses.

        Returns None when every key is either still inside its spacing, out of
        addresses, or disabled — the caller decides whether to wait, and
        wait_seconds() says how long it would be.
        """
        now = time.monotonic()
        with self._lock:
            ready = [s for s in self.keys
                     if s.disabled_reason is None
                     and s.routes
                     and s.ready_at(self.spacing) <= now]
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
            if route is None or not route.available(self.pool.reserve, now):
                continue
            candidates.append(route)
        if not candidates:
            return None
        # Within a key's own slice, spend the fullest address first: its budget
        # is ours either way, and the least recently used breaks a tie so the
        # key's own addresses take turns too.
        candidates.sort(key=lambda r: (-r.budget_remaining(), r.last_used))
        chosen = candidates[0]
        chosen.last_used = now
        chosen.note_request()
        return chosen

    def wait_seconds(self) -> float:
        """How long until some key could speak again; 0 if one can now."""
        now = time.monotonic()
        with self._lock:
            waits = [max(0.0, s.ready_at(self.spacing) - now)
                     for s in self.keys if s.disabled_reason is None and s.routes]
        if not waits:
            return 0.0
        return min(waits)

    # -- health --------------------------------------------------------------

    def disable(self, key: str, reason: str) -> None:
        """Take a key out of rotation: revoked, or refused by CSFloat."""
        with self._lock:
            for state in self.keys:
                if state.key == key:
                    state.disabled_reason = reason
                    return

    def note_failure(self, key: str) -> None:
        with self._lock:
            for state in self.keys:
                if state.key == key:
                    state.failures += 1
                    return

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
        """Dashboard rows. Carries fingerprints, never the keys themselves."""
        with self._lock:
            return [{"key": s.name,
                     "routes": list(s.routes),
                     "requests": s.requests,
                     "failures": s.failures,
                     "disabled": s.disabled_reason}
                    for s in self.keys]
