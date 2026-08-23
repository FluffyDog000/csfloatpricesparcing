"""The collector: for each tracked item, fetch its latest sales, store new
ones (deduplicated), detect possible gaps, and reschedule the next poll with
randomized jitter so requests are spread out over time.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AppConfig, ItemConfig, load_items
from .csfloat_client import AuthError, CSFloatClient, RateLimited
from .db import Database, utcnow_iso
from .images import ImageService
from .proxies import ROTATING_DEFAULT_LIMIT, parse_proxy_list
from .pacing import (
    ADAPTIVE_MAX_MINUTES,
    PACE_MAX,
    PACE_RECOVER_SECONDS,
    PACE_UP_FACTOR,
    adaptive_minutes,
    window_start,
)
from .parser import extract_icon_hash, extract_records, parse_sales

log = logging.getLogger("csfloat.collector")

# Keep this many requests of the quota untouched as a safety margin, and never
# stretch intervals by more than this factor when the quota is tight.
QUOTA_RESERVE = 15
QUOTA_FACTOR_MAX = 60.0
MAX_QUOTA_PAUSE_SECONDS = 3600.0   # re-check at least hourly while waiting



class Collector:
    def __init__(self, config: AppConfig, db: Database, client: CSFloatClient):
        self.config = config
        self.db = db
        self.client = client
        self._dumped: set[str] = set()
        # Resolve item images here (spaced via the client) so the dashboard only
        # reads cached URLs and never bursts the official API into a 429.
        self.images = ImageService(config, db, client=client)

    def fetch_one_missing_image(self) -> bool:
        """Resolve at most ONE item image that still has no cached URL. Called
        as a slow background trickle so the official listings API is never
        bursted. Returns True if an item was attempted."""
        if not self.config.http.api_key:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        names = self.db.items_needing_icon(cutoff, limit=1)
        if not names:
            return False
        try:
            self.images.get_or_fetch(names[0])
        except Exception as exc:  # noqa: BLE001
            log.debug("image trickle skipped for '%s': %s", names[0], exc)
        return True

    def reopen_db(self) -> None:
        """Reopen the DB connection (after the file was swapped by a restore)."""
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass
        self.db = Database(self.config.db_path)
        log.info("Collector DB connection reopened.")

    # -- item registry (source of truth = DB) --------------------------------

    def seed_from_yaml_if_empty(self) -> int:
        """One-time import of items.yaml into the DB when the items table is
        empty (first run on a fresh DB). After that the DB is authoritative and
        items.yaml is ignored — items are managed via the web UI. Returns the
        number of items seeded."""
        if self.db.items_count() > 0:
            return 0
        try:
            items = load_items()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read items.yaml for seeding: %s", exc)
            return 0
        for it in items:
            self.db.upsert_item(
                it.name,
                active=it.active,
                pattern_sensitive=it.pattern_sensitive,
                interval_min_minutes=it.interval_min_minutes,
                interval_max_minutes=it.interval_max_minutes,
            )
        if items:
            log.info("Seeded %d item(s) from items.yaml into the database.", len(items))
        return len(items)

    def active_items(self) -> dict[str, ItemConfig]:
        """Current active items, read live from the DB (so web edits apply)."""
        out: dict[str, ItemConfig] = {}
        for r in self.db.get_active_items():
            out[r["market_hash_name"]] = ItemConfig(
                name=r["market_hash_name"],
                active=True,
                interval_min_minutes=r.get("interval_min_minutes"),
                interval_max_minutes=r.get("interval_max_minutes"),
                pattern_sensitive=bool(r.get("pattern_sensitive", 1)),
            )
        return out

    def runtime_polling(self) -> tuple[float, float, float]:
        """(min_minutes, max_minutes, seconds_between_requests) in effect now.

        Values saved from the dashboard (settings table) win over config.yaml,
        so the pace can be tuned live without restarting the collector."""
        p = self.config.polling

        def val(key: str, default: float) -> float:
            raw = self.db.get_setting(key)
            if raw in (None, ""):
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        lo = val("poll_interval_min_minutes", p.interval_min_minutes)
        hi = val("poll_interval_max_minutes", p.interval_max_minutes)
        spacing = val("min_seconds_between_requests", p.min_seconds_between_requests)
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi, spacing

    def apply_runtime_settings(self) -> None:
        """Push the live request spacing into the HTTP client."""
        self.client.polling.min_seconds_between_requests = self.runtime_polling()[2]

    # -- adaptive pacing -----------------------------------------------------

    def pace_multiplier(self) -> float:
        """Global slowdown factor learned from 429s (1.0 = configured pace)."""
        raw = self.db.get_setting("pace_multiplier")
        try:
            return min(max(float(raw), 1.0), PACE_MAX) if raw else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _set_pace_multiplier(self, value: float) -> None:
        self.db.set_setting("pace_multiplier", f"{min(max(value, 1.0), PACE_MAX):.3f}")

    def slow_down(self) -> None:
        """Called on a 429: back off the whole schedule (multiplicative)."""
        before = self.pace_multiplier()
        after = min(before * PACE_UP_FACTOR, PACE_MAX)
        if after > before:
            self._set_pace_multiplier(after)
            log.warning("Rate limit hit — slowing the schedule x%.2f (was x%.2f)",
                        after, before)
        self.db.set_setting("pace_last_429", utcnow_iso())

    def _clean_seconds(self, key: str) -> float | None:
        """Seconds since the timestamp in `key`, or None when it isn't set."""
        raw = self.db.get_setting(key)
        if not raw:
            return None
        try:
            t = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()

    def maybe_speed_up(self) -> None:
        """After a clean stretch with no 429, edge the pace back up.

        Recovery is driven by the CLOCK, not by how often we poll. The pace
        multiplier is what makes polls rare in the first place, so tying its
        decay to successful polls means a x8 backoff keeps itself alive: at one
        poll per 16 hours it would take days to unwind. Each clean hour undoes
        exactly one slow_down step, so the schedule recovers as fast as it
        backed off."""
        mult = self.pace_multiplier()
        if mult <= 1.0:
            return
        since_429 = self._clean_seconds("pace_last_429")
        if since_429 is not None and since_429 < PACE_RECOVER_SECONDS:
            return
        since_step = self._clean_seconds("pace_last_step")
        if since_step is not None and since_step < PACE_RECOVER_SECONDS:
            return
        # Symmetric with slow_down (x1.5 up): one step down per clean hour.
        new_mult = max(mult / PACE_UP_FACTOR, 1.0)
        self._set_pace_multiplier(new_mult)
        self.db.set_setting("pace_last_step", utcnow_iso())
        log.info("No rate limits for a while — speeding up to x%.2f (was x%.2f)",
                 new_mult, mult)

    def adaptive_enabled(self) -> bool:
        return (self.db.get_setting("adaptive_intervals", "1") or "1") != "0"

    def adaptive_ceiling(self) -> float:
        raw = self.db.get_setting("adaptive_max_minutes")
        try:
            return float(raw) if raw else ADAPTIVE_MAX_MINUTES
        except (TypeError, ValueError):
            return ADAPTIVE_MAX_MINUTES

    def adaptive_interval_minutes(self, item_id: int, floor_min: float) -> float | None:
        """Interval sized to this item's own sale rate (None if too little
        history — the caller then uses the plain configured interval)."""
        stats = self.db.sales_window(item_id, window_start())
        return adaptive_minutes(
            int(stats.get("c") or 0), stats.get("first_sold"),
            floor_minutes=floor_min, ceiling_minutes=self.adaptive_ceiling(),
        )

    def stretch_factor(self) -> float:
        """How much to stretch every interval right now.

        The 429 backoff and the quota budget describe the SAME wall from two
        sides, so multiplying them double-counts: ×8 after 429s times ×8 for a
        spent quota is ×64, which pushes a 2h interval past five days and stops
        collection entirely. Back off by whichever signal is tighter instead."""
        return max(self.pace_multiplier(), self.cached_budget_factor())

    def interval_for(self, item: ItemConfig, item_id: int | None = None) -> float:
        """Seconds until this item's next poll.

        Per-item overrides win; otherwise the interval adapts to the item's own
        sale rate (when enabled), and the global pace multiplier — learned from
        429s — stretches everything."""
        gmin, gmax, _ = self.runtime_polling()
        mult = self.stretch_factor()

        if item.interval_min_minutes or item.interval_max_minutes:
            lo = item.interval_min_minutes or gmin
            hi = item.interval_max_minutes or gmax
            if hi < lo:
                lo, hi = hi, lo
            return random.uniform(lo, hi) * 60.0 * mult

        if self.adaptive_enabled():
            if item_id is None:
                item_id = self.db.get_item_id(item.name)
            adaptive = (self.adaptive_interval_minutes(item_id, gmin)
                        if item_id is not None else None)
            if adaptive is not None:
                jitter = random.uniform(0.9, 1.1)   # avoid items syncing up
                return adaptive * 60.0 * jitter * mult

        return random.uniform(gmin, gmax) * 60.0 * mult

    # -- cooldown state shared with the dashboard ----------------------------

    def _store_cooldown(self) -> None:
        """Persist the client's global 429 pause so the web UI can display it."""
        remaining = self.client.cooldown_remaining()
        if remaining <= 0:
            return
        until = (datetime.now(timezone.utc) + timedelta(seconds=remaining))
        self.db.set_setting("cooldown_until", until.replace(microsecond=0).isoformat())
        self.db.set_setting("cooldown_consecutive",
                            str(getattr(self.client, "_consecutive_429", 0)))

    # -- proxies (DB is the source of truth, .env seeds it once) -------------

    def seed_proxies_from_env(self) -> None:
        """First run: copy CSFLOAT_PROXIES from .env into the DB, after which
        the dashboard is the place to manage them."""
        if self.db.get_setting("proxies") is not None:
            return
        env_list = list(self.config.http.proxies)
        self.db.set_setting("proxies", "\n".join(env_list))
        self.db.set_setting("use_direct", "1" if self.config.http.use_direct else "0")
        if env_list:
            log.info("Seeded %d proxy/proxies from .env into the database.",
                     len(env_list))

    def rotating_limit(self) -> int:
        """Requests per 24h allowed through a rotating proxy. Deliberately a
        local cap: a rotating endpoint reports someone else's quota headers."""
        raw = self.db.get_setting("rotating_daily_limit")
        try:
            return max(int(raw), 1) if raw else ROTATING_DEFAULT_LIMIT
        except (TypeError, ValueError):
            return ROTATING_DEFAULT_LIMIT

    def sync_proxies(self) -> bool:
        """Apply the proxy list saved in the DB to the live pool. Returns True
        when the pool changed (called periodically, so web edits apply live)."""
        raw = self.db.get_setting("proxies")
        if raw is None:
            return False
        urls = parse_proxy_list(raw)
        use_direct = (self.db.get_setting("use_direct", "1") or "1") != "0"
        if not urls and not use_direct:
            use_direct = True          # never leave the pool empty
        changed = self.client.pool.replace(urls, use_direct=use_direct,
                                           rotating_limit=self.rotating_limit())
        if changed:
            self.restore_rotating_usage()
        return changed

    def restore_rotating_usage(self) -> None:
        """Re-apply the spent local budget of rotating routes, so a restart (or
        a list edit) doesn't hand them a fresh window they already used up."""
        raw = self.db.get_setting("rotating_usage")
        if not raw:
            return
        try:
            self.client.pool.restore_usage(json.loads(raw))
        except (TypeError, ValueError):
            log.warning("Could not read stored rotating-proxy usage; ignoring.")

    def store_rotating_usage(self) -> None:
        pool = getattr(self.client, "pool", None)
        if pool is None or not pool.has_rotating():
            return
        self.db.set_setting("rotating_usage", json.dumps(pool.usage_snapshot()))

    # -- API quota (x-ratelimit-*) -------------------------------------------

    def store_rate_state(self) -> None:
        """Persist the latest quota snapshot so the dashboard can show it."""
        pool = getattr(self.client, "pool", None)
        if pool is not None:
            self.db.set_setting("proxy_state", pool.to_json())
            self.store_rotating_usage()
            blocked = getattr(self.client, "account_ip_block_at", None)
            if blocked:
                self.db.set_setting("account_ip_block_at", blocked)
            total = pool.total_remaining()
            if total is not None and len(pool.routes) > 1:
                # With several routes the usable budget is their sum.
                self.db.set_setting("rl_remaining", str(total))
                reset = pool.earliest_reset()
                if reset:
                    self.db.set_setting("rl_reset", str(reset))
                self.db.set_setting("rl_seen_at", utcnow_iso())
                return
        st = getattr(self.client, "rate_state", None)
        if not st:
            return
        for key in ("limit", "remaining", "reset"):
            value = st.get(key)
            self.db.set_setting(f"rl_{key}", "" if value is None else str(value))
        self.db.set_setting("rl_seen_at", utcnow_iso())

    def quota(self) -> tuple[int | None, int | None, int | None]:
        """(limit, remaining, reset_epoch) as last reported by CSFloat."""
        def num(key: str) -> int | None:
            raw = self.db.get_setting(key)
            try:
                return int(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return None
        return num("rl_limit"), num("rl_remaining"), num("rl_reset")

    def quota_pause_seconds(self) -> float:
        """Seconds to hold off polling because the quota is (nearly) spent."""
        _, remaining, reset = self.quota()
        if remaining is None or reset is None:
            return 0.0
        if remaining > QUOTA_RESERVE:
            return 0.0
        left = reset - time.time()
        return max(0.0, min(left, MAX_QUOTA_PAUSE_SECONDS))

    def budget_factor(self, planned_per_second: float) -> float:
        """How much to stretch every interval so the planned request rate fits
        the quota that's actually left before the next reset (>= 1.0)."""
        limit, remaining, reset = self.quota()
        if remaining is None or reset is None or planned_per_second <= 0:
            return 1.0
        seconds_left = max(reset - time.time(), 1.0)
        usable = max(remaining - QUOTA_RESERVE, 0)
        if usable <= 0:
            return PACE_MAX
        allowed_per_second = usable / seconds_left
        if planned_per_second <= allowed_per_second:
            return 1.0
        return min(planned_per_second / allowed_per_second, QUOTA_FACTOR_MAX)

    def planned_rate_per_second(self) -> float:
        """Current scheduled request rate across all active items."""
        gmin, gmax, _ = self.runtime_polling()
        total = 0.0
        for row in self.db.get_active_items():
            lo = row.get("interval_min_minutes")
            hi = row.get("interval_max_minutes")
            if lo or hi:
                minutes = ((lo or gmin) + (hi or gmax)) / 2.0
            else:
                minutes = (gmin + gmax) / 2.0
                if self.adaptive_enabled():
                    adapt = self.adaptive_interval_minutes(int(row["id"]), gmin)
                    if adapt is not None:
                        minutes = adapt
            total += 1.0 / max(minutes * 60.0, 1.0)
        return total

    def refresh_budget_factor(self) -> float:
        """Recompute and cache the quota stretch factor (called periodically)."""
        factor = self.budget_factor(self.planned_rate_per_second())
        self.db.set_setting("quota_factor", f"{factor:.3f}")
        return factor

    def cached_budget_factor(self) -> float:
        raw = self.db.get_setting("quota_factor")
        try:
            return min(max(float(raw), 1.0), QUOTA_FACTOR_MAX) if raw else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _store_429_headers(self) -> None:
        """Remember what CSFloat told us about its limit (shown on the dashboard)."""
        headers = getattr(self.client, "last_429_headers", None)
        if headers:
            self.db.set_setting("last_429_headers", json.dumps(headers, ensure_ascii=False))
        body = getattr(self.client, "last_429_body", "")
        if body:
            self.db.set_setting("last_429_body", body)
        self.db.set_setting("last_429_at", utcnow_iso())

    def restore_cooldown_from_db(self) -> float:
        """On startup, re-arm a cooldown that was still running before the
        restart — otherwise we immediately burst back into the rate limit."""
        raw = self.db.get_setting("cooldown_until")
        if not raw:
            return 0.0
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return 0.0
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            self._clear_cooldown()
            return 0.0
        try:
            consecutive = int(self.db.get_setting("cooldown_consecutive") or 0)
        except ValueError:
            consecutive = 0
        self.client.restore_cooldown(remaining, consecutive)
        log.warning("Restored 429 cooldown from previous run: %.1f min left",
                    remaining / 60.0)
        return remaining

    def _clear_cooldown(self) -> None:
        if self.db.get_setting("cooldown_until"):
            self.db.set_setting("cooldown_until", "")
            self.db.set_setting("cooldown_consecutive", "0")

    # -- raw dump for field verification ------------------------------------

    def _maybe_dump_raw(self, name: str, payload: object) -> None:
        """Dump the first raw response per item so field mappings can be
        verified against real data."""
        if name in self._dumped:
            return
        self._dumped.add(name)
        try:
            self.config.raw_dump_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in name)[:80]
            path = self.config.raw_dump_dir / f"{safe}.json"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            log.info("Wrote raw sample response for '%s' -> %s", name, path)
        except OSError as exc:
            log.warning("Could not write raw dump for '%s': %s", name, exc)

    # -- one poll ------------------------------------------------------------

    def poll_item(self, item: ItemConfig) -> None:
        name = item.name
        # DB is the source of truth: the item row already exists. Look it up
        # (don't upsert — that would clobber folder/interval fields). If it was
        # deleted meanwhile, create it so the poll still records.
        item_id = self.db.get_item_id(name)
        if item_id is None:
            item_id = self.db.upsert_item(
                name, active=item.active, pattern_sensitive=item.pattern_sensitive
            )
        had_before = self.db.item_sales_count(item_id) > 0

        self.apply_runtime_settings()
        try:
            payload = self.client.fetch_latest_sales(name)
            self._clear_cooldown()
            self.maybe_speed_up()
            self.store_rate_state()
        except AuthError as exc:
            log.error("AUTH ERROR for '%s': %s", name, exc)
            self.db.log_poll(
                item_id=item_id, market_hash_name=name, fetched_count=0,
                new_count=0, overlap_count=0, status="auth_error", note=str(exc),
            )
            return
        except RateLimited as exc:
            log.error("RATE LIMITED for '%s': %s", name, exc)
            self._store_cooldown()
            self.slow_down()
            self._store_429_headers()
            self.store_rate_state()
            self.db.log_poll(
                item_id=item_id, market_hash_name=name, fetched_count=0,
                new_count=0, overlap_count=0, status="rate_limited", note=str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 - log & continue, never crash loop
            log.exception("ERROR polling '%s': %s", name, exc)
            self.db.log_poll(
                item_id=item_id, market_hash_name=name, fetched_count=0,
                new_count=0, overlap_count=0, status="error", note=str(exc),
            )
            return

        self._maybe_dump_raw(name, payload)

        now = datetime.now(timezone.utc)
        scraped_at = utcnow_iso()
        sales = parse_sales(
            payload, item_id=item_id, fallback_name=name,
            scraped_at_iso=scraped_at, now=now,
        )
        fetched = len(sales)

        if fetched == 0:
            n_records = len(extract_records(payload))
            note = (
                "0 sales parsed from a non-empty response — the field mapping in "
                "parser.py may not match this response. Check the raw dump."
                if n_records else "endpoint returned no sales"
            )
            log.warning("'%s': %s (raw records=%d)", name, note, n_records)
            self.db.log_poll(
                item_id=item_id, market_hash_name=name, fetched_count=n_records,
                new_count=0, overlap_count=0, status="ok", note=note,
                response_bytes=self.client.last_response_bytes,
            )
            self.db.set_last_polled(item_id)
            return

        # Deduplication accounting.
        fetched_ids = [s.sale_id for s in sales]
        already = self.db.existing_sale_ids(fetched_ids)
        overlap = len(already)
        inserted = self.db.insert_sales(sales)

        # Gap detection: if we already had history but almost none of this
        # window overlaps, the 40-sale window may have rolled past.
        note = ""
        if had_before and overlap < self.config.polling.gap_warning_min_overlap:
            note = (
                f"POSSIBLE MISSED SALES for '{name}': only {overlap} of "
                f"{fetched} fetched sales were already known — the 40-sale "
                f"window may have rolled past between polls. Increase this "
                f"item's poll frequency."
            )
            log.warning(note)

        log.info(
            "'%s': fetched=%d new=%d overlap=%d", name, fetched, inserted, overlap
        )
        self.db.log_poll(
            item_id=item_id, market_hash_name=name, fetched_count=fetched,
            new_count=inserted, overlap_count=overlap, status="ok", note=note,
            response_bytes=self.client.last_response_bytes,
        )
        self.db.set_last_polled(item_id)

        # Item image comes free with the sales data (item.icon_url) — no need
        # for the rate-limited official listings API. Cache it if changed.
        try:
            icon_hash = extract_icon_hash(payload)
            if icon_hash:
                url = self.images._build_url(icon_hash)
                cached = self.db.get_icon(name)
                if not cached or cached.get("icon_url") != url:
                    self.db.set_icon(name, url)
        except Exception as exc:  # noqa: BLE001
            log.debug("icon extract skipped for '%s': %s", name, exc)
