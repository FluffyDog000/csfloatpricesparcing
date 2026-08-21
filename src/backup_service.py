"""Daily DB export to Telegram + inbound restore, driven by the collector loop.

Reuses the collector's scheduler: `tick()` is called on each loop iteration and
internally throttles two jobs:
  * export: once per day at the MSK time configured in the web settings page
    (retries every 10 min on failure);
  * restore: polls Telegram for a document sent from the authorized chat_id and
    swaps it in as the new database.

All timing is anchored to Europe/Moscow regardless of the server timezone.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # noqa: BLE001 - missing tzdata; fall back to fixed +3
    MSK = timezone(timedelta(hours=3))

from .backup import prune_backups, restore, snapshot_db
from .config import AppConfig
from .telegram import TelegramClient

log = logging.getLogger("csfloat.backup.svc")

# Setting keys
S_TIME = "export_time_msk"       # "HH:MM" or "" (disabled)
S_ENABLED = "export_enabled"     # "1"/"0"
S_LAST = "last_export_date_msk"  # "YYYY-MM-DD"
S_RETRY = "export_retry_at"      # ISO UTC
S_OFFSET = "tg_update_offset"    # int as str

POLL_EVERY = 20.0     # seconds between Telegram polls
SCHED_EVERY = 30.0    # seconds between export-schedule checks
RETRY_DELAY = timedelta(minutes=10)


def now_msk() -> datetime:
    return datetime.now(MSK)


def export_db(config: AppConfig, reason: str = "manual") -> bool:
    """Snapshot the DB and send it to Telegram as a document. Standalone so both
    the collector's scheduler and the web "Export now" button can call it."""
    tg = TelegramClient(config.telegram)
    if not tg.configured():
        log.warning("Export requested (%s) but Telegram is not configured.", reason)
        return False
    try:
        ts = now_msk().strftime("%Y%m%d-%H%M")
        snap = config.backups_dir / f"export-{ts}.db"
        snapshot_db(config.db_path, snap)
    except Exception as exc:  # noqa: BLE001
        log.exception("Snapshot for export failed: %s", exc)
        return False
    caption = f"CSFloat tracker DB — {now_msk().strftime('%Y-%m-%d %H:%M МСК')} ({reason})"
    ok = tg.send_document(snap, caption=caption)
    prune_backups(config.backups_dir)
    log.info("DB export to Telegram %s (%s).", "succeeded" if ok else "FAILED", reason)
    return ok


class BackupService:
    def __init__(self, config: AppConfig, collector):
        self.config = config
        self.collector = collector          # gives access to collector.db (reopenable)
        self.tg = TelegramClient(config.telegram)
        self._last_poll = 0.0
        self._last_sched = 0.0

    @property
    def db(self):
        return self.collector.db

    # -- manual / scheduled export ------------------------------------------

    def export_now(self, reason: str = "manual") -> bool:
        return export_db(self.config, reason)

    def _check_schedule(self) -> None:
        enabled = (self.db.get_setting(S_ENABLED, "0") == "1")
        target = self.db.get_setting(S_TIME, "")
        if not enabled or not target:
            return
        try:
            hh, mm = [int(x) for x in target.split(":")]
        except (ValueError, AttributeError):
            return

        nm = now_msk()
        today = nm.date().isoformat()
        if self.db.get_setting(S_LAST) == today:
            return  # already exported today

        due = (nm.hour, nm.minute) >= (hh, mm)
        retry_at = self.db.get_setting(S_RETRY)
        if retry_at:
            try:
                due = datetime.now(timezone.utc) >= datetime.fromisoformat(retry_at)
            except ValueError:
                due = True
        if not due:
            return

        ok = self.export_now("scheduled")
        if ok:
            self.db.set_setting(S_LAST, today)
            self.db.set_setting(S_RETRY, None)
        else:
            retry = (datetime.now(timezone.utc) + RETRY_DELAY).isoformat()
            self.db.set_setting(S_RETRY, retry)
            log.warning("Scheduled export failed; will retry after %s", retry)

    # -- inbound restore via Telegram ---------------------------------------

    def _do_restore(self, new_file: Path) -> dict:
        """Swap in a new DB file, reopening the collector's connection."""
        self.collector.db.close()
        try:
            info = restore(self.config.db_path, new_file, self.config.backups_dir)
        finally:
            self.collector.reopen_db()
        return info

    def _poll_telegram(self) -> None:
        if not self.tg.configured():
            return
        try:
            offset = int(self.db.get_setting(S_OFFSET, "0") or "0")
        except ValueError:
            offset = 0
        updates = self.tg.get_updates(offset=offset or None)
        auth_chat = str(self.config.telegram.chat_id)

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            doc = msg.get("document")
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not doc:
                continue
            if chat_id != auth_chat:
                # Someone who knows the bot's username sent a file — ignore it.
                log.warning("Ignoring document from unauthorized chat %s", chat_id)
                continue
            fname = doc.get("file_name", "restore.db")
            log.info("Received DB document '%s' from authorized chat; restoring.", fname)
            self.config.backups_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.config.backups_dir / f"incoming-{now_msk().strftime('%Y%m%d-%H%M%S')}.db"
            if not self.tg.download_file(doc["file_id"], tmp):
                self.tg.send_message("Не удалось скачать файл из Telegram.")
                continue
            try:
                self._do_restore(tmp)
                when = now_msk().strftime("%Y-%m-%d %H:%M МСК")
                self.tg.send_message(f"База восстановлена из «{fname}» ({when}).")
            except ValueError as exc:
                tmp.unlink(missing_ok=True)
                self.tg.send_message(f"Восстановление отклонено: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Restore failed: %s", exc)
                self.tg.send_message(f"Ошибка восстановления: {exc}")

        self.db.set_setting(S_OFFSET, str(offset))

    # -- called from the collector loop -------------------------------------

    def tick(self) -> None:
        if not self.tg.configured():
            return
        mono = time.monotonic()
        if mono - self._last_poll >= POLL_EVERY:
            self._last_poll = mono
            try:
                self._poll_telegram()
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram poll error: %s", exc)
        if mono - self._last_sched >= SCHED_EVERY:
            self._last_sched = mono
            try:
                self._check_schedule()
            except Exception as exc:  # noqa: BLE001
                log.warning("Export schedule check error: %s", exc)
