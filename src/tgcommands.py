"""Commands the Telegram bot answers.

Kept apart from the transport so the answers can be tested without a network
and without a token: this turns a command into text, and the caller does the
sending. Every command reads the database and nothing else - a phone asking
what is standing must never set a sweep going or spend a request.
"""
from __future__ import annotations

import logging

from . import holdings_report as hr
from .settings import defending, defend_minutes, limits as read_limits, params as read_params

log = logging.getLogger("csfloat.tgcommands")

HELP = (
    "<b>Что умею</b>\n"
    "/orders — сколько в ордерах и какая ожидаемая прибыль\n"
    "/items — список ордеров по одному\n"
    "/status — состояние бота: защита, лимиты, последняя сверка\n"
    "/help — это сообщение"
)


def known(text: str) -> bool:
    return command(text) in ("orders", "items", "status", "help", "start")


def command(text: str) -> str:
    """The bare command name, without the slash or a @botname suffix."""
    word = (text or "").strip().split()[:1]
    if not word or not word[0].startswith("/"):
        return ""
    return word[0][1:].split("@")[0].lower()


def answer(text: str, db) -> str | None:
    """The reply to one message, or None if it is not for us."""
    name = command(text)
    if name in ("help", "start"):
        return HELP
    if name == "orders":
        return _orders(db)
    if name == "items":
        return _items(db)
    if name == "status":
        return _status(db)
    return None


def _orders(db) -> str:
    held = hr.collect(db, read_params(db))
    body = hr.summary_text(held)
    limits = read_limits(db)
    if limits.total_capital:
        left = max(0.0, limits.budget - held.committed)
        body += (f"\n\nЛимит: {hr.money(limits.budget)} · "
                 f"свободно {hr.money(left)}")
    return body


def _items(db) -> str:
    held = hr.collect(db, read_params(db))
    return hr.items_text(held)


def _status(db) -> str:
    held = hr.collect(db, read_params(db))
    lines = [
        "<b>Защита:</b> " + (
            f"включена, проверка каждые {defend_minutes(db):.0f} мин"
            if defending(db) else "выключена — перебитые останутся как есть"),
        "<b>Отправка:</b> " + (
            "вхолостую — ничего не отправляется"
            if (db.get_setting("analysis_dry_run", "1") or "1") != "0"
            else "боевая"),
    ]
    for key, label in (("defend_last_at", "Последняя защита"),
                       ("orders_sync_at", "Последняя сверка с аккаунтом")):
        when = db.get_setting(key)
        lines.append(f"{label}: " + (str(when)[:16].replace("T", " ")
                                     if when else "не запускалась"))
    lines.append(f"Ордеров: {len(held.positions)}"
                 + (f", перебито {held.outbid}" if held.outbid else ""))
    return "\n".join(lines)
