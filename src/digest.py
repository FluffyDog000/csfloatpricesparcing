"""The day in one message, and the things worth a message on their own.

`summary_text` answers /summary and goes out once a day; `alerts` lists what
is wrong right now, for the digest and for `AlertService`, which repeats each
one hourly while it lasts.
Both read the database only: a phone asking how things stand must never set a
request going, and an alert about a dead proxy cannot depend on that proxy.

Before these, a problem was found by opening the journal: the collector ran
out of file handles one morning and nothing said so until the history page
showed an hour of red.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .pacing import parse_iso

DIGEST_TIME_KEY = "tg_digest_time"       # "HH:MM" Moscow time, "" = off
DIGEST_DEFAULT_TIME = "21:00"
DIGEST_LAST_KEY = "tg_digest_last"

# How long a condition has to hold before it is worth a message: one failed
# sync or one refused amend is weather.
SYNC_FAIL_MINUTES = 30
HOLD_MINUTES = 20
NO_ROUTE_WINDOW_MINUTES = 30
NO_ROUTE_COUNT = 3
REFUSALS_WINDOW_MINUTES = 60
REFUSALS_COUNT = 20
HISTORY_QUIET_MINUTES = 60


def money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}".replace(",", " ")


def signed(value: float | None) -> str:
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + money(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _msk(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m %H:%M")


def _ago(minutes: float) -> str:
    return (_now() - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def _events_since(db, minutes: float) -> list[dict]:
    return [e for e in db.order_events(limit=20000, include_dry=False,
                                       since=_ago(minutes))]


# -- what is wrong now -----------------------------------------------------------

def alerts(db) -> dict[str, str]:
    """Conditions that hold right now, by key: {key: text}. Cheap - no
    scoring, no books - because it runs every few minutes."""
    out: dict[str, str] = {}

    # The account's order list cannot be read: fills go unnoticed, the brake
    # reads stale numbers, the defence tends what may be gone.
    try:
        sync = json.loads(db.get_setting("orders_sync_result") or "null") or {}
    except ValueError:
        sync = {}
    if sync.get("error"):
        last_ok = parse_iso(db.get_setting("orders_sync_at"))
        if last_ok is None or _now() - last_ok > timedelta(minutes=SYNC_FAIL_MINUTES):
            out["sync"] = ("сверка с аккаунтом не удаётся"
                           + (f" с {_msk(last_ok)} МСК" if last_ok else "")
                           + f": {str(sync['error'])[:200]}")

    # Amends refused for balance and held back.
    from .collector import AMEND_HOLD_KEY
    try:
        hold = json.loads(db.get_setting(AMEND_HOLD_KEY) or "null")
    except ValueError:
        hold = None
    if hold:
        since = parse_iso(hold.get("at"))
        if since and _now() - since > timedelta(minutes=HOLD_MINUTES):
            out["balance"] = ("CSFloat отказывает в правке цен «не хватило "
                              "баланса» — ордера не перебиваются. Сними слабые "
                              "ордера или пополни баланс")

    recent = _events_since(db, max(NO_ROUTE_WINDOW_MINUTES,
                                   REFUSALS_WINDOW_MINUTES))
    cut_route = _ago(NO_ROUTE_WINDOW_MINUTES)
    no_route = [e for e in recent if not e["ok"] and e["at"] >= cut_route
                and ("noroute" in str(e.get("detail") or "").lower()
                     or "адреса главного ключа недоступны"
                     in str(e.get("detail") or ""))]
    if len(no_route) >= NO_ROUTE_COUNT:
        out["route"] = ("прокси главного ключа не отвечает — ордера не "
                        "ставятся и не перебиваются. Проверь его на «Нагрузке»")

    cut_ref = _ago(REFUSALS_WINDOW_MINUTES)
    refused = [e for e in recent if not e["ok"] and e["at"] >= cut_ref]
    if len(refused) >= REFUSALS_COUNT and "route" not in out:
        out["refusals"] = (f"{len(refused)} отказов CSFloat за час — "
                           "посмотри «Проблемы» в журнале")

    # The history collector has stopped: every poll failing, or none at all.
    if db.active_items_count():
        rows = db.recent_poll_log(limit=1)
        last_ok = db.conn.execute(
            "SELECT MAX(polled_at) AS t FROM poll_log WHERE status = 'ok'"
        ).fetchone()["t"]
        when = parse_iso(last_ok)
        if rows and (when is None or _now() - when
                     > timedelta(minutes=HISTORY_QUIET_MINUTES)):
            out["history"] = ("история продаж не собирается"
                              + (f" с {_msk(when)} МСК" if when else "")
                              + ". Последняя ошибка: "
                              + str(rows[0].get("note") or rows[0].get("status"))[:200])
    return out


# -- the digest -------------------------------------------------------------------

def summary_text(db) -> str:
    """Balance, orders, the day's actions and buys, earnings, problems."""
    from . import profit_report
    from .settings import limits as read_limits, params as read_params

    limits = read_limits(db)
    lines = [f"<b>📊 Сводка — {_msk(_now())} МСК</b>", ""]

    # Money.
    if limits.balance:
        lines.append(f"Баланс: <b>{money(limits.balance)}</b>"
                     + (" (с аккаунта)" if limits.balance_live else " (из настроек)"))
    rows = [r for r in db.our_orders(live_only=False)
            if r["state"] in ("live", "manual")]
    face = sum(float(r["price"]) * int(r.get("quantity") or 1) for r in rows)
    manual = sum(1 for r in rows if r["state"] == "manual")
    line = f"Ордеров: <b>{len(rows)}</b> на <b>{money(face)}</b>"
    if limits.balance:
        line += f" · лимит CSFloat {money(limits.allowance)}"
        if face > limits.allowance:
            line += " ⚠️ сумма больше лимита"
    lines.append(line)
    if manual:
        lines.append(f"  из них вручную: {manual}")

    # The day.
    day = _events_since(db, 24 * 60)
    counts: dict[str, int] = {}
    room = []
    for e in day:
        key = e["kind"] if e["ok"] else "fail"
        counts[key] = counts.get(key, 0) + 1
        if e["ok"] and e.get("source") == "room":
            room.append(e)
    lines += ["", "<b>За сутки</b>",
              f"поставлено {counts.get('place', 0)} · поднято "
              f"{counts.get('raise', 0)} · снижено {counts.get('lower', 0)} · "
              f"снято {counts.get('cancel', 0)} · отказов {counts.get('fail', 0)}"]
    if room:
        lines.append(f"снято ради места под перебивание: {len(room)} — "
                     + ", ".join(html.escape(e["market_hash_name"])
                                 for e in room[:5])
                     + (" …" if len(room) > 5 else ""))
    try:
        from .guard import current
        reading = current(db, limits)
        lines.append(f"куплено: {reading.fills} на {money(reading.spent)}"
                     + (f" (защита от слива — при {money(reading.limit)})"
                        if reading.limit else ""))
    except Exception:  # noqa: BLE001 - the digest goes out regardless
        pass

    # Earnings.
    try:
        week = profit_report.build(db, read_params(db).fee, days=7)
        t, h = week["totals"], week["holding_totals"]
        lines += ["", "<b>Заработок</b>",
                  f"за 7 дней: {signed(t['profit'])} по {t['deals']} сделк(ам)"
                  + (f" ({t['pct']:+.1f}%)" if t.get("pct") is not None else "")]
        if h["count"]:
            lines.append(
                f"в наличии: {h['count']} на {money(h['spent'])}"
                + (f", из них ждут обмена {h['pending']}" if h.get("pending") else "")
                + f" · ожидаемо {signed(h['est_profit'])}"
                + (f" ({h['est_pct']:+.1f}%)" if h.get("est_pct") is not None else ""))
    except Exception:  # noqa: BLE001
        pass

    # Problems.
    now = alerts(db)
    lines += ["", "<b>Проблемы</b>"]
    lines += (["⚠️ " + html.escape(t) for t in now.values()] if now
              else ["нет"])
    return "\n".join(lines)


def digest_due(db, now: datetime | None = None) -> bool:
    """Whether today's digest is owed: past its time, not yet sent today."""
    target = db.get_setting(DIGEST_TIME_KEY)
    target = DIGEST_DEFAULT_TIME if target is None else target.strip()
    if not target:
        return False
    try:
        hh, mm = (int(x) for x in target.split(":"))
    except ValueError:
        return False
    msk = (now or _now()) + timedelta(hours=3)
    if db.get_setting(DIGEST_LAST_KEY) == msk.date().isoformat():
        return False
    return (msk.hour, msk.minute) >= (hh, mm)


def mark_sent(db, now: datetime | None = None) -> None:
    msk = (now or _now()) + timedelta(hours=3)
    db.set_setting(DIGEST_LAST_KEY, msk.date().isoformat())


def as_dict(db) -> dict[str, Any]:
    """For the settings page."""
    t = db.get_setting(DIGEST_TIME_KEY)
    return {"time": DIGEST_DEFAULT_TIME if t is None else t,
            "last": db.get_setting(DIGEST_LAST_KEY) or None}
