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

    # -- item registry -------------------------------------------------------

    def sync_items(self, items: list[ItemConfig]) -> dict[str, ItemConfig]:
        """Register items from items.yaml into the DB and deactivate any that
        were removed. Returns a name -> ItemConfig map of active items."""
        active_names = [i.name for i in items if i.active]
        for it in items:
            self.db.upsert_item(it.name, active=it.active)
        self.db.deactivate_missing_items(active_names)
        return {i.name: i for i in items if i.active}

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
        item_id = self.db.upsert_item(name, active=item.active)
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
