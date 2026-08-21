"""The collector: for each tracked item, fetch its latest sales, store new
ones (deduplicated), detect possible gaps, and reschedule the next poll with
randomized jitter so requests are spread out over time.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, ItemConfig, load_items
from .csfloat_client import AuthError, CSFloatClient, RateLimited
from .db import Database, utcnow_iso
from .parser import extract_records, parse_sales

log = logging.getLogger("csfloat.collector")


class Collector:
    def __init__(self, config: AppConfig, db: Database, client: CSFloatClient):
        self.config = config
        self.db = db
        self.client = client
        self._dumped: set[str] = set()

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

    def interval_for(self, item: ItemConfig) -> float:
        """Random poll interval in seconds for this item (jitter)."""
        lo = item.interval_min_minutes or self.config.polling.interval_min_minutes
        hi = item.interval_max_minutes or self.config.polling.interval_max_minutes
        if hi < lo:
            lo, hi = hi, lo
        return random.uniform(lo, hi) * 60.0

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

        try:
            payload = self.client.fetch_latest_sales(name)
        except AuthError as exc:
            log.error("AUTH ERROR for '%s': %s", name, exc)
            self.db.log_poll(
                item_id=item_id, market_hash_name=name, fetched_count=0,
                new_count=0, overlap_count=0, status="auth_error", note=str(exc),
            )
            return
        except RateLimited as exc:
            log.error("RATE LIMITED for '%s': %s", name, exc)
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
        )
        self.db.set_last_polled(item_id)
