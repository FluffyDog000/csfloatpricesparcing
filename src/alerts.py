"""Telegram alerts for collection problems.

Watches the same signals the dashboard shows (auth failures, a stalled
collector, persistent rate limiting) and pings the configured chat so a
silent outage doesn't go unnoticed for hours. Each condition is rate-limited
to one message per ALERT_REPEAT_SECONDS, and a recovery message is sent once
the problem clears.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow_iso
from .pacing import parse_iso
from .telegram import TelegramClient

log = logging.getLogger("csfloat.alerts")

CHECK_EVERY_SECONDS = 300.0      # evaluate at most every 5 minutes
ALERT_REPEAT_SECONDS = 3600.0    # don't repeat the same alert within an hour
DEFAULT_STALE_MINUTES = 90.0     # no successful poll for this long -> alert


class AlertService:
    def __init__(self, config, db: Database):
        self.config = config
        self.db = db
        self.tg = TelegramClient(config.telegram)
        self._last_check = 0.0

    # -- helpers -------------------------------------------------------------

    def enabled(self) -> bool:
        if not self.tg.configured():
            return False
        return (self.db.get_setting("alerts_enabled", "1") or "1") != "0"

    def _stale_limit(self) -> float:
        raw = self.db.get_setting("alert_stale_minutes")
        try:
            return float(raw) if raw else DEFAULT_STALE_MINUTES
        except (TypeError, ValueError):
            return DEFAULT_STALE_MINUTES

    def _should_send(self, key: str) -> bool:
        """True when this alert hasn't been sent recently."""
        last = parse_iso(self.db.get_setting(f"alert_sent_{key}"))
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() >= ALERT_REPEAT_SECONDS

    def _mark_sent(self, key: str) -> None:
        self.db.set_setting(f"alert_sent_{key}", utcnow_iso())

    def _clear(self, key: str) -> None:
        self.db.set_setting(f"alert_sent_{key}", "")

    def _fire(self, key: str, text: str) -> None:
        if not self._should_send(key):
            return
        if self.tg.send_message(text):
            self._mark_sent(key)
            log.warning("Alert sent (%s): %s", key, text)

    # -- checks --------------------------------------------------------------

    def evaluate(self) -> None:
        now = datetime.now(timezone.utc)
        hour_ago = (now - timedelta(hours=1)).replace(microsecond=0).isoformat()
        stats = self.db.poll_stats(hour_ago)

        # 1) Expired cookie — nothing will be collected until it's refreshed.
        if stats["auth_error"] > 0 and stats["ok"] == 0:
            self._fire("auth",
                       "🔑 CSFloat трекер: ошибка авторизации — cookie протухла.\n"
                       f"За час: {stats['auth_error']} ошибок, успешных сборов нет.\n"
                       "Обнови CSFLOAT_COOKIE в .env и перезапусти сборщик.")
        else:
            self._clear("auth")

        # 2) Collector stalled (no successful poll for a long time).
        last_ok = parse_iso(self.db.last_successful_poll())
        limit = self._stale_limit()
        if last_ok is None:
            stale_min = None
        else:
            stale_min = (now - last_ok).total_seconds() / 60.0
        if stale_min is not None and stale_min > limit:
            self._fire("stale",
                       f"⚠️ CSFloat трекер: нет успешного сбора {stale_min:.0f} мин "
                       f"(порог {limit:.0f}).\nПроверь логи сборщика.")
        else:
            self._clear("stale")

        # 3) Rate limited with nothing getting through.
        if stats["rate_limited"] > 0 and stats["ok"] == 0:
            self._fire("limited",
                       f"⛔ CSFloat трекер: лимит 429 — за час {stats['rate_limited']} "
                       "отказов и ни одного успешного опроса.\n"
                       "Бот сам замедляется; при желании увеличь интервалы "
                       "на вкладке «Нагрузка».")
        else:
            self._clear("limited")

    def tick(self, monotonic_now: float) -> None:
        """Called from the collector loop; throttles its own frequency."""
        if monotonic_now - self._last_check < CHECK_EVERY_SECONDS:
            return
        self._last_check = monotonic_now
        if not self.enabled():
            return
        try:
            self.evaluate()
        except Exception as exc:  # noqa: BLE001 - alerts must never break polling
            log.warning("Alert check failed: %s", exc)
