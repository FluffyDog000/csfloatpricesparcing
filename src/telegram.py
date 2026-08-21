"""Minimal Telegram Bot API client: send the DB backup as a document, poll for
incoming documents (for restore), and send text confirmations.

Token and chat_id come from .env only (never hard-coded).
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

from .config import TelegramConfig

log = logging.getLogger("csfloat.telegram")

API = "https://api.telegram.org"


class TelegramClient:
    def __init__(self, cfg: TelegramConfig, timeout: int = 60):
        self.cfg = cfg
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.cfg.bot_token}/{method}"

    def configured(self) -> bool:
        return self.cfg.configured()

    # -- outbound -----------------------------------------------------------

    def send_message(self, text: str) -> bool:
        try:
            r = requests.post(
                self._url("sendMessage"),
                data={"chat_id": self.cfg.chat_id, "text": text},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("Telegram sendMessage failed: %s", exc)
            return False

    def send_document(self, path: Path, caption: str = "") -> bool:
        """Send a file as a document to the configured chat. Returns success."""
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    self._url("sendDocument"),
                    data={"chat_id": self.cfg.chat_id, "caption": caption},
                    files={"document": (Path(path).name, fh)},
                    timeout=max(self.timeout, 120),
                )
            r.raise_for_status()
            ok = r.json().get("ok", False)
            if not ok:
                log.warning("Telegram sendDocument not ok: %s", r.text[:200])
            return bool(ok)
        except (requests.RequestException, OSError, ValueError) as exc:
            log.warning("Telegram sendDocument failed: %s", exc)
            return False

    # -- inbound (for restore) ----------------------------------------------

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """Poll getUpdates. Short-poll by default (timeout=0) so it doesn't
        block the collector loop. Returns the list of update objects."""
        try:
            params = {"timeout": timeout, "allowed_updates": '["message"]'}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                self._url("getUpdates"), params=params,
                timeout=self.timeout + timeout,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except (requests.RequestException, ValueError) as exc:
            log.warning("Telegram getUpdates failed: %s", exc)
            return []

    def download_file(self, file_id: str, dest: Path) -> bool:
        """Resolve a file_id via getFile and download it to `dest`."""
        try:
            r = requests.get(
                self._url("getFile"), params={"file_id": file_id},
                timeout=self.timeout,
            )
            r.raise_for_status()
            result = r.json().get("result", {})
            file_path = result.get("file_path")
            if not file_path:
                return False
            url = f"{API}/file/bot{self.cfg.bot_token}/{file_path}"
            with requests.get(url, stream=True, timeout=max(self.timeout, 120)) as fr:
                fr.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as out:
                    for chunk in fr.iter_content(chunk_size=65536):
                        out.write(chunk)
            return True
        except (requests.RequestException, ValueError, OSError) as exc:
            log.warning("Telegram download_file failed: %s", exc)
            return False
