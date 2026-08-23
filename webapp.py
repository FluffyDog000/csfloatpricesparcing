#!/usr/bin/env python3
"""Local web dashboard for the CSFloat sales tracker.

Reads (and now also manages) the SAME SQLite database the collector uses. The
tracked-item list lives in the DB — this dashboard can add / remove / pause /
resume items and toggle their pattern flag; the collector re-reads the list
every ~30s, so changes apply without restarting it. WAL mode keeps concurrent
read+write from colliding.

Run alongside the collector:
    python run_collector.py      # background writer
    python webapp.py             # this dashboard
Then open http://localhost:5000
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import statistics
import time
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask, abort, g, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from src.backup import bump_generation, restore
from src.backup_service import export_db
from src.config import load_config, load_items
from src.db import Database
from src.logging_setup import setup_logging
from src.pacing import (
    ADAPTIVE_MAX_MINUTES, PACE_MAX, adaptive_minutes, window_start,
)
from src.report import (
    aggregate_buckets,
    aggregate_seeds,
    date_bounds_iso,
    period_to_since_iso,
)

config = load_config()
setup_logging(config.log_path)
log = logging.getLogger("csfloat.web")

app = Flask(__name__)
app.secret_key = config.web.secret_key or secrets.token_hex(32)
if not config.web.secret_key:
    log.warning("FLASK_SECRET_KEY not set — using a random key (sessions reset on "
                "restart). Set it in .env for persistent logins.")
app.permanent_session_lifetime = timedelta(days=config.web.session_days)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MB upload cap

AUTH_ENABLED = bool(config.web.auth_password_hash)
if not AUTH_ENABLED:
    log.warning("DASHBOARD_PASSWORD_HASH not set — the dashboard is OPEN (no "
                "login). Fine for localhost; set it before exposing to the internet.")

# Simple in-memory brute-force guard: ip -> [fail_count, lock_until_epoch].
_LOGIN_FAILS: dict[str, list[float]] = {}
_MAX_FAILS = 7
_LOCK_SECONDS = 300  # 5 minutes


def _seed_if_empty() -> None:
    """Import items.yaml into a fresh DB so the dashboard has items to show
    even before the collector runs (one-time, only when items table is empty)."""
    db = Database(config.db_path)
    try:
        if db.items_count() == 0:
            for it in load_items():
                db.upsert_item(
                    it.name, active=it.active,
                    pattern_sensitive=it.pattern_sensitive,
                    interval_min_minutes=it.interval_min_minutes,
                    interval_max_minutes=it.interval_max_minutes,
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("Item seeding skipped: %s", exc)
    finally:
        db.close()


_seed_if_empty()


# ---------------------------------------------------------------------------
# Authentication (single login/password; password stored as a hash in .env)
# ---------------------------------------------------------------------------

_PUBLIC_ENDPOINTS = {"login", "static"}


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def _locked_out(ip: str) -> float:
    """Return seconds remaining on a lockout, or 0 if not locked."""
    rec = _LOGIN_FAILS.get(ip)
    if rec and rec[1] > time.time():
        return rec[1] - time.time()
    return 0.0


def _record_fail(ip: str) -> None:
    rec = _LOGIN_FAILS.setdefault(ip, [0.0, 0.0])
    rec[0] += 1
    if rec[0] >= _MAX_FAILS:
        rec[1] = time.time() + _LOCK_SECONDS
        rec[0] = 0
        log.warning("Login locked for %s for %ds after repeated failures", ip, _LOCK_SECONDS)


def _clear_fails(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)


@app.before_request
def _require_login():
    if not AUTH_ENABLED:
        return None
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if session.get("logged_in"):
        return None
    # Not authenticated.
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("login.html", error=None)

    ip = _client_ip()
    wait = _locked_out(ip)
    if wait > 0:
        return render_template(
            "login.html",
            error=f"Слишком много попыток. Подожди {int(wait) + 1} с.",
        ), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if (username == config.web.auth_username
            and check_password_hash(config.web.auth_password_hash, password)):
        _clear_fails(ip)
        session.permanent = True
        session["logged_in"] = True
        session["user"] = username
        nxt = request.args.get("next") or url_for("index")
        return redirect(nxt if nxt.startswith("/") else url_for("index"))

    _record_fail(ip)
    log.warning("Failed login for user '%s' from %s", username, ip)
    return render_template("login.html", error="Неверный логин или пароль."), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if AUTH_ENABLED else url_for("index"))


@app.context_processor
def _inject_globals():
    return {"auth_enabled": AUTH_ENABLED}


# ---------------------------------------------------------------------------
# Per-request DB (SQLite connections are per-thread; the dev server is threaded)
# ---------------------------------------------------------------------------

def get_db() -> Database:
    if "db" not in g:
        g.db = Database(config.db_path)
    return g.db


@app.teardown_appcontext
def close_db(_exc: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_PERIODS = {"7d", "30d", "all"}


def _pattern_sensitive(name: str) -> bool:
    meta = get_db().get_item_meta(name)
    return bool(meta["pattern_sensitive"]) if meta else True


def _require_item(name: str) -> int:
    item_id = get_db().get_item_id(name)
    if item_id is None:
        abort(404, description=f"Item not tracked: {name}")
    return item_id


def _resolve_range() -> tuple[str | None, str | None, str]:
    """Resolve the time window from request args. Either ?from=YYYY-MM-DD&to=...
    (custom range) or ?period=7d|30d|all. Returns (since, until, label)."""
    frm = request.args.get("from")
    to = request.args.get("to")
    if frm or to:
        try:
            since, until = date_bounds_iso(frm or None, to or None)
        except ValueError:
            abort(400, description="Bad date; expected YYYY-MM-DD")
        return since, until, "custom"
    p = (request.args.get("period") or config.reporting.default_period).lower()
    if p not in _ALLOWED_PERIODS:
        p = config.reporting.default_period
    since = period_to_since_iso(None if p == "all" else p)
    return since, None, p


def _serialize_sale(row: dict) -> dict:
    stickers = None
    if row.get("stickers_json"):
        try:
            stickers = json.loads(row["stickers_json"])
        except (ValueError, TypeError):
            stickers = None
    return {
        "sold_at": row.get("sold_at"),
        "sold_at_estimated": bool(row.get("sold_at_estimated")),
        "price": row.get("price"),
        "float_value": row.get("float_value"),
        "paint_seed": row.get("paint_seed"),
        "paint_index": row.get("paint_index"),
        "stickers": stickers,
    }


def _require_admin() -> None:
    """Gate mutating endpoints when an admin token is configured. No token set
    => open (fine for a localhost-only dashboard)."""
    token = config.web.admin_token
    if not token:
        return
    body = request.get_json(silent=True) or {}
    given = (
        request.headers.get("X-Admin-Token")
        or request.args.get("token")
        or body.get("token")
    )
    if given != token:
        abort(403, description="Admin token required to modify items.")


def _body_name() -> str:
    data = request.get_json(silent=True) or {}
    name = (data.get("market_hash_name") or "").strip()
    if not name:
        abort(400, description="market_hash_name is required")
    return name


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", admin_required=bool(config.web.admin_token))


@app.route("/item/<path:name>")
def item_page(name: str):
    _require_item(name)
    return render_template(
        "item.html",
        item_name=name,
        default_bucket=config.reporting.float_bucket_size,
        pattern_sensitive=_pattern_sensitive(name),
    )


# ---------------------------------------------------------------------------
# JSON API — reads
# ---------------------------------------------------------------------------

@app.route("/api/items")
def api_items():
    db = get_db()
    rows = db.items_summary()
    out = []
    folders: set[str] = set()
    for r in rows:
        name = r["market_hash_name"]
        icon = r["icon_url"]        # cached by the collector; never fetched here
        folder = r["folder"] or ""
        if folder:
            folders.add(folder)
        out.append(
            {
                "market_hash_name": name,
                "active": bool(r["active"]),
                "pattern_sensitive": bool(r["pattern_sensitive"]),
                "hidden": bool(r["hidden"]),
                "folder": folder,
                "icon_url": icon,
                "total_sales": r["total_sales"],
                "avg_price": r["avg_price"],
                "min_price": round(r["min_price"], 2) if r["min_price"] is not None else None,
                "max_price": round(r["max_price"], 2) if r["max_price"] is not None else None,
                "last_polled_at": r["last_polled_at"],
                "last_sold_at": r["last_sold_at"],
            }
        )
    return jsonify(
        {
            "items": out,
            "folders": sorted(folders),
            "last_update": db.last_successful_poll(),
        }
    )


@app.route("/api/item/aggregates")
def api_aggregates():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    since, until, label = _resolve_range()
    try:
        bucket = float(request.args.get("bucket", config.reporting.float_bucket_size))
    except ValueError:
        bucket = config.reporting.float_bucket_size

    rows = db.query_sales(item_id, since_iso=since, until_iso=until)
    prices = [r["price"] for r in rows if r["price"] is not None]
    overall = {
        "avg_price": round(statistics.mean(prices), 2) if prices else None,
        "median_price": round(statistics.median(prices), 2) if prices else None,
        "min_price": round(min(prices), 2) if prices else None,
        "max_price": round(max(prices), 2) if prices else None,
    }
    meta_icon = db.get_icon(name)
    icon = meta_icon.get("icon_url") if meta_icon else None
    return jsonify(
        {
            "item": name,
            "icon_url": icon,
            "pattern_sensitive": _pattern_sensitive(name),
            "period": label,
            "bucket_size": bucket,
            "total_sales": len(rows),
            "overall": overall,
            "buckets": aggregate_buckets(rows, bucket),
            "seeds": aggregate_seeds(rows),
            "last_update": db.last_successful_poll(item_id),
        }
    )


@app.route("/api/item/bucket_sales")
def api_bucket_sales():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    since, until, _ = _resolve_range()
    try:
        lo = float(request.args["bucket_lo"])
        size = float(request.args.get("bucket_size", config.reporting.float_bucket_size))
    except (KeyError, ValueError):
        abort(400, description="bucket_lo (and optional bucket_size) required")
    rows = db.sales_in_float_range(item_id, lo, lo + size, since_iso=since, until_iso=until)
    return jsonify({"sales": [_serialize_sale(r) for r in rows]})


@app.route("/api/item/latest_sales")
def api_latest_sales():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    since, until, _ = _resolve_range()
    try:
        limit = min(max(int(request.args.get("limit", 40)), 1), 200)
    except ValueError:
        limit = 40
    rows = db.query_sales(item_id, since_iso=since, until_iso=until)[:limit]
    return jsonify({"sales": [_serialize_sale(r) for r in rows]})


@app.route("/api/item/seed_sales")
def api_seed_sales():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    since, until, _ = _resolve_range()
    seed_raw = request.args.get("seed", "")
    seed = None if seed_raw in ("", "none", "(none)") else int(seed_raw)
    rows = db.query_sales(item_id, since_iso=since, until_iso=until, paint_seed=seed)
    return jsonify({"sales": [_serialize_sale(r) for r in rows]})


@app.route("/api/status")
def api_status():
    db = get_db()
    return jsonify({"last_update": db.last_successful_poll()})


# ---------------------------------------------------------------------------
# JSON API — item management (writes). Changes are picked up by the collector
# within ~30s. Guarded by CSFLOAT_ADMIN_TOKEN when set.
# ---------------------------------------------------------------------------

@app.route("/api/items/add", methods=["POST"])
def api_add_item():
    _require_admin()
    name = _body_name()
    data = request.get_json(silent=True) or {}
    folder = (data.get("folder") or "").strip() or None
    pattern = bool(data.get("pattern_sensitive", True))
    db = get_db()
    db.add_item(name, folder=folder, pattern_sensitive=pattern)
    log.info("Item added via web: '%s' (folder=%s)", name, folder)
    return jsonify({"ok": True})


@app.route("/api/items/add_bulk", methods=["POST"])
def api_add_bulk():
    _require_admin()
    data = request.get_json(silent=True) or {}
    names = data.get("names") or []
    if not isinstance(names, list):
        abort(400, description="names must be a list")
    folder = (data.get("folder") or "").strip() or None
    pattern = bool(data.get("pattern_sensitive", True))
    db = get_db()
    added, skipped = 0, 0
    seen: set[str] = set()
    for raw in names:
        name = (str(raw) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        db.add_item(name, folder=folder, pattern_sensitive=pattern)
        added += 1
    log.info("Bulk add via web: %d item(s), folder=%s", added, folder)
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.route("/api/items/update", methods=["POST"])
def api_update_item():
    _require_admin()
    name = _body_name()
    data = request.get_json(silent=True) or {}
    kwargs = {}
    if "active" in data:
        kwargs["active"] = bool(data["active"])
    if "pattern_sensitive" in data:
        kwargs["pattern_sensitive"] = bool(data["pattern_sensitive"])
    if "hidden" in data:
        kwargs["hidden"] = bool(data["hidden"])
    if "folder" in data:
        kwargs["folder"] = (data.get("folder") or "").strip() or None
    if not get_db().update_item(name, **kwargs):
        abort(404, description=f"Item not tracked: {name}")
    log.info("Item updated via web: '%s' %s", name, kwargs)
    return jsonify({"ok": True})


@app.route("/api/items/bulk", methods=["POST"])
def api_bulk_items():
    """Apply one action to many items: move to folder, hide/show, pause/resume,
    or delete (with history)."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    names = data.get("names") or []
    action = (data.get("action") or "").strip()
    if not isinstance(names, list) or not names:
        abort(400, description="names must be a non-empty list")

    db = get_db()
    done, missing = 0, 0
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        ok = False
        if action == "delete":
            ok = db.delete_item(name, purge_history=True)
        elif action == "folder":
            ok = db.update_item(name, folder=(data.get("folder") or "").strip() or None)
        elif action == "hide":
            ok = db.update_item(name, hidden=True)
        elif action == "show":
            ok = db.update_item(name, hidden=False)
        elif action == "pause":
            ok = db.update_item(name, active=False)
        elif action == "resume":
            ok = db.update_item(name, active=True)
        else:
            abort(400, description=f"Unknown action: {action}")
        done += 1 if ok else 0
        missing += 0 if ok else 1
    log.info("Bulk '%s' via web on %d item(s) (%d applied)", action, len(names), done)
    return jsonify({"ok": True, "applied": done, "missing": missing})


@app.route("/api/items/delete", methods=["POST"])
def api_delete_item():
    _require_admin()
    name = _body_name()
    data = request.get_json(silent=True) or {}
    purge = bool(data.get("purge_history", True))
    if not get_db().delete_item(name, purge_history=purge):
        abort(404, description=f"Item not tracked: {name}")
    log.info("Item deleted via web: '%s' (purge_history=%s)", name, purge)
    return jsonify({"ok": True, "purged": purge})


# ---------------------------------------------------------------------------
# Settings page + backup/restore
# ---------------------------------------------------------------------------

@app.route("/load")
def load_page():
    return render_template("load.html")


def _num_setting(db, key):
    raw = db.get_setting(key)
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _polling_settings():
    """Effective polling pace: dashboard settings override config.yaml."""
    db = get_db()
    p = config.polling

    def val(key, default):
        raw = db.get_setting(key)
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
    custom = any(db.get_setting(k) not in (None, "") for k in (
        "poll_interval_min_minutes", "poll_interval_max_minutes",
        "min_seconds_between_requests"))
    return lo, hi, spacing, custom


@app.route("/api/load")
def api_load():
    from datetime import datetime, timedelta, timezone
    db = get_db()
    active = db.get_active_items()
    gmin, gmax, spacing, custom = _polling_settings()

    # Estimated request rate, mirroring how the collector schedules: per-item
    # overrides win, then the adaptive interval, then the plain range — all
    # stretched by the learned pace multiplier.
    adaptive_on = (db.get_setting("adaptive_intervals", "1") or "1") != "0"
    try:
        adaptive_ceiling = float(db.get_setting("adaptive_max_minutes")
                                 or ADAPTIVE_MAX_MINUTES)
    except (TypeError, ValueError):
        adaptive_ceiling = ADAPTIVE_MAX_MINUTES
    try:
        pace_mult = min(max(float(db.get_setting("pace_multiplier") or 1.0), 1.0), PACE_MAX)
    except (TypeError, ValueError):
        pace_mult = 1.0

    try:
        quota_factor = max(float(db.get_setting("quota_factor") or 1.0), 1.0)
    except (TypeError, ValueError):
        quota_factor = 1.0
    rates = db.sales_rates(window_start()) if adaptive_on else {}
    reqs_per_min = 0.0
    intervals: list[float] = []
    for it in active:
        lo = it.get("interval_min_minutes")
        hi = it.get("interval_max_minutes")
        if lo or hi:
            avg_min = ((lo or gmin) + (hi or gmax)) / 2.0
        else:
            avg_min = (gmin + gmax) / 2.0
            if adaptive_on:
                item_id = it.get("id")
                cnt, first = rates.get(item_id, (0, None)) if item_id else (0, None)
                adapt = adaptive_minutes(cnt, first, floor_minutes=gmin,
                                         ceiling_minutes=adaptive_ceiling)
                if adapt is not None:
                    avg_min = adapt
        avg_min = max(avg_min * pace_mult * quota_factor, 0.1)
        intervals.append(avg_min)
        reqs_per_min += 1.0 / avg_min
    budget = 60.0 / max(spacing, 0.01)

    now = datetime.now(timezone.utc)
    hour_iso = (now - timedelta(hours=1)).replace(microsecond=0).isoformat()
    day_iso = (now - timedelta(hours=24)).replace(microsecond=0).isoformat()

    # Global 429 pause, persisted by the collector process.
    cooldown_until = db.get_setting("cooldown_until") or None
    cooldown_left = 0.0
    if cooldown_until:
        try:
            until = datetime.fromisoformat(cooldown_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            cooldown_left = max(0.0, (until - now).total_seconds())
        except ValueError:
            cooldown_until = None

    stats_hour = db.poll_stats(hour_iso)
    last_ok = db.last_successful_poll()
    stale_min = None
    if last_ok:
        try:
            t = datetime.fromisoformat(last_ok)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            stale_min = round((now - t).total_seconds() / 60.0, 1)
        except ValueError:
            pass

    # One-line health verdict for the dashboard.
    if stats_hour["auth_error"]:
        state, state_text = "auth", "Ошибка авторизации — обнови cookie в .env"
    elif cooldown_left > 0:
        state, state_text = "cooldown", (
            f"Пауза из-за лимита CSFloat, осталось {cooldown_left / 60:.1f} мин")
    elif stats_hour["rate_limited"]:
        state, state_text = "limited", "Были 429 за последний час — снизь частоту"
    elif stale_min is not None and stale_min > 60:
        state, state_text = "stale", f"Нет успешного сбора {stale_min:.0f} мин"
    elif not active:
        state, state_text = "idle", "Нет активных предметов"
    else:
        state, state_text = "ok", "Сбор идёт штатно"

    return jsonify(
        {
            "active_items": len(active),
            "total_items": db.items_count(),
            "reqs_per_min_est": round(reqs_per_min, 2),
            "budget_per_min": round(budget, 1),
            "budget_used_pct": round(100.0 * reqs_per_min / budget, 1) if budget else None,
            "min_seconds_between_requests": spacing,
            "interval_min_minutes": gmin,
            "interval_max_minutes": gmax,
            "intervals_customized": custom,
            "config_interval_min": config.polling.interval_min_minutes,
            "config_interval_max": config.polling.interval_max_minutes,
            "config_spacing": config.polling.min_seconds_between_requests,
            "quota_limit": _num_setting(db, "rl_limit"),
            "quota_remaining": _num_setting(db, "rl_remaining"),
            "quota_reset": _num_setting(db, "rl_reset"),
            "quota_factor": float(db.get_setting("quota_factor") or 1.0),
            "adaptive_enabled": adaptive_on,
            "adaptive_max_minutes": adaptive_ceiling,
            "pace_multiplier": round(pace_mult, 2),
            "pace_max": PACE_MAX,
            "avg_interval_minutes": round(sum(intervals) / len(intervals), 1) if intervals else None,
            "last_429_headers": db.get_setting("last_429_headers") or "",
            "last_429_body": db.get_setting("last_429_body") or "",
            "last_429_at": db.get_setting("last_429_at") or None,
            "cooldown_until": cooldown_until,
            "cooldown_remaining_sec": round(cooldown_left),
            "cooldown_consecutive": int(db.get_setting("cooldown_consecutive") or 0),
            "state": state,
            "state_text": state_text,
            "stale_minutes": stale_min,
            "last_update": last_ok,
            "stats_hour": stats_hour,
            "stats_day": db.poll_stats(day_iso),
            "gap_warnings": db.gap_warnings(day_iso, limit=50),
            "recent": db.recent_poll_log(limit=30),
        }
    )


@app.route("/api/load/settings", methods=["POST"])
def api_load_settings():
    """Change the polling pace live (collector picks it up on its next poll)."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()

    def store(key, field, lo_ok, hi_ok, label):
        # Absent field => leave the current value alone (partial update).
        if field not in data:
            return
        value = data.get(field)
        if value in (None, ""):
            db.set_setting(key, "")      # explicit empty = fall back to config.yaml
            return
        try:
            num = float(value)
        except (TypeError, ValueError):
            abort(400, description=f"{label}: нужно число")
        if not (lo_ok <= num <= hi_ok):
            abort(400, description=f"{label}: допустимо {lo_ok}–{hi_ok}")
        db.set_setting(key, str(num))

    if "reset" in data and data["reset"]:
        for k in ("poll_interval_min_minutes", "poll_interval_max_minutes",
                  "min_seconds_between_requests", "adaptive_max_minutes"):
            db.set_setting(k, "")
        log.info("Polling settings reset to config.yaml defaults")
        return jsonify({"ok": True, "reset": True})

    imin = data.get("interval_min_minutes")
    imax = data.get("interval_max_minutes")
    if imin not in (None, "") and imax not in (None, ""):
        try:
            if float(imax) < float(imin):
                abort(400, description="Максимальный интервал меньше минимального")
        except (TypeError, ValueError):
            abort(400, description="Интервалы: нужны числа")
    if "adaptive_intervals" in data:
        db.set_setting("adaptive_intervals", "1" if data["adaptive_intervals"] else "0")
    if "reset_pace" in data and data["reset_pace"]:
        db.set_setting("pace_multiplier", "1.0")
        log.info("Pace multiplier reset to 1.0 by user")
    store("adaptive_max_minutes", "adaptive_max_minutes", 15, 1440, "Потолок интервала")
    store("poll_interval_min_minutes", "interval_min_minutes", 1, 1440, "Интервал min")
    store("poll_interval_max_minutes", "interval_max_minutes", 1, 1440, "Интервал max")
    store("min_seconds_between_requests", "min_seconds_between_requests",
          0.5, 60, "Пауза между запросами")
    log.info("Polling settings updated via web: %s", data)
    return jsonify({"ok": True})


@app.route("/settings")
def settings_page():
    return render_template(
        "settings.html",
        telegram_configured=config.telegram.configured(),
    )


@app.route("/api/settings")
def api_get_settings():
    db = get_db()
    return jsonify(
        {
            "export_time_msk": db.get_setting("export_time_msk", "02:00"),
            "export_enabled": db.get_setting("export_enabled", "0") == "1",
            "last_export_date_msk": db.get_setting("last_export_date_msk"),
            "telegram_configured": config.telegram.configured(),
            "alerts_enabled": (db.get_setting("alerts_enabled", "1") or "1") != "0",
            "alert_stale_minutes": float(db.get_setting("alert_stale_minutes") or 90),
        }
    )


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    if "export_time_msk" in data:
        t = (data.get("export_time_msk") or "").strip()
        if t:
            try:
                hh, mm = [int(x) for x in t.split(":")]
                assert 0 <= hh < 24 and 0 <= mm < 60
            except (ValueError, AssertionError):
                abort(400, description="Время должно быть в формате ЧЧ:ММ (МСК)")
        db.set_setting("export_time_msk", t)
    if "export_enabled" in data:
        db.set_setting("export_enabled", "1" if data.get("export_enabled") else "0")
    if "alerts_enabled" in data:
        db.set_setting("alerts_enabled", "1" if data.get("alerts_enabled") else "0")
    if "alert_stale_minutes" in data:
        raw = data.get("alert_stale_minutes")
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            abort(400, description="Порог простоя: нужно число минут")
        if not (10 <= minutes <= 1440):
            abort(400, description="Порог простоя: допустимо 10–1440 минут")
        db.set_setting("alert_stale_minutes", str(minutes))
    log.info("Settings updated via web: %s", data)
    return jsonify({"ok": True})


@app.route("/api/backup/export_now", methods=["POST"])
def api_export_now():
    _require_admin()
    if not config.telegram.configured():
        abort(400, description="Telegram не настроен (TELEGRAM_BOT_TOKEN / CHAT_ID в .env)")
    ok = export_db(config, reason="manual-web")
    if not ok:
        abort(502, description="Не удалось отправить в Telegram — смотри лог")
    return jsonify({"ok": True})


@app.route("/api/backup/restore", methods=["POST"])
def api_restore():
    _require_admin()
    file = request.files.get("dbfile")
    if not file or not file.filename:
        abort(400, description="Файл не выбран")
    config.backups_dir.mkdir(parents=True, exist_ok=True)
    tmp = config.backups_dir / ("upload-" + secure_filename(file.filename or "upload.db"))
    file.save(str(tmp))
    # Close this request's connection before swapping the file.
    db = g.pop("db", None)
    if db is not None:
        db.close()
    try:
        info = restore(config.db_path, tmp, config.backups_dir)
    except ValueError as exc:
        Path(tmp).unlink(missing_ok=True)
        abort(400, description=str(exc))
    log.info("DB restored via web upload; previous DB backed up to %s", info.get("backup_path"))
    return jsonify({"ok": True, "backup": info.get("backup_path")})


@app.errorhandler(404)
def not_found(err):
    return jsonify({"error": str(getattr(err, "description", "not found"))}), 404


@app.errorhandler(400)
def bad_request(err):
    return jsonify({"error": str(getattr(err, "description", "bad request"))}), 400


@app.errorhandler(403)
def forbidden(err):
    return jsonify({"error": str(getattr(err, "description", "forbidden"))}), 403


@app.errorhandler(413)
def too_large(err):
    return jsonify({"error": "Файл слишком большой"}), 413


@app.errorhandler(502)
def bad_gateway(err):
    return jsonify({"error": str(getattr(err, "description", "upstream error"))}), 502


if __name__ == "__main__":
    log.info("Dashboard on http://%s:%d", config.web.host, config.web.port)
    app.run(host=config.web.host, port=config.web.port, threaded=True)
