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
CLOUD_DEFAULT_TIME = "04:00"     # МСК, when only the cloud is configured


def now_msk() -> datetime:
    return datetime.now(MSK)


# Telegram refuses documents over 50 MB; a little under, for the multipart
# overhead.
TG_MAX_BYTES = 49 * 1024 * 1024
LOCAL_PACKED_KEEP = 5


def export_db(config: AppConfig, reason: str = "manual") -> bool:
    """Snapshot the DB, pack it, and send it wherever backups go: the cloud
    (CSFLOAT_CLOUD_REMOTE, see src/cloud.py) and Telegram, if it fits.

    The raw database went to Telegram until it passed 50 MB, and from then on
    every export failed with 413 and nothing was backed up anywhere. Packed it
    is several times smaller; the cloud takes it whatever its size.
    Standalone so both the scheduler and the web "Export now" button call it.
    Returns True when at least one destination took the copy.
    """
    from . import cloud

    tg = TelegramClient(config.telegram)
    to_tg, to_cloud = tg.configured(), cloud.configured()
    if not (to_tg or to_cloud):
        log.warning("Export requested (%s) but neither Telegram nor the cloud "
                    "is configured.", reason)
        return False
    stamp = now_msk().strftime("%Y-%m-%d_%H-%M")
    try:
        snap = config.backups_dir / f"export-{stamp}.db"
        snapshot_db(config.db_path, snap)
        packed = cloud.gzip_file(
            snap, config.backups_dir / f"{cloud.PREFIX}{stamp}{cloud.SUFFIX}")
        raw = snap.stat().st_size
        snap.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("Snapshot for export failed: %s", exc)
        if to_tg:
            tg.send_message(f"⚠️ Бэкап базы не сделан: {exc}")
        return False
    size = packed.stat().st_size
    done, trouble = [], []

    if to_cloud:
        ok, what = cloud.upload(packed)
        if ok:
            gone = cloud.prune()
            done.append(f"облако ({what}" + (f", удалено старых: {gone}" if gone else "")
                        + ")")
        else:
            trouble.append(f"облако: {what}")

    if to_tg:
        if size <= TG_MAX_BYTES:
            caption = (f"CSFloat tracker DB — {now_msk().strftime('%Y-%m-%d %H:%M МСК')}"
                       f" ({reason}), {cloud.human(size)} сжато")
            if tg.send_document(packed, caption=caption):
                done.append("Telegram")
            else:
                trouble.append("Telegram не принял файл")
        else:
            trouble.append(f"в Telegram не влезает: {cloud.human(size)} даже сжатой")
        # The file itself says it arrived; the rest is said in words, so a
        # backup that went only to the cloud - or nowhere - is not silent.
        if trouble or "Telegram" not in done:
            tg.send_message(
                ("✅ Бэкап базы: " + ", ".join(done) if done else "⚠️ Бэкап базы НЕ сохранён")
                + f" · {cloud.human(raw)} → {cloud.human(size)}"
                + ("\n" + "\n".join(trouble) if trouble else ""))

    _prune_packed(config.backups_dir)
    prune_backups(config.backups_dir)
    log.info("DB export (%s): %s%s", reason, ", ".join(done) or "nowhere",
             (" — " + "; ".join(trouble)) if trouble else "")
    return bool(done)


def _prune_packed(backups_dir: Path, keep: int = LOCAL_PACKED_KEEP) -> None:
    """The packed copies kept on the server itself: the last few."""
    from . import cloud
    try:
        files = sorted(backups_dir.glob(f"{cloud.PREFIX}*{cloud.SUFFIX}"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Local packed backup prune failed: %s", exc)


class BackupService:
    def __init__(self, config: AppConfig, collector):
        self.config = config
        self.collector = collector          # gives access to collector.db (reopenable)
        self.tg = TelegramClient(config.telegram)
        self._last_poll = 0.0
        self._last_sched = 0.0
        self._export = None

    @property
    def db(self):
        return self.collector.db

    # -- manual / scheduled export ------------------------------------------

    def export_now(self, reason: str = "manual") -> bool:
        return export_db(self.config, reason)

    def _check_schedule(self) -> None:
        from . import cloud
        enabled = (self.db.get_setting(S_ENABLED, "0") == "1")
        target = self.db.get_setting(S_TIME, "")
        # The cloud is turned on by its line in .env; it needs no switch on
        # the settings page as well, and backs up at four in the morning
        # Moscow time unless a time is set there.
        if cloud.configured():
            enabled = True
            target = target or CLOUD_DEFAULT_TIME
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

        # In the background: packing and uploading a large database takes a
        # minute or two, and the loop it would hold up places and defends
        # orders.
        if self._export is not None and self._export.is_alive():
            return
        import threading

        def run() -> None:
            ok = self.export_now("scheduled")
            if ok:
                self.db.set_setting(S_LAST, today)
                self.db.set_setting(S_RETRY, None)
            else:
                retry = (datetime.now(timezone.utc) + RETRY_DELAY).isoformat()
                self.db.set_setting(S_RETRY, retry)
                log.warning("Scheduled export failed; will retry after %s", retry)

        self._export = threading.Thread(target=run, name="db-export", daemon=True)
        self._export.start()

    # -- inbound restore via Telegram ---------------------------------------

    def _do_restore(self, new_file: Path) -> dict:
        """Swap in a new DB file, reopening the collector's connection."""
        self.collector.db.close()
        try:
            info = restore(self.config.db_path, new_file, self.config.backups_dir)
        finally:
            self.collector.reopen_db()
        return info

    def _answer_command(self, text: str) -> None:
        """Reply to one command. A question about what is standing must never
        set a sweep going, so the answer is read from the database only."""
        from . import tgcommands

        if tgcommands.command(text) in tgcommands.SENDS_A_FILE:
            self._send_item_dump(tgcommands.argument(text))
            return

        try:
            reply = tgcommands.answer(text, self.db)
        except Exception as exc:  # noqa: BLE001 - a bad answer is not a crash
            log.exception("Command %r failed: %s", text, exc)
            self.tg.send_message(f"Не смог ответить: {exc}")
            return
        if reply is None:
            if text.startswith("/"):
                self.tg.send_message(tgcommands.HELP, parse_mode="HTML")
            return
        self.tg.send_message(reply, parse_mode="HTML")

    DUMP_DAYS = 60

    def _send_item_dump(self, wanted: str) -> None:
        """Answer /dump with one item's tables as a file.

        The whole-database export is 69MB against Telegram's ~50MB ceiling and
        fails, and it carries the settings table - the proxy list with its
        passwords - so it was never the right thing to ask for anyway. One
        item's tables are tens of kilobytes and hold no credentials.
        """
        from . import item_dump

        if not wanted:
            self.tg.send_message(
                "Напиши, какой предмет: <code>/dump AWP | Printstream (Field-Tested)</code>",
                parse_mode="HTML")
            return
        try:
            item_id, name = item_dump.resolve(self.db.conn, wanted)
        except item_dump.ItemNotFound as miss:
            listed = "\n".join(f"• {c}" for c in miss.candidates[:30])
            more = ("\n…и ещё" if len(miss.candidates) > 30 else "")
            self.tg.send_message(
                f"Не нашёл «{wanted}». Есть такие:\n{listed}{more}"
                if listed else f"Не нашёл «{wanted}», и в базе пока нет предметов.")
            return
        except Exception as exc:  # noqa: BLE001 - a bad command is not a crash
            log.exception("Dump for %r failed to resolve: %s", wanted, exc)
            self.tg.send_message(f"Не смог найти предмет: {exc}")
            return

        try:
            body = item_dump.render(self.db.conn, item_id, name, self.DUMP_DAYS)
            self.config.backups_dir.mkdir(parents=True, exist_ok=True)
            path = self.config.backups_dir / item_dump.safe_filename(
                name, self.DUMP_DAYS)
            path.write_text(body, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.exception("Dump for %r failed: %s", name, exc)
            self.tg.send_message(f"Не смог собрать выгрузку: {exc}")
            return

        headings = [line[3:] for line in body.splitlines() if line.startswith("## ")]
        caption = name + ("\n" + "\n".join(headings) if headings else "")
        if not self.tg.send_document(path, caption=caption[:1000]):
            self.tg.send_message(f"Собрал выгрузку по «{name}», но отправить не смог.")

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

            # Text first: the loop was reading updates and throwing away
            # everything that was not a file.
            text = (msg.get("text") or "").strip()
            if text and not doc:
                if chat_id != auth_chat:
                    log.warning("Ignoring text from unauthorized chat %s", chat_id)
                    continue
                self._answer_command(text)
                continue

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
            got = tmp.with_suffix(".db.gz") if fname.endswith(".gz") else tmp
            if not self.tg.download_file(doc["file_id"], got):
                self.tg.send_message("Не удалось скачать файл из Telegram.")
                continue
            if got != tmp:
                # The exports go out packed now; one sent back to restore from
                # is unpacked first.
                from . import cloud
                try:
                    cloud.gunzip_file(got, tmp)
                except OSError as exc:
                    self.tg.send_message(f"Не удалось распаковать «{fname}»: {exc}")
                    continue
                finally:
                    got.unlink(missing_ok=True)
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
        from . import cloud
        tg_on = self.tg.configured()
        if not (tg_on or cloud.configured()):
            return
        mono = time.monotonic()
        if tg_on and mono - self._last_poll >= POLL_EVERY:
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
