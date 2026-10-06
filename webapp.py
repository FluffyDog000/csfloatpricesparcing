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
import pathlib
import sys
import secrets
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, Response, abort, g, jsonify, redirect, render_template, request, session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from src.backup import bump_generation, restore
from src.backup_service import export_db
from src.config import load_config, load_items
from src.db import Database, utcnow_iso
from src.logging_setup import setup_logging
from src.proxies import ROTATING_DEFAULT_LIMIT
from src.rates import DEFAULT_RATE_URL

# Until real responses are measured, size a full 40-sale page from an
# observed sample. Replaced by the measured average as soon as one poll
# has been logged.
ASSUMED_RESPONSE_BYTES = 9000
from src.pacing import PACE_MAX
from src.report import (
    aggregate_buckets,
    aggregate_seeds,
    date_bounds_iso,
    period_to_since_iso,
)

config = load_config()
setup_logging(config.web_log_path)
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


# A template's URL is revalidated on every load, but /static is served with a
# cache lifetime - so a deploy could leave a browser running yesterday's
# JavaScript against today's markup, which reads as "my changes did nothing".
# Stamping each asset with its mtime makes a changed file a different URL.
@app.context_processor
def _asset_version():
    def asset(filename: str) -> str:
        url = url_for("static", filename=filename)
        try:
            stamp = int(os.path.getmtime(
                os.path.join(app.static_folder, filename)))
        except OSError:
            return url
        return f"{url}?v={stamp}"

    def asset_version(filename: str) -> str:
        """The exact stamp `asset()` puts in that file's URL.

        The staleness check compared a script's own `?v=` against
        `asset_build`, which is neither the same file nor the same length - it
        is the newest mtime across all of static, cut to its last six digits
        for the header. The two could never be equal, so the page cried
        "браузер выполняет старый analysis.js" on every load, including this
        one, while the script it named was current.
        """
        try:
            return str(int(os.path.getmtime(
                os.path.join(app.static_folder, filename))))
        except OSError:
            return ""

    return {"asset": asset, "asset_build": _build_stamp(),
            "asset_version": asset_version,
            "session_persistent": bool(config.web.secret_key)}


_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_stamp() -> str:
    """Newest mtime across the served assets, shown in the UI so a screenshot
    says which build is actually running."""
    newest = 0.0
    try:
        for name in os.listdir(app.static_folder):
            path = os.path.join(app.static_folder, name)
            if os.path.isfile(path):
                newest = max(newest, os.path.getmtime(path))
    except OSError:
        return "?"
    return str(int(newest))[-6:]


@app.route("/api/diag")
def api_diag():
    """What is actually deployed here.

    Three screenshots were spent guessing whether a fix had reached the
    server, whether the browser had the new script, and which build was
    running. This answers all three from one URL."""
    import hashlib
    import subprocess

    def digest(path):
        try:
            data = pathlib.Path(path).read_bytes()
        except OSError as exc:
            return {"error": str(exc)}
        return {"bytes": len(data),
                "sha": hashlib.sha256(data).hexdigest()[:12],
                "mtime": int(os.path.getmtime(path))}

    try:
        commit = subprocess.run(["git", "log", "-1", "--format=%h %s"],
                                capture_output=True, text=True, timeout=5,
                                cwd=os.path.dirname(os.path.abspath(__file__)))
        head = commit.stdout.strip() or commit.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        head = f"недоступно: {exc}"

    files = {}
    for name in ("static/analysis.js", "static/common.js", "static/style.css",
                 "templates/analysis.html", "webapp.py", "src/pricing.py"):
        files[name] = digest(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), name))
    return jsonify({
        "commit": head,
        "build": _build_stamp(),
        "python": sys.version.split()[0],
        "files": files,
        "started_at": _STARTED_AT,
        "auth_enabled": AUTH_ENABLED,
        "session_persistent": bool(config.web.secret_key),
    })


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


def _cny_rate(db) -> float | None:
    """Stored USD->CNY rate, or None when we have never got one. Every caller
    must handle None: no rate simply means prices stay in USD."""
    raw = db.get_setting("usd_cny_rate")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


@app.route("/api/rate")
def api_rate():
    db = get_db()
    return jsonify({
        "rate": _cny_rate(db),
        "updated_at": db.get_setting("rate_updated_at") or None,
        "source": db.get_setting("rate_source", "auto"),
        "error": db.get_setting("rate_error") or "",
        "url": db.get_setting("rate_url") or DEFAULT_RATE_URL,
    })


@app.route("/api/rate", methods=["POST"])
def api_set_rate():
    """Set the rate by hand, or point auto mode at a different endpoint."""
    _require_admin()
    from src.rates import validate_rate

    data = request.get_json(silent=True) or {}
    db = get_db()
    if "rate" in data and str(data["rate"]).strip():
        rate, why = validate_rate(data["rate"])
        if rate is None:
            abort(400, description=why)
        db.set_setting("usd_cny_rate", f"{rate:.4f}")
        db.set_setting("rate_updated_at", utcnow_iso())
        db.set_setting("rate_error", "")
    if "source" in data:
        db.set_setting("rate_source",
                       "manual" if data["source"] == "manual" else "auto")
    if "url" in data:
        db.set_setting("rate_url", str(data["url"]).strip() or DEFAULT_RATE_URL)
    return jsonify({"ok": True, "rate": _cny_rate(db),
                    "source": db.get_setting("rate_source", "auto")})


@app.context_processor
def _inject_globals():
    return {"auth_enabled": AUTH_ENABLED, "cny_rate": _cny_rate(get_db())}


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

_ALLOWED_PERIODS = {"7d", "14d", "30d", "all"}


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
    (custom range) or ?period=7d|14d|30d|all. Returns (since, until, label)."""
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
    from src.catalog import CATEGORIES, WEARS, describe, rarity_label

    db = get_db()
    rows = db.items_summary()
    in_analysis = set(_analysis_items(db))
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
                # Recent windows. The page sorted on sales_7d and sales_30d
                # while this reply never carried them, so both sorts were a
                # no-op on zeros.
                "sales_1d": r["sales_1d"] or 0,
                "sales_7d": r["sales_7d"] or 0,
                "sales_30d": r["sales_30d"] or 0,
                "sales_90d": r["sales_90d"] or 0,
                "avg_price": r["avg_price"],
                # What it goes for now; the all-time average drifts with the
                # history's age.
                "price": r["avg_price_30d"] if r["avg_price_30d"] is not None
                else r["avg_price"],
                "rarity": r["rarity"],
                "rarity_name": rarity_label(r["rarity"]),
                "collection": r["collection"],
                "in_analysis": name in in_analysis,
                **describe(name),
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
            # Labels, so the page does not keep its own copy of them. Pairs,
            # not an object: the JSON encoder sorts keys, and the order here is
            # the order the chips are meant to read in.
            "categories": [[k, v] for k, v in CATEGORIES.items()],
            "wears": [[code, ru] for code, _, ru in WEARS],
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


def _cooldown_left(db) -> float:
    """Seconds remaining on the global 429 pause the collector persisted."""
    from datetime import datetime, timezone
    raw = db.get_setting("cooldown_until")
    if not raw:
        return 0.0
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())


def _quota_numbers(db):
    """(limit, remaining, reset) as last reported by CSFloat."""
    return (_num_setting(db, "rl_limit"), _num_setting(db, "rl_remaining"),
            _num_setting(db, "rl_reset"))


@app.route("/api/items/poll", methods=["POST"])
def api_poll_item():
    """Queue an immediate poll of one item, carried out by the collector.

    The web process deliberately does not fetch it itself: the collector owns
    the proxy pool, the request spacing and the 429 cooldown, and a second
    fetcher would spend quota those never see."""
    _require_admin()
    name = _body_name()
    db = get_db()
    if not db.request_poll(name):
        abort(404, description="Предмет не найден или снят с опроса.")

    # Say honestly when it will actually happen.
    waiting = []
    cooldown_left = _cooldown_left(db)
    if cooldown_left > 0:
        waiting.append(f"идёт пауза после 429, осталось {cooldown_left / 60:.0f} мин")
    _, remaining, _ = _quota_numbers(db)
    if remaining is not None and remaining <= 0:
        waiting.append("квота на окно исчерпана")

    log.info("Manual poll queued for '%s'%s", name,
             f" (ожидает: {'; '.join(waiting)})" if waiting else "")
    return jsonify({
        "ok": True,
        "queued": True,
        "waiting": waiting,
        "note": ("Опрос начнётся, когда снимется пауза: " + "; ".join(waiting))
                if waiting else "Опрос пройдёт в ближайшие ~5 секунд.",
    })


def _why_waiting(db) -> list[str]:
    """Why a queued request is not running yet.

    The collector holds manual requests behind the same quota and cooldown
    gates as scheduled polls, so "queued" can mean "waiting hours". Without a
    reason the panel looks like a broken collector.

    The collector gates on the live pool, not on the stored 429 timer, so the
    parked-route case has to be read from the routes too — otherwise a request
    blocked for hours reports no reason at all and reads as "просто долго"."""
    reasons = []
    try:
        routes = json.loads(db.get_setting("proxy_state") or "[]")
    except ValueError:
        routes = []
    if routes and not any(r.get("available") for r in routes):
        waits = [max(r.get("parked_sec") or 0, r.get("cooldown_sec") or 0)
                 for r in routes]
        soonest = min(waits) if waits else 0
        has_direct = any(r.get("direct") for r in routes)
        reasons.append(
            (f"нет доступных маршрутов, ближайший через {soonest / 60:.0f} мин"
             if soonest else "нет доступных маршрутов")
            + ("" if has_direct else " (свой IP сервера выключен)"))
    left = _cooldown_left(db)
    if left > 0:
        reasons.append(f"пауза после 429, осталось {left / 60:.0f} мин")
    for row in _json_setting(db, "main_key_state") or []:
        if (row.get("wait_sec") or 0) > 0:
            reasons.append(f"главный ключ: лимит CSFloat на {row.get('label')}, "
                           f"сброс через {row['wait_sec'] / 60:.0f} мин")
    _, remaining, reset = _quota_numbers(db)
    if remaining is not None and remaining <= 0:
        if reset:
            from datetime import datetime, timezone
            mins = (datetime.fromtimestamp(reset, timezone.utc)
                    - datetime.now(timezone.utc)).total_seconds() / 60
            if mins > 0:
                reasons.append(f"квота исчерпана, сброс через {mins:.0f} мин")
            else:
                reasons.append("квота исчерпана")
        else:
            reasons.append("квота исчерпана")
    return reasons


@app.route("/api/items/orders", methods=["POST"])
def api_request_orders():
    """Queue a buy-order fetch. Same reasoning as the poll button: the
    collector owns the routes and the rate limits, so it makes the request."""
    _require_admin()
    name = _body_name()
    db = get_db()
    if not db.request_orders(name):
        abort(404, description="Предмет не найден.")
    # Clear the previous failure: leaving it visible while the new attempt is
    # queued makes an old error look like the new one's result.
    db.set_setting("orders_error", "")
    db.set_setting("orders_error_at", "")
    waiting = _why_waiting(db)
    log.info("Buy-order fetch queued for '%s'%s", name,
             f" (ожидает: {'; '.join(waiting)})" if waiting else "")
    return jsonify({
        "ok": True, "waiting": waiting,
        "note": (f"Обход начнётся, когда снимется пауза: {'; '.join(waiting)}"
                 if waiting else "Обхожу лоты по диапазонам флота — до минуты."),
    })


@app.route("/api/orders")
def api_orders():
    """The stored order-book snapshot for one item."""
    db = get_db()
    name = request.args.get("item", "")
    item_id = db.get_item_id(name)
    if item_id is None:
        abort(404, description="Предмет не найден.")
    orders = db.buy_orders(item_id)
    # A queued request that never runs used to look identical to "nothing was
    # ever asked for": no orders, no error, no explanation. Surfacing the queue
    # makes a collector that is down or stuck visible instead of silent.
    queued = db.conn.execute(
        "SELECT orders_requested_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()["orders_requested_at"]
    # The sweep summary belongs to whichever item was swept last; only show it
    # when that is this item, or the panel would report someone else's numbers.
    summary = {}
    try:
        stored = json.loads(db.get_setting("orders_summary") or "{}")
        if stored.get("item") == name:
            summary = stored
    except ValueError:
        pass
    return jsonify({
        "orders": orders,
        "fetched_at": orders[0]["fetched_at"] if orders else None,
        "error": db.get_setting("orders_error") or "",
        "error_at": db.get_setting("orders_error_at") or None,
        "queued_at": queued,
        "waiting": _why_waiting(db),
        "bands": summary.get("bands"),
        "requests": summary.get("requests"),
    })


# ---------------------------------------------------------------------------
# Order analysis: which orders would we place, and why not the rest
# ---------------------------------------------------------------------------

ANALYSIS_KEY = "analysis_items"


def _analysis_items(db) -> list[str]:
    try:
        names = json.loads(db.get_setting(ANALYSIS_KEY) or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)]


from src.settings import (LIMIT_BOUNDS, LIMIT_KEYS, PARAM_BOUNDS,
                          PARAM_KEYS, defend_minutes, defending)
from src.settings import limits as _analysis_limits
from src.settings import (SCREEN_BOUNDS, SCREEN_KEYS,
                          params as _analysis_params,
                          screen_limits as _analysis_screen)

ANALYSIS_BOUNDS = PARAM_BOUNDS
ANALYSIS_KEYS = PARAM_KEYS


@app.route("/api/analysis/items", methods=["POST"])
def api_analysis_items():
    """Add or drop items on the analysis list.

    `set` replaces the whole list at once, which is what the picker sends: a
    dialog where boxes are ticked and unticked has one answer at the end, and
    applying it as a stream of adds and removes would leave the list half
    changed if one of them failed.

    `add_many` and `remove_many` are what the item list sends for a selection:
    a filter of several hundred items goes over in one request, and whatever
    is already on the list stays where it is."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    name = (data.get("market_hash_name") or "").strip()
    action = data.get("action") or "add"
    db = get_db()
    names = _analysis_items(db)
    unknown: list[str] = []
    if action == "clear":
        names = []
    elif action in ("add_many", "remove_many"):
        wanted = data.get("names")
        if not isinstance(wanted, list):
            abort(400, description="names must be a list")
        picked = [str(n or "").strip() for n in wanted]
        picked = [n for n in picked if n]
        if action == "remove_many":
            drop = set(picked)
            names = [n for n in names if n not in drop]
        else:
            for n in picked:
                if n in names:
                    continue
                if db.get_item_id(n) is None:
                    unknown.append(n)
                    continue
                names.append(n)
    elif action == "set":
        wanted = data.get("names")
        if not isinstance(wanted, list):
            abort(400, description="names must be a list")
        seen: list[str] = []
        for raw in wanted:
            n = str(raw or "").strip()
            if not n or n in seen:
                continue
            if db.get_item_id(n) is None:
                unknown.append(n)
                continue
            seen.append(n)
        names = seen
    elif not name:
        abort(400, description="market_hash_name is required")
    elif action == "remove":
        names = [n for n in names if n != name]
    else:
        if db.get_item_id(name) is None:
            abort(404, description=f"'{name}' не отслеживается — сначала добавь предмет")
        if name not in names:
            names.append(name)
    db.set_setting(ANALYSIS_KEY, json.dumps(names, ensure_ascii=False))
    return jsonify({"items": names, "unknown": unknown})


@app.route("/api/analysis/sweep", methods=["POST"])
def api_analysis_sweep():
    """Queue an order-book sweep for every item on the list.

    The web process never talks to CSFloat: the collector owns the routes and
    the rate limits, so it does the fetching and this only asks."""
    from src.orders import sweep_cost
    from src.screen import look

    _require_admin()
    db = get_db()
    params = _analysis_params(db)
    screen = _analysis_screen(db)
    names = _analysis_items(db)

    # The whole point of the free pass: one book costs about six requests
    # through the cookie and the residential route, so three hundred items is
    # over an hour of asking on the path that once drew "too many requests
    # from too many IPs". An item the history already rules out must not be
    # bought a place in that queue.
    # Adding a tenth item used to re-sweep the nine already done: the button
    # queued the whole list. A book and a set of listings read an hour ago are
    # what the analysis is about to read again, at thirty-odd requests a head.
    force = bool((request.get_json(silent=True) or {}).get("force"))

    wanted, skipped = [], []
    for name in names:
        item_id = db.get_item_id(name)
        if item_id is None:
            continue
        verdict = look(_sales_for(db, item_id, params), screen,
                       params.window_days, params.fee)
        if not verdict.passed:
            skipped.append((name, verdict.reason))
            continue
        fresh = "" if force else _sweep_not_needed(db, item_id, name)
        (skipped if fresh else wanted).append((name, fresh or verdict.reason))

    queued = [n for n, _ in wanted if db.request_orders(n)]
    db.set_setting("orders_error", "")
    db.set_setting("orders_error_at", "")
    waiting = _why_waiting(db)
    log.info("Analysis sweep queued for %d of %d item(s)%s", len(queued),
             len(names), f" (ожидает: {'; '.join(waiting)})" if waiting else "")
    saved = len(skipped)
    return jsonify({
        "queued": queued, "waiting": waiting,
        "skipped": [{"item": n, "reason": r} for n, r in skipped],
        "note": (f"Обход начнётся, когда снимется пауза: {'; '.join(waiting)}"
                 if waiting else
                 f"Обхожу обе стороны по {len(queued)} предмет(ам) — стакан и "
                 f"листинги, до минуты на каждый."
                 + (f" Пропущено {saved} — отсев по истории и уже свежие: "
                    f"до {sum(sweep_cost(n) for n, _ in skipped)} запросов, "
                    f"которые не придётся тратить."
                    if saved else "")),
    })


def _json_setting(db, key: str):
    try:
        return json.loads(db.get_setting(key) or "null")
    except ValueError:
        return None


BOOK_UNREAD = "стакан покупки не читался — сначала обход"

# How long a completed reading of both sides counts as current for the sweep
# button. Long enough that adding an item to the list does not re-buy the ones
# already done; short enough that "обойти" still means it when the market has
# had time to move.
SWEEP_FRESH_MINUTES = 45.0


def _sweep_not_needed(db, item_id: int, name: str,
                      fresh_minutes: float | None = None) -> str:
    """Why this item does not need the requests, or "" when it does.

    Three ways to need them: the book was never read, the listings are short
    of the bands the wear range implies - a sweep cut short leaves exactly
    that - or what we have has aged out.
    """
    from datetime import datetime, timezone

    from src.depth import depth_profile
    from src.depth_sweep import needs_prices
    from src.orders import wear_range

    swept = db.book_swept_at(item_id)
    if not swept:
        return ""
    try:
        depth = db.listing_depth(item_id)
    except Exception:  # noqa: BLE001 - an older DB has no such table
        return ""
    span = wear_range(name)
    if needs_prices(depth, span):
        return ""
    if span and len(depth) < len(depth_profile([], span)):
        return ""
    newest = max([swept] + [str(b["fetched_at"]) for b in depth])
    try:
        when = datetime.fromisoformat(newest)
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
    if age >= (SWEEP_FRESH_MINUTES if fresh_minutes is None else fresh_minutes):
        return ""
    return f"обойдён {age:.0f} мин назад — обе стороны на месте"


def _refuse_unread_book(db, item_id: int, bands: list) -> list:
    """Refuse to price an item whose competition nobody has measured.

    With no book the pricing finds no rival, bids the whole ceiling, leaves no
    room to answer an outbid, and counts every cheap sale as a fill nobody
    would have taken from us. All three are wrong in the same direction, and
    together they make an unswept item outrank the ones we have looked at - so
    the plan offered to spend $787 on one.

    The bands are still returned, with the reason, because "why is this item
    not here" is asked of the table. Held orders are left alone: an unpriceable
    band reconciles to KEEP, and cancelling a live order for want of a sweep
    would be acting on the same ignorance from the other side.
    """
    if db.book_swept_at(item_id):
        return bands
    for band in bands:
        band.take = False
        band.reason = BOOK_UNREAD
    return bands


def _sales_for(db, item_id: int, params) -> list[dict]:
    """An item's sales with each one's age, which is what the scoring reads.

    More history is loaded than the window uses. The screen wants a long-run
    median and the quiet-days figure, which need every row; the ladder windows
    what it is given down to `window_days` itself.

    That last part used to be untrue, and the comment here was the excuse: the
    rates were said to use the window while the medians used more. Nothing
    windowed anything - a quarter of sales was divided by a fortnight, and
    every flow in the model came out six times too large."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(params.window_days * 3, 90))).isoformat()
    rows = [dict(r) for r in db.conn.execute(
        "SELECT price, float_value, sold_at FROM sales "
        "WHERE item_id = ? AND float_value IS NOT NULL AND sold_at >= ?",
        (item_id, cutoff))]
    now = datetime.now(timezone.utc)
    for s in rows:
        try:
            t = datetime.fromisoformat(s["sold_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            s["age_days"] = (now - t).total_seconds() / 86400
        except (TypeError, ValueError):
            s["age_days"] = None
    return rows


@app.route("/api/analysis")
def api_analysis():
    """Score every float band of every listed item."""
    from src.executor import tied_up
    from src.orders import wear_range
    from src.pricing import plan

    from src.screen import cap_bids, look

    db = get_db()
    params = _analysis_params(db)
    screen = _analysis_screen(db)
    compare = request.args.get("compare") == "1"
    out = []
    for name in _analysis_items(db):
        item_id = db.get_item_id(name)
        if item_id is None:
            out.append({"item": name, "error": "предмет больше не отслеживается"})
            continue
        sales = _sales_for(db, item_id, params)

        # The free pass first. Scoring an item is cheap, but the order book it
        # needs is not, and an item that fails here would have failed after
        # the requests too.
        verdict = look(sales, screen, params.window_days, params.fee)
        if not verdict.passed:
            out.append({"item": name, "sales": len(sales), "orders": 0,
                        "depth": 0, "bands": [], "capital": 0.0,
                        "profit": 0.0, "swept_at": None,
                        "screen": verdict.as_dict(),
                        "screened_out": verdict.reason})
            continue

        # Our own orders are in the public book. The plan takes them out
        # before pricing; this did not, so every item we already bid on was
        # priced against ourselves here - a rung read as "cannot outbid"
        # because the order above it was ours - and the totals on this page
        # disagreed with the plan's.
        from src.holdings import strip_own
        orders = strip_own(db.buy_orders(item_id), db.our_orders(item_id))
        try:
            depth = db.listing_depth(item_id)
        except Exception:  # noqa: BLE001 - an older DB has no such table
            depth = []
        own = _locked_tops(db, item_id)
        scored = cap_bids(_refuse_unread_book(
            db, item_id,
            plan(sales, orders, wear_range(name), depth, params, own=own)),
            screen)
        bands = [b.as_dict() for b in scored]
        # The other pricing beside the one in use, rung by rung: switching is
        # a decision about money, and it is made on the items it would change.
        # Only when asked: it scores every item twice, and the page opening
        # scores the whole list already.
        from dataclasses import replace
        alt = {}
        if compare:
            alt_params = replace(params, adaptive=not params.adaptive)
            alt = {(round(b.float_min, 4), round(b.float_max, 4)): b
                   for b in cap_bids(_refuse_unread_book(
                       db, item_id,
                       plan(sales, orders, wear_range(name), depth, alt_params,
                            own=own)), screen)}
        for b in bands:
            other = alt.get((round(b["float_min"], 4), round(b["float_max"], 4)))
            if other is not None:
                b["alt"] = {"ceiling": other.ceiling, "bid": other.bid,
                            "take": other.take, "reason": other.reason,
                            "market": other.market, "sample": other.sample,
                            "window": other.window, "shift": other.shift,
                            "market_then": other.market_then,
                            "rank": other.rank}
        take = [b for b in bands if b["take"]]
        out.append({
            "item": name,
            "screen": verdict.as_dict(),
            "screened_out": "",
            "sales": len(sales),
            "orders": len(orders),
            "swept_at": orders[0]["fetched_at"] if orders else None,
            "depth": len(depth),
            # When the sell side was last read, which is not when the book was:
            # the listings are written only by a requested sweep, never on a
            # schedule, so the queue count is exactly as old as that. A lot
            # repriced or sold since is still in it.
            "depth_at": max((b["fetched_at"] for b in depth), default=None),
            "bands": bands,
            **({"alt_take": sum(1 for b in bands if (b.get("alt") or {}).get("take"))}
               if compare else {}),
            # Face value of the orders - what the ten-times-balance rule counts.
            "capital": round(sum(b["bid"] for b in take), 2),
            # And the money their fills would keep busy - what actually runs
            # out, and what the plan spends its budget by.
            "tied_up": round(sum(tied_up(b) for b in scored if b.take), 2),
            # What the orders would make once, not per month: the annualised
            # figure divided a margin we trust by a cycle time we do not.
            "profit": round(sum(b["bid"] * (b["margin"] or 0) for b in take), 2),
        })
    return jsonify({
        "items": out,
        "params": params.__dict__,
        "screen": screen.as_dict(),
        "error": db.get_setting("orders_error") or "",
        "waiting": _why_waiting(db),
    })


@app.route("/api/analysis/plan")
def api_analysis_plan():
    """What the bot would do right now: place, defend, withdraw.

    Nothing is sent. The request that creates an order on CSFloat is not
    documented and has to be captured from the browser, so until it is
    supplied this is the whole of the feature - and even once it is, the plan
    is produced first and acted on separately."""
    from src.executor import (exposure, rank, reconcile, select_portfolio,
                              tied_up)
    from src.holdings import strip_own
    from src.orders import wear_range
    from src.placement import PLACEMENT_KEY, describe, load
    from src.pricing import plan as plan_bands

    from src.screen import cap_bids

    db = get_db()
    params = _analysis_params(db)
    limits = _analysis_limits(db)
    screen = _analysis_screen(db)
    spec = load(db.get_setting(PLACEMENT_KEY))

    # Everything is scored first and chosen afterwards. Choosing item by item
    # spent the budget in the order the list happened to be typed in, which
    # with three hundred items decides the whole result.
    held: dict[str, float] = {}
    books: dict[str, list] = {}
    mine_by_item: dict[str, list] = {}
    candidates: list[tuple[str, object]] = []
    holding: list[tuple[str, float, float]] = []
    # What each held item was priced from, for the orders the plan drops:
    # their own range is priced again to say why (and, in the Excel export,
    # how it reads under the other settings).
    context: dict[str, dict] = {}
    from src.phases import is_phase
    phase_items = 0
    for name in _analysis_items(db):
        item_id = db.get_item_id(name)
        if item_id is None:
            continue
        if is_phase(name):
            # An order is placed by market name, and every Doppler phase
            # shares one: an order for "Phase 2" would buy a Phase 1 or a Ruby
            # just as readily. Scored on the analysis page, never placed,
            # until orders can be tied to a paint index.
            phase_items += 1
            continue
        sales = _sales_for(db, item_id, params)
        orders = db.buy_orders(item_id)
        try:
            depth = db.listing_depth(item_id)
        except Exception:  # noqa: BLE001 - an older DB has no such table
            depth = []
        mine = db.our_orders(item_id)
        # Our own orders are in the public book. Left there, every count of
        # who is ahead of us includes us.
        orders = strip_own(orders, mine)
        books[name] = orders
        mine_by_item[name] = mine
        held[name] = sum(float(r["price"]) * int(r.get("quantity") or 1)
                         for r in mine)
        holding += [(name, float(r["float_min"]), float(r["float_max"]))
                    for r in mine]
        own = _locked_tops(db, item_id)
        candidates += [(name, b) for b in cap_bids(_refuse_unread_book(
            db, item_id,
            plan_bands(sales, orders, wear_range(name), depth, params,
                       own=own)), screen)]
        if mine:
            context[name] = {"sales": sales, "orders": orders, "depth": depth,
                             "own": own, "span": wear_range(name), "cache": {}}

    # How many items each new order asks for, before the money is shared
    # out: an order for three holds three bids of face value.
    from src.executor import size_orders
    size_orders([b for _, b in candidates], limits)

    trace: list = []
    wanted_by_item = select_portfolio(candidates, limits, holding, trace=trace)
    # Kept for the Excel export, which needs every row and the fields the
    # page does not show.
    g.plan_trace = trace

    actions: list[dict] = []
    ranked: dict[tuple, tuple[float, float]] = {}
    for name in mine_by_item:
        wanted = wanted_by_item.get(name, [])
        for b in wanted:
            ranked[(name, round(b.float_min, 4), round(b.float_max, 4))] = \
                (rank(b), tied_up(b))
        actions += [a.as_dict() for a in
                    reconcile(name, wanted, mine_by_item[name],
                              books[name], limits)]

    # A held order the plan drops is withdrawn either because its band no
    # longer qualifies or because it qualifies and does not fit a limit. Both
    # read "больше не проходит фильтры" before, and 54 orders about to come
    # down for the 6× cap looked like 54 that had gone bad.
    not_taken = {(row["item"], round(row["band"].float_min, 4),
                  round(row["band"].float_max, 4)): row["reason"]
                 for row in trace if not row["taken"] and row["reason"]}
    # The rest no longer qualify - and "не проходит фильтры" alone left the
    # question every one of 74 such cancels raised: which filter, by how much.
    scored = {(name, round(b.float_min, 4), round(b.float_max, 4)): b
              for name, b in candidates}
    dropped: dict[tuple, object] = {}
    for action in actions:
        if action["kind"] != "cancel":
            continue
        key = (action["item"], round(action["float_min"], 4),
               round(action["float_max"], 4))
        why = not_taken.get(key)
        if why:
            action["reason"] = f"проходит, но не помещается в план: {why}"
            continue
        band = scored.get(key)
        if band is None and action["item"] in context:
            # A range the climb never steps on: priced on its own.
            from src.pricing import evaluate as price_one
            ctx = context[action["item"]]
            band = price_one(key[1], key[2], ctx["sales"], ctx["orders"],
                             ctx["span"], ctx["depth"], params, own=ctx["own"],
                             cache=ctx["cache"])
            if band.take:
                band.reason = ("такой полосы нет в лестнице "
                               "(сама по себе проходит)")
        if band is not None:
            dropped[key] = band
            if band.reason:
                action["reason"] = f"больше не проходит: {band.reason}"
    g.plan_dropped = dropped
    g.plan_scored = scored
    g.plan_context = context

    # The rank travels with the action, not just the sort. Ordering by a
    # number the page never shows leaves "why is this one first" unanswerable
    # from the table, which is the question the order exists to answer.
    # Full precision, rounded only where it is printed: two ranks that agree
    # to four decimals are still ordered by the difference behind them, and
    # rounding before the sort turned that into a tie broken by item name.
    for action in actions:
        action["rank"], tied = ranked.get(
            (action["item"], round(action["float_min"], 4),
             round(action["float_max"], 4)), (0.0, 0.0))
        # Not a field of the action itself: what the plan spends its budget
        # by, carried to the page so "из лимита" counts the same thing.
        action["tied_up"] = round(tied, 2) if tied != float("inf") else None

    # Shown best-first, across every item. Grouping by item was the order the
    # names happened to be typed in, so the page said nothing about which
    # order is worth placing first - the very thing the rank is for. Cancels
    # and raises stay on top: they free capacity the places then use.
    kind_order = {"cancel": 0, "raise": 1, "place": 2, "keep": 3}
    actions.sort(key=lambda a: (
        kind_order.get(a["kind"], 9),
        -a["rank"],
        a["item"], a["float_max"]))

    # What each item would hold once the plan is applied. Several orders on one
    # item are not several bets: they are one bet in pieces, and they fill
    # together when that market moves, so the split has to be visible.
    from src.executor import Action
    fields = set(Action.__dataclass_fields__)
    after = exposure([Action(**{k: v for k, v in a.items() if k in fields})
                      for a in actions], held)
    after = {k: round(v, 2) for k, v in after.items() if v > 0}
    total = sum(after.values())

    return jsonify({
        "actions": actions,
        "queue": _placement_queue(trace),
        "limits": limits.as_dict(),
        "screen": _analysis_screen(db).as_dict(),
        "held": held,
        "by_item": after,
        "planned_total": round(total, 2),
        "concentration": (max(after.values()) / total) if total else 0.0,
        "armed": (db.get_setting("analysis_armed") or "0") == "1",
        "dry_run": (db.get_setting("analysis_dry_run", "1") or "1") != "0",
        "pending": bool(db.get_setting("analysis_pending_actions")),
        "creates": _creates_status(db),
        "phase_items": phase_items,
        "defend": defending(db),
        "defend_minutes": defend_minutes(db),
        "auto_free": (db.get_setting("an_auto_free") or "1") == "1",
        "auto_fill": (db.get_setting(AUTO_FILL_KEY) or "0") == "1",
        **_auto_sweep_settings(db),
        "defend_at": db.get_setting("defend_last_at") or None,
        "last_defend": _json_setting(db, "defend_result"),
        "last_apply": _json_setting(db, "analysis_apply_result"),
        "placement": describe(spec),
        "can_place": spec.can_place,
        "can_cancel": spec.can_cancel,
        # Whether the held figures above were checked against the account, or
        # are only what the bot wrote down. They are not the same claim.
        "sync_at": db.get_setting("orders_sync_at") or None,
        "sync": _json_setting(db, "orders_sync_result"),
        "guard": _guard_state(db, limits),
    })


QUEUE_ROWS = 150


def _placement_queue(trace: list) -> list[dict]:
    """Every band the model would open, in the order the plan spends money
    on them: what it costs, the running total, and - for the ones that did
    not make it - which limit stopped them. The actions table shows only
    what fits; this shows where the line falls and what lies past it."""
    from src.executor import rank as rank_of

    out, running = [], 0.0
    for n, row in enumerate(trace[:QUEUE_ROWS], 1):
        b = row["band"]
        cost = row["cost"]
        finite = cost is not None and cost != float("inf")
        if row["taken"] and finite:
            running += cost
        margin = b.margin_expected if b.margin_expected is not None else b.margin
        profit = ((b.lam or 0.0) * (b.bid or 0.0) * (margin or 0.0)
                  if b.bid else None)
        out.append({
            "n": n, "item": row["item"],
            "float_min": b.float_min, "float_max": b.float_max,
            "bid": b.bid, "ceiling": b.ceiling, "margin": b.margin,
            "quantity": b.quantity,
            "margin_expected": b.margin_expected, "lam": b.lam,
            "t_sell": b.t_sell, "rank": rank_of(b),
            "tied_up": round(cost, 2) if finite else None,
            "running": round(running, 2) if row["taken"] else None,
            "profit_day": round(profit, 2) if profit is not None else None,
            "taken": row["taken"], "held": row["held"],
            "reason": row["reason"],
            # Running, for the taken: the money in use on a bad day, and the
            # face value of the orders - what CSFloat's 10× rule counts.
            "peak": round(row["peak"], 2) if row.get("peak") is not None else None,
            "face": round(row["face"], 2) if row.get("face") is not None else None,
        })
    return out


def _locked_tops(db, item_id: int) -> list[float]:
    """Our purchases of this item still inside the trade lock: they will be
    in front of anything we buy next when the queue is reached."""
    from src.holdings import locked_tops
    from src.ladder import LOCK_DAYS
    return locked_tops(db.our_orders(item_id, live_only=False), LOCK_DAYS)


def _guard_state(db, limits) -> dict:
    """The brake as the page shows it: whether it tripped, and where today
    stands against it."""
    from src.guard import current, trades_readable, tripped
    return {"tripped": tripped(db),
            "today": current(db, limits).as_dict(),
            "by_trades": trades_readable(db)}


@app.route("/api/analysis/guard", methods=["POST"])
def api_analysis_guard():
    """Release the brake. By hand only: it tripped because something looked
    wrong, and nothing but a person can say that it no longer does."""
    from src.guard import reset
    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    if data.get("reset"):
        reset(db)
        log.warning("Brake released by hand")
    return jsonify(_guard_state(db, _analysis_limits(db)))


@app.route("/api/analysis/arm", methods=["POST"])
def api_analysis_arm():
    """Permission to spend, given separately from configuring and planning."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    on = bool(data.get("armed"))
    db.set_setting("analysis_armed", "1" if on else "0")
    if "dry_run" in data:
        db.set_setting("analysis_dry_run", "0" if not data["dry_run"] else "1")
    if "defend" in data:
        db.set_setting("an_defend", "1" if data["defend"] else "0")
    if "defend_minutes" in data:
        db.set_setting("an_defend_minutes", str(data["defend_minutes"]).strip())
    if "auto_free" in data:
        db.set_setting("an_auto_free", "1" if data["auto_free"] else "0")
    if "auto_fill" in data:
        db.set_setting(AUTO_FILL_KEY, "1" if data["auto_fill"] else "0")
    if "auto_sweep" in data:
        db.set_setting(AUTO_SWEEP_KEY, "1" if data["auto_sweep"] else "0")
    if "auto_sweep_minutes" in data:
        db.set_setting(AUTO_SWEEP_MIN_KEY, str(_clamp_sweep_minutes(
            data["auto_sweep_minutes"])))
    log.warning("Analysis arming set to %s (dry run %s, defence %s)", on,
                db.get_setting("analysis_dry_run", "1"),
                db.get_setting("an_defend", "0"))
    return jsonify({"armed": on,
                    "dry_run": (db.get_setting("analysis_dry_run", "1") or "1") != "0",
                    "defend": defending(db),
                    "defend_minutes": defend_minutes(db),
                    "auto_free": (db.get_setting("an_auto_free") or "1") == "1",
                    "auto_fill": (db.get_setting(AUTO_FILL_KEY) or "0") == "1",
                    **_auto_sweep_settings(db)})


AUTO_FILL_KEY = "an_auto_fill"
AUTO_FILL_LAST_KEY = "an_auto_fill_last"
AUTO_FILL_EVERY_SECONDS = 1800
# A plan priced from an old book bids against rivals who may have moved; the
# defence would correct it within minutes, but there is no need to start off.
AUTO_FILL_FRESH_HOURS = 6.0


def _face_standing(db) -> float:
    """Face value of every order standing on the account now."""
    return sum(float(r["price"]) * int(r.get("quantity") or 1)
               for r in db.our_orders(live_only=False)
               if r["state"] in ("live", "manual"))


def auto_fill_once(db) -> dict:
    """One round of auto-fill, its outcome kept for the status panel."""
    out = _auto_fill_round(db)
    if out.get("skipped") != "выключено":
        db.set_setting(AUTO_FILL_RESULT_KEY, json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **out},
            ensure_ascii=False))
    return out


def _auto_fill_round(db) -> dict:
    """Place the best new orders the plan wants, up to the day's creations
    left, without anyone pressing "apply". Only placements, and only where
    the book is fresh: an item with an old book is sent for a sweep instead,
    for the next round. Handed to the collector the way the button does it,
    so the brake, dry run and logging all apply unchanged.

    Returns what it did, for logging and tests."""
    import json as _json

    from src.guard import tripped
    from src.pacing import parse_iso

    if (db.get_setting(AUTO_FILL_KEY) or "0") != "1":
        return {"skipped": "выключено"}
    if tripped(db):
        return {"skipped": "сработала защита от слива"}
    if db.get_setting("analysis_pending_actions"):
        return {"skipped": "в очереди уже есть план"}
    plan = api_analysis_plan().get_json()
    if not plan["can_place"]:
        return {"skipped": "постановка не настроена"}
    left = int(plan["creates"]["left"])
    if left <= 0:
        return {"skipped": "создания на сегодня кончились"}

    now = datetime.now(timezone.utc)
    fresh, stale = [], []
    for a in plan["actions"]:
        if a["kind"] != "place":
            continue
        item_id = db.get_item_id(a["item"])
        swept = parse_iso(db.book_swept_at(item_id)) if item_id else None
        if swept and (now - swept).total_seconds() <= AUTO_FILL_FRESH_HOURS * 3600:
            fresh.append(a)
        else:
            stale.append(a)
    # Only into the room actually free. The plan's places assume its cancels
    # happen too, and auto-fill sends no cancels: placing them on top of every
    # standing order went past the cap, and took the very room the defence
    # had just freed to answer an outbid.
    lim = plan["limits"]
    caps = [float(v) for v in (lim.get("order_cap"), lim.get("allowance"))
            if v is not None]
    room = (min(caps) - _face_standing(db)) if caps else float("inf")
    take = []
    for a in fresh:
        if len(take) >= left:
            break
        need = float(a["price"]) * int(a.get("quantity") or 1)
        if need > room + 1e-9:
            continue
        take.append(a)
        room -= need
    if fresh and not take:
        return {"skipped": "нет места под лимитом суммы ордеров", "left": left}
    asked = []
    for a in stale:
        if len(asked) >= max(left - len(take), 0) * 2:
            break
        if a["item"] not in asked and db.request_orders(a["item"]):
            asked.append(a["item"])
    if take:
        db.set_setting("analysis_pending_actions", _json.dumps(
            {"at": now.isoformat(timespec="seconds"), "source": "auto",
             "actions": take}, ensure_ascii=False))
        log.warning("Auto-fill queued %d new order(s); %d book(s) sent for a sweep",
                    len(take), len(asked))
    return {"queued": len(take), "swept": len(asked), "left": left}


AUTO_FILL_RESULT_KEY = "an_auto_fill_result"
AUTO_SWEEP_KEY = "an_auto_sweep"
AUTO_SWEEP_MIN_KEY = "an_auto_sweep_minutes"
AUTO_SWEEP_LAST_KEY = "an_auto_sweep_last"
AUTO_SWEEP_RESULT_KEY = "an_auto_sweep_result"
AUTO_SWEEP_BOUNDS = (30, 1440)
AUTO_SWEEP_DEFAULT = 120


def _clamp_sweep_minutes(raw) -> int:
    try:
        value = int(float(str(raw).strip().replace(",", ".")))
    except (TypeError, ValueError):
        value = AUTO_SWEEP_DEFAULT
    lo, hi = AUTO_SWEEP_BOUNDS
    return min(max(value, lo), hi)


def _auto_sweep_settings(db) -> dict:
    return {"auto_sweep": (db.get_setting(AUTO_SWEEP_KEY) or "0") == "1",
            "auto_sweep_minutes": _clamp_sweep_minutes(
                db.get_setting(AUTO_SWEEP_MIN_KEY) or AUTO_SWEEP_DEFAULT)}


def auto_sweep_once(db) -> dict:
    """Send the analysis list's books for a sweep: every item that passes the
    free pass and whose book is older than the interval.

    Only the defence and auto-fill read books on their own, and both only for
    items already in play - an item added to the list, or one whose book went
    stale, never reached the plan and so never got read. Kept for the status
    panel; the next auto-fill runs as soon as this sweep is done.
    """
    from src.phases import is_phase
    from src.screen import look

    cfg = _auto_sweep_settings(db)
    if not cfg["auto_sweep"]:
        return {"skipped": "выключено"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if db.pending_order_requests():
        out = {"skipped": "предыдущий обход ещё идёт"}
    else:
        params = _analysis_params(db)
        screen = _analysis_screen(db)
        queued, fresh, screened = [], 0, 0
        for name in _analysis_items(db):
            item_id = db.get_item_id(name)
            if item_id is None or is_phase(name):
                continue
            if not look(_sales_for(db, item_id, params), screen,
                        params.window_days, params.fee).passed:
                screened += 1
                continue
            # A little under the interval: a book read 115 minutes ago is due
            # on a two-hour round, not skipped to the next one.
            if _sweep_not_needed(db, item_id, name,
                                 fresh_minutes=cfg["auto_sweep_minutes"] * 0.9):
                fresh += 1
                continue
            if db.request_orders(name):
                queued.append(name)
        out = {"queued": len(queued), "fresh": fresh, "screened": screened}
        log.info("Auto-sweep queued %d book(s); %d fresh, %d screened out",
                 len(queued), fresh, screened)
    db.set_setting(AUTO_SWEEP_RESULT_KEY, json.dumps({"at": now, **out},
                                                     ensure_ascii=False))
    return out


def _sweep_finished_after(db, epoch: float) -> bool:
    """The last sweep ended after `epoch` and nothing is left in the queue."""
    from src.pacing import parse_iso
    state = _json_setting(db, "sweep_state") or {}
    ended = parse_iso(state.get("finished_at"))
    return (ended is not None and ended.timestamp() > epoch
            and not db.pending_order_requests())


def automation_tick(db, now: float | None = None) -> dict:
    """One check of both background jobs. Auto-sweep on its own interval;
    auto-fill every half hour, and also right after an auto-sweep finishes -
    the new books are what it should place from, not ones half an hour old."""
    import time as _time

    now = _time.time() if now is None else now
    did: dict = {}
    sweep = _auto_sweep_settings(db)
    if sweep["auto_sweep"]:
        last = float(db.get_setting(AUTO_SWEEP_LAST_KEY) or 0)
        if now - last >= sweep["auto_sweep_minutes"] * 60:
            did["sweep"] = auto_sweep_once(db)
            # Behind a sweep still running, it asks again next minute rather
            # than skipping a whole interval.
            if did["sweep"].get("skipped") != "предыдущий обход ещё идёт":
                db.set_setting(AUTO_SWEEP_LAST_KEY, str(now))
    if (db.get_setting(AUTO_FILL_KEY) or "0") == "1":
        last_fill = float(db.get_setting(AUTO_FILL_LAST_KEY) or 0)
        last_sweep = float(db.get_setting(AUTO_SWEEP_LAST_KEY) or 0)
        after_sweep = (sweep["auto_sweep"] and last_sweep > last_fill
                       and _sweep_finished_after(db, last_sweep))
        if now - last_fill >= AUTO_FILL_EVERY_SECONDS or after_sweep:
            db.set_setting(AUTO_FILL_LAST_KEY, str(now))
            did["fill"] = auto_fill_once(db)
    return did


def _epoch_iso(raw) -> str | None:
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _plus_minutes(iso: str | None, minutes: float) -> str | None:
    from src.pacing import parse_iso
    when = parse_iso(iso) if iso else None
    if when is None:
        return None
    return (when + timedelta(minutes=minutes)).isoformat(timespec="seconds")


@app.route("/api/bot_status")
def api_bot_status():
    """What the bot is doing and when it does it next: the defence, auto-fill,
    the auto-sweep, a sweep or a plan in progress, how fresh the analysis
    books are, and the day's creations. Times are ISO, in UTC."""
    from src.collector import PLACING_KEY
    from src.pacing import parse_iso
    from src.phases import is_phase

    db = get_db()
    now = datetime.now(timezone.utc)

    defend_at = db.get_setting("defend_last_at") or None
    minutes = defend_minutes(db)
    sweep_cfg = _auto_sweep_settings(db)
    fill_last = _epoch_iso(db.get_setting(AUTO_FILL_LAST_KEY))
    sweep_last = _epoch_iso(db.get_setting(AUTO_SWEEP_LAST_KEY))

    queue = db.pending_order_requests()
    try:
        pending = json.loads(db.get_setting("analysis_pending_actions") or "null")
    except ValueError:
        pending = None

    fresh_limit = now - timedelta(hours=AUTO_FILL_FRESH_HOURS)
    books = {"items": 0, "fresh": 0, "stale": 0, "never": 0, "oldest": None,
             "fresh_hours": AUTO_FILL_FRESH_HOURS}
    for name in _analysis_items(db):
        item_id = db.get_item_id(name)
        if item_id is None or is_phase(name):
            continue
        books["items"] += 1
        when = parse_iso(db.book_swept_at(item_id))
        if when is None:
            books["never"] += 1
        elif when >= fresh_limit:
            books["fresh"] += 1
        else:
            books["stale"] += 1
        if when is not None and (books["oldest"] is None or when.isoformat() < books["oldest"]):
            books["oldest"] = when.isoformat(timespec="seconds")

    return jsonify({
        "now": now.isoformat(timespec="seconds"),
        "defence": {"on": defending(db), "minutes": minutes, "last_at": defend_at,
                    "next_at": _plus_minutes(defend_at, minutes) if defending(db) else None},
        "auto_fill": {"on": (db.get_setting(AUTO_FILL_KEY) or "0") == "1",
                      "minutes": AUTO_FILL_EVERY_SECONDS // 60,
                      "last_at": fill_last,
                      "next_at": _plus_minutes(fill_last, AUTO_FILL_EVERY_SECONDS / 60),
                      "result": _json_setting(db, AUTO_FILL_RESULT_KEY)},
        "auto_sweep": {"on": sweep_cfg["auto_sweep"],
                       "minutes": sweep_cfg["auto_sweep_minutes"],
                       "last_at": sweep_last,
                       "next_at": _plus_minutes(sweep_last, sweep_cfg["auto_sweep_minutes"]),
                       "result": _json_setting(db, AUTO_SWEEP_RESULT_KEY)},
        "sweep": {"state": _json_setting(db, "sweep_state"),
                  "queued": len(queue),
                  "queued_names": [r["market_hash_name"] for r in queue[:5]]},
        "placing": {"state": _json_setting(db, PLACING_KEY),
                    "pending": len((pending or {}).get("actions") or []),
                    "pending_source": (pending or {}).get("source") or ("plan" if pending else None)},
        "books": books,
        "creates": _creates_status(db),
        "waiting": _why_waiting(db),
    })


def _auto_fill_loop() -> None:
    """Auto-sweep and auto-fill, checked every minute in the background of
    the web process."""
    import time as _time

    while True:
        _time.sleep(60)
        try:
            with app.app_context():
                automation_tick(get_db())
        except Exception as exc:  # noqa: BLE001 - the dashboard keeps serving
            log.warning("Background round failed: %s", exc)


def _creates_status(db) -> dict:
    from src import creates
    return creates.status(db)


@app.route("/api/analysis/apply", methods=["POST"])
def api_analysis_apply():
    """Hand the collector the plan that was on screen, and disarm.

    The actions are stored rather than recomputed, so what runs is what was
    approved: a plan rebuilt a minute later against a moved book would be a
    different plan, and nothing would say so. Arming is spent by use."""
    import json as _json

    _require_admin()
    db = get_db()
    from src.guard import tripped
    if tripped(db):
        abort(403, description="сработала защита от слива — выставление "
                               "выключено до ручного сброса")
    if (db.get_setting("analysis_armed") or "0") != "1":
        abort(403, description="не разрешено — включи разрешение на выставление")

    from src.creates import cap_places

    plan = api_analysis_plan().get_json()
    # New orders only up to what is left of the day's 200 creations, best
    # first; the rest wait for the reset rather than fail one by one.
    doing, deferred = cap_places(
        [a for a in plan["actions"] if a["kind"] != "keep"],
        plan["creates"]["left"])
    if not doing:
        return jsonify({"queued": 0, "deferred": deferred,
                        "note": ("создания на сегодня кончились — новые ордера "
                                 f"({deferred}) встанут после сброса"
                                 if deferred else "в плане нечего выполнять")})
    if not plan["can_place"]:
        abort(400, description=plan["placement"])

    db.set_setting("analysis_pending_actions", _json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "actions": doing}, ensure_ascii=False))
    db.set_setting("analysis_armed", "0")
    db.set_setting("analysis_apply_result", "")
    waiting = _why_waiting(db)
    log.warning("Plan queued for execution: %d action(s), dry run %s",
                len(doing), db.get_setting("analysis_dry_run", "1"))
    return jsonify({
        "queued": len(doing),
        "deferred": deferred,
        # Handed back so the page can show what went, rather than only how
        # many: a count is indistinguishable from a button that did nothing.
        "actions": doing,
        "dry_run": (db.get_setting("analysis_dry_run", "1") or "1") != "0",
        "waiting": waiting,
        "note": ("Сборщик занят: " + "; ".join(waiting)) if waiting else
                "Передано сборщику — выполнит в ближайшем цикле.",
    })


KIND_RU = {"place": "поставить", "raise": "поднять", "lower": "снизить",
           "cancel": "снять", "keep": "оставить"}


@app.route("/api/analysis/plan.xlsx")
def api_analysis_plan_xlsx():
    """The plan as an Excel file: the whole placement queue, the actions, and
    the settings it was built with - so two plans (say, before and after the
    pricing changed) can be laid side by side."""
    from src.executor import rank as rank_of
    from src.xlsx import workbook

    db = get_db()
    plan = api_analysis_plan().get_json()
    trace = getattr(g, "plan_trace", []) or []
    params = _analysis_params(db)

    def num(v, nd=2):
        return None if v is None or v == float("inf") else round(float(v), nd)

    def pct(v):
        return None if v is None else round(float(v) * 100, 2)

    queue, running = [], 0.0
    for n, row in enumerate(trace, 1):
        b = row["band"]
        cost = row["cost"]
        finite = cost is not None and cost != float("inf")
        if row["taken"] and finite:
            running += cost
        margin = b.margin_expected if b.margin_expected is not None else b.margin
        profit = ((b.lam or 0) * (b.bid or 0) * (margin or 0)) if b.bid else None
        queue.append([
            n, row["item"], num(b.float_min, 4), num(b.float_max, 4),
            "да" if row["taken"] else "нет", "да" if row["held"] else "",
            row["reason"] or "",
            num(b.bid), num(b.ceiling), pct(b.margin), pct(b.margin_expected),
            num(b.lam, 3), num(b.t_sell, 1), num(rank_of(b), 5), b.quantity,
            num(cost) if finite else None,
            num(running) if row["taken"] else None, num(profit),
            num(b.market), num(b.market_plain), num(b.market_then), b.sample,
            num(b.window, 0), pct(b.shift), b.priced_from, num(b.top),
        ])
    actions = [[KIND_RU.get(a["kind"], a["kind"]), a["item"],
                num(a["float_min"], 4), num(a["float_max"], 4),
                num(a.get("was")), num(a["price"]), num(a.get("ceiling")),
                a.get("quantity") or 1, num(a.get("rank"), 5), a.get("reason") or ""]
               for a in plan["actions"]]
    # Every order the plan takes down, priced on its own range now and under
    # the other settings: the old pricing, and the new one at each strength
    # of the careful median. What would keep it is then read off a row rather
    # than guessed.
    from dataclasses import replace

    from src.pricing import evaluate as price_one
    variants = [("старый", replace(params, adaptive=False)),
                ("новый, 1", replace(params, adaptive=True, careful=1.0)),
                ("новый, ½", replace(params, adaptive=True, careful=0.5)),
                ("новый, 0", replace(params, adaptive=True, careful=0.0))]
    dropped = getattr(g, "plan_dropped", {}) or {}
    scored = getattr(g, "plan_scored", {}) or {}
    context = getattr(g, "plan_context", {}) or {}
    cancels = []
    for a in plan["actions"]:
        if a["kind"] != "cancel":
            continue
        key = (a["item"], round(a["float_min"], 4), round(a["float_max"], 4))
        b = dropped.get(key) or scored.get(key)
        row = [a["item"], num(a["float_min"], 4), num(a["float_max"], 4),
               num(a.get("was") if a.get("was") is not None else a["price"]),
               num(a.get("ceiling")), a.get("reason") or ""]
        if b is not None:
            row += [num(b.ceiling), num(b.bid), num(b.top), pct(b.margin),
                    num(b.market), num(b.market_plain), b.sample,
                    num(b.window, 0)]
        else:
            row += [None] * 8
        ctx = context.get(a["item"])
        for _, vp in variants:
            if ctx is None:
                row += [None, None]
                continue
            v = price_one(key[1], key[2], ctx["sales"], ctx["orders"],
                          ctx["span"], ctx["depth"], vp, own=ctx["own"],
                          cache=ctx["cache"])
            row += [num(v.ceiling), "проходит" if v.take else (v.reason or "нет")]
        cancels.append(row)
    lim = plan["limits"]
    stamp = datetime.now(timezone.utc) + timedelta(hours=3)
    settings_rows = [
        ["выгружено (МСК)", stamp.strftime("%Y-%m-%d %H:%M")],
        ["расчёт цен", "новый" if params.adaptive else "старый"],
        ["осторожность медианы, погрешностей", params.careful],
        ["очередь расходится за, дней", params.queue_days],
        ["окно истории, дн", params.window_days],
        ["минимальная выборка", params.min_sample],
        ["минимальная маржа, %", pct(params.min_margin)],
        ["комиссия, %", pct(params.fee)],
        ["подешевел за неделю не больше, %", pct(params.max_drop)],
        ["баланс", lim.get("balance")],
        ["баланс с аккаунта", "да" if lim.get("balance_live") else "нет"],
        ["ордеров на N× баланса", lim.get("leverage")],
        ["предел суммы ордеров", lim.get("order_cap")],
        ["денег в сделках", lim.get("total_capital")],
        ["максимум ордеров", lim.get("max_orders")],
        ["максимум на предмет", lim.get("max_orders_per_item")],
        ["полос в очереди", len(queue)],
        ["в плане", sum(1 for r in queue if r[4] == "да")],
        ["новых к постановке", sum(1 for a in plan["actions"] if a["kind"] == "place")],
        ["предметов с фазой (не в плане)", plan.get("phase_items", 0)],
    ]
    body = workbook([
        ("Очередь",
         ["№", "предмет", "float от", "float до", "в плане", "уже стоит",
          "почему нет", "ставка $", "потолок $", "маржа %", "ожид. маржа %",
          "налив /сут", "продажа, дн", "ранг", "штук", "держит $", "итого $",
          "приб./сут $", "цена выхода $", "медиана $", "медиана без пересчёта $",
          "продаж в выборке", "окно, дн", "приведение %", "цена от",
          "соперник $"],
         queue,
         [5, 44, 9, 9, 8, 9, 40, 10, 10, 9, 11, 10, 10, 10, 6, 10, 10, 11,
          12, 10, 14, 10, 8, 11, 10, 10]),
        ("Действия",
         ["действие", "предмет", "float от", "float до", "было $", "цена $",
          "потолок $", "штук", "ранг", "почему"],
         actions, [11, 44, 9, 9, 9, 9, 10, 6, 10, 60]),
        ("Снимаемые",
         ["предмет", "float от", "float до", "наша цена $",
          "потолок при постановке $", "почему снимается",
          "потолок сейчас $", "ставка сейчас $", "соперник $", "маржа %",
          "цена выхода $", "медиана без поправки $", "продаж у верха",
          "окно, дн"]
         + [f"{n}: {c}" for n, _ in variants for c in ("потолок $", "итог")],
         cancels,
         [44, 9, 9, 10, 12, 50, 11, 11, 10, 8, 11, 12, 9, 7]
         + [11, 40] * len(variants)),
        ("Настройки", ["параметр", "значение"], settings_rows, [34, 20]),
    ])
    name = (f"plan_{stamp.strftime('%Y-%m-%d_%H%M')}_"
            f"{'new' if params.adaptive else 'old'}.xlsx")
    return Response(body, mimetype=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/api/analysis/cancel_lowest", methods=["POST"])
def api_analysis_cancel_lowest():
    """Take down the one standing order the plan values least.

    When fills spend the balance, CSFloat's allowance (ten times it) drops
    under what is already standing, and from then on it refuses every amend
    of every order - raising and lowering alike - as "insufficient balance".
    Freeing room by hand means choosing which order goes, and the plan's rank
    already says: one the plan would take down anyway first, then the lowest
    rank, the larger one first on a tie. {"preview": true} only names it."""
    import json as _json

    _require_admin()
    db = get_db()
    data = request.get_json(silent=True) or {}
    plan = api_analysis_plan().get_json()
    if not plan["can_cancel"]:
        abort(400, description="снятие ордеров не настроено в запросе постановки")
    held = [a for a in plan["actions"]
            if a["kind"] in ("keep", "raise", "lower", "cancel")
            and a.get("remote_id")]
    if not held:
        abort(404, description="снимать нечего: стоящих ордеров бота нет")

    def worth(a):
        # A band the plan would withdraw is worth nothing to it, whatever
        # number rides along.
        r = -1.0 if a["kind"] == "cancel" else float(a.get("rank") or 0.0)
        face = float(a.get("was") or a["price"]) * int(a.get("quantity") or 1)
        return (r, -face)

    victim = min(held, key=worth)
    price = float(victim.get("was") or victim["price"])
    rank_text = ("план и так снял бы его" if victim["kind"] == "cancel"
                 else f"ранг {float(victim.get('rank') or 0):.4f}")
    label = (f"{victim['item']} {victim['float_min']:.4f}–"
             f"{victim['float_max']:.4f} за ${price:.2f}"
             + (f" ×{victim['quantity']}" if int(victim.get("quantity") or 1) > 1
                else "") + f" ({rank_text})")
    if data.get("preview"):
        stronger = sorted((a for a in held if a is not victim), key=worth)
        return jsonify({
            "order": label, "item": victim["item"],
            "float_min": victim["float_min"], "float_max": victim["float_max"],
            "price": price, "quantity": int(victim.get("quantity") or 1),
            "rank": (None if victim["kind"] == "cancel"
                     else float(victim.get("rank") or 0.0)),
            "withdrawn": victim["kind"] == "cancel",
            "reason": victim.get("reason") or "",
            "held": len(held),
            # The next one up, so "weakest" reads against something.
            "next_rank": (None if not stronger or stronger[0]["kind"] == "cancel"
                          else float(stronger[0].get("rank") or 0.0)),
        })
    if db.get_setting("analysis_pending_actions"):
        abort(409, description="в очереди уже есть план — дождись, пока сборщик его выполнит")

    action = {
        "kind": "cancel", "item": victim["item"],
        "float_min": victim["float_min"], "float_max": victim["float_max"],
        "price": price, "ceiling": victim.get("ceiling") or price,
        "reason": f"снят кнопкой: самый слабый ордер ({rank_text}) — "
                  "освободить лимит CSFloat",
        "order_id": victim.get("order_id"), "remote_id": victim["remote_id"],
        "quantity": int(victim.get("quantity") or 1),
    }
    db.set_setting("analysis_pending_actions", _json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "source": "manual", "actions": [action]}, ensure_ascii=False))
    log.warning("Weakest order queued for cancel: %s", label)
    waiting = _why_waiting(db)
    return jsonify({"order": label, "waiting": waiting,
                    "note": f"Снимаю: {label}."
                    + (" Сборщик занят: " + "; ".join(waiting) if waiting else "")})


@app.route("/api/analysis/placement", methods=["GET", "POST"])
def api_analysis_placement():
    """The captured request that creates, amends and cancels an order.

    Saved only when sent deliberately: one endpoint was captured from the
    browser and the rest follow its shape, and the one being guessed at is the
    one that spends money."""
    import json as _json

    from src.placement import (CONFIRMED, PLACEMENT_KEY, SUGGESTED, Spec,
                               describe, load, render)

    db = get_db()
    if request.method == "POST":
        _require_admin()
        data = request.get_json(silent=True) or {}
        known = set(Spec().__dict__)
        spec = Spec(**{k: str(v).strip() for k, v in data.items() if k in known})
        problems = []
        for label, body in (("создания", spec.create_body),
                            ("правки", spec.update_body),
                            ("отмены", spec.cancel_body)):
            if not body:
                continue
            try:
                render(body, name="проверка", price=1.23,
                       float_min=0.15, float_max=0.18)
            except Exception as exc:  # noqa: BLE001 - the message is the point
                problems.append(f"тело {label}: {exc}")
        if problems:
            return jsonify({"saved": False, "problems": problems}), 400
        db.set_setting(PLACEMENT_KEY, _json.dumps(spec.as_dict(),
                                                  ensure_ascii=False))
        # Saving a new request shape disarms: what was approved was the old one.
        db.set_setting("analysis_armed", "0")
        log.info("Placement spec saved (%s %s)", spec.create_method,
                 spec.create_path)
        return jsonify({"saved": True, "spec": spec.as_dict(),
                        "describe": describe(spec)})

    spec = load(db.get_setting(PLACEMENT_KEY))
    # Rendered from what is stored, so the request that would go out can be
    # read off the page and compared with the browser's own - "the code was
    # updated" and "the saved shape was updated" are different things, and
    # only the second one is what gets sent.
    preview, warn = {}, []
    sample = dict(name="★ Hand Wraps | Duct Tape (Field-Tested)", price=49.0,
                  float_min=0.1501, float_max=0.158)
    for label, body in (("создание", spec.create_body),
                        ("правка", spec.update_body)):
        if not body:
            continue
        try:
            preview[label] = render(body, **sample)
        except Exception as exc:  # noqa: BLE001 - the message is the point
            preview[label] = {"ошибка": str(exc)}
    amend = preview.get("правка")
    if isinstance(amend, dict) and "min_float" not in amend:
        warn.append(
            "тело «правка» не передаёт границы float. Снятый с сайта PATCH "
            "шлёт весь набор полей ордера — если CSFloat читает его как "
            "замену, правка цены снимет фильтр по float, и ордер начнёт "
            "скупать любой износ")
    for label, body in preview.items():
        if isinstance(body, dict) and "price" in body and "max_price" not in body:
            warn.append(
                f"тело «{label}» шлёт price — CSFloat отвечает «orders must "
                f"have a max price above 0»; поле называется max_price")

    return jsonify({
        "spec": spec.as_dict(),
        "preview": preview,
        "warnings": warn,
        "suggested": SUGGESTED.as_dict(),
        "confirmed": sorted(CONFIRMED),
        "describe": describe(spec),
        "configured": spec.can_place,
    })


@app.route("/api/analysis/journal")
def api_analysis_journal():
    """What the bot has actually done, newest first.

    Separate from the plan: the plan is what it would do, and the two answer
    different questions. "Did it place anything, and did it hold on to it" is
    only answerable from the log."""
    db = get_db()
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 2000)
    except (TypeError, ValueError):
        limit = 200
    name = (request.args.get("item") or "").strip() or None
    include_dry = (request.args.get("dry") or "1") != "0"
    # A period rather than a row count: "what happened today" is the question
    # the page is opened with, and three hundred rows is a day on a busy list
    # and a month on a quiet one.
    since = None
    try:
        hours = float(request.args.get("hours") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    if hours > 0:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)) \
            .replace(microsecond=0).isoformat()
    events = db.order_events(limit=limit, name=name, include_dry=include_dry,
                             since=since)
    _infer_fills(db, events)

    # The same band, over time: the rows for one order are its history, and
    # the count is what says "it has been outbid nine times today".
    live = db.our_orders()
    by_name: dict[str, str] = {}
    for row in live:
        nm = db.item_name(int(row["item_id"]))
        if nm:
            by_name[nm] = nm
    # Counted apart: an order the bot placed and one it merely found are not
    # the same thing, and only the first is one it will defend.
    managed = [r for r in live if r["state"] in ("planned", "live")]
    manual = db.our_orders(live_only=False)
    manual = [r for r in manual if r["state"] == "manual"]
    return jsonify({
        "events": events,
        "held": len(managed),
        "manual": len(manual),
        "sync": _json_setting(db, "orders_sync_result"),
        "sync_at": db.get_setting("orders_sync_at") or None,
        "sync_pending": db.get_setting("orders_sync_requested") == "1",
        "items": sorted({e["market_hash_name"] for e in events}),
        "since": since,
        "truncated": len(events) >= limit,
        "defend": defending(db),
        "defend_minutes": defend_minutes(db),
        "defend_at": db.get_setting("defend_last_at") or None,
    })


def _infer_fills(db, events: list[dict]) -> None:
    """Tell a filled order from one taken down, where the trades can.

    The account's list only says an order is no longer there, so the sync
    logs every disappearance as "taken down or filled" - and most of them are
    fills. A purchase on the same item, at a float inside the order's range,
    made in the days before the order vanished, says which: the event is
    shown as filled, with the deal that filled it. Each trade answers for one
    order only."""
    gone = [e for e in events
            if e.get("kind") == "cancel" and e.get("source") == "sync"
            and e.get("ok")]
    if not gone:
        return
    try:
        trades = db.all_trades()
    except Exception:  # noqa: BLE001 - an older DB has no trades yet
        return
    buys: dict[str, list[dict]] = {}
    for t in trades:
        state = str(t.get("state") or "").lower()
        if t.get("role") == "sell" or "cancel" in state or "fail" in state:
            continue
        if t.get("float_value") is None or not t.get("market_hash_name"):
            continue
        buys.setdefault(t["market_hash_name"], []).append(t)
    used: set[str] = set()
    for e in sorted(gone, key=lambda e: e["at"]):
        lo, hi = e.get("float_min"), e.get("float_max")
        if lo is None or hi is None:
            continue
        try:
            at = datetime.fromisoformat(str(e["at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        for t in buys.get(e.get("market_hash_name") or "", []):
            if t["trade_id"] in used:
                continue
            if not (float(lo) - 1e-6 <= float(t["float_value"]) <= float(hi) + 1e-6):
                continue
            try:
                made = datetime.fromisoformat(
                    str(t.get("created_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if made.tzinfo is None:
                made = made.replace(tzinfo=timezone.utc)
            if not (at - timedelta(days=3) <= made <= at + timedelta(hours=1)):
                continue
            used.add(t["trade_id"])
            e["kind"] = "fill"
            e["inferred"] = True
            price = t.get("price")
            e["reason"] = ("куплено: сделка"
                           + (f" ${float(price):.2f}" if price is not None else "")
                           + f", float {float(t['float_value']):.4f}")
            break


@app.route("/api/analysis/sync", methods=["POST"])
def api_analysis_sync():
    """Ask the collector to compare our record against the account.

    The web process never talks to CSFloat - the collector owns the routes and
    the cookie - so this sets the request and the page waits for the answer,
    the same contract the book sweep uses."""
    _require_admin()
    db = get_db()
    db.set_setting("orders_sync_requested", "1")
    waiting = _why_waiting(db)
    return jsonify({
        "queued": True, "waiting": waiting,
        "note": ("Сборщик занят: " + "; ".join(waiting)) if waiting else
                "Сверяю с аккаунтом — ответ через несколько секунд.",
    })


@app.route("/api/analysis/positions")
def api_analysis_positions():
    """Every order we hold, and where it stands in its own book right now.

    The plan says what would be done and the journal says what was done;
    neither answers "am I still first". That needs the book read against the
    item we would actually get - the one at our order's top float - because a
    rival scoped below it takes better items, not ours.
    """
    from src.executor import ahead_of
    from src.holdings import strip_own
    from src.ladder import rival_bid

    from src.collector import VERDICTS_KEY

    db = get_db()
    try:
        verdicts = json.loads(db.get_setting(VERDICTS_KEY) or "{}")
    except ValueError:
        verdicts = {}
    rows = []
    books: dict[int, list] = {}
    raw_books: dict[int, list] = {}
    for row in db.our_orders(live_only=False):
        if row["state"] not in ("planned", "live", "manual"):
            continue
        item_id = int(row["item_id"])
        name = db.item_name(item_id) or "?"
        if item_id not in books:
            raw = db.buy_orders(item_id)
            raw_books[item_id] = raw
            books[item_id] = strip_own(raw, db.our_orders(item_id))
        book = books[item_id]
        lo, hi = float(row["float_min"]), float(row["float_max"])
        price = float(row["price"])

        # Who would take the item we actually get - the one at our top - at
        # our price or above; the same reading the defence acts on. At or
        # above, not strictly above: an order matching our price is filled
        # before ours or after it depending on who placed first, which the
        # book does not say, and counting it is the reading that does not
        # flatter us.
        above = ahead_of(book, lo, hi, price)
        top = rival_bid(book, hi)
        ahead = sum(int(o.get("qty") or 1) for o in above)
        # The ceiling stored with the order is the one it was last sent with;
        # the defence re-scores it every pass, and the one it last arrived at
        # is what the next answer is held to.
        verdict = verdicts.get(str(row["id"]))
        ceiling = float(verdict["ceiling"]) if verdict and verdict.get(
            "ceiling") is not None else float(row["ceiling"])
        rows.append({
            "id": row["id"], "item": name, "float_min": lo, "float_max": hi,
            "price": price, "ceiling": ceiling,
            "placed_ceiling": float(row["ceiling"]),
            "room": round(ceiling - price, 2),
            "verdict": verdict,
            "quantity": int(row.get("quantity") or 1),
            "state": row["state"], "remote_id": row["remote_id"],
            "placed_at": row["placed_at"], "note": row["note"],
            "top": top, "ahead": ahead,
            "first": ahead == 0,
            # Whether the book actually names our order. When it does there is
            # nothing to guess at; when it does not, ours was matched by price
            # and bounds and a rival at the same price could be taken for it.
            "seen_in_book": bool(row["remote_id"]) and any(
                str(o.get("order_id") or "") == str(row["remote_id"])
                for o in raw_books.get(item_id, [])),
            "swept_at": book[0]["fetched_at"] if book else None,
            "book": len(book),
        })
    rows.sort(key=lambda r: (r["first"], r["item"], r["float_min"]))
    named = sum(1 for b in raw_books.values() for o in b if o.get("order_id"))
    total = sum(len(b) for b in raw_books.values())
    return jsonify({
        "orders": rows,
        # What CSFloat holds against its allowance: every standing order,
        # the bot's and the hand-placed alike, at its price times its count.
        "face": round(sum(r["price"] * r["quantity"] for r in rows
                          if r["state"] != "planned"), 2),
        # Ten times the balance typed on the analysis page - the real one
        # falls as fills spend it, and then amends are refused.
        "allowance": _analysis_limits(db).as_dict().get("allowance"),
        "balance": _analysis_limits(db).balance,
        "balance_live": _analysis_limits(db).balance_live,
        "order_cap": _analysis_limits(db).as_dict().get("order_cap"),
        "outbid": sum(1 for r in rows if not r["first"]),
        # One sweep answers it: if the book names its orders we can point at
        # ours exactly, and the price-and-bounds guess retires.
        "book_named": named,
        "book_rows": total,
        "checking": bool(db.pending_order_requests()),
    })


@app.route("/api/analysis/positions/refresh", methods=["POST"])
def api_analysis_positions_refresh():
    """Re-read the book of every item we hold an order on.

    Asked for by hand: knowing whether a standing order has been outbid is not
    something the defence should be the only route to, because the defence
    also acts, and looking is not the same decision as answering."""
    _require_admin()
    db = get_db()
    names = set()
    for row in db.our_orders(live_only=False):
        if row["state"] not in ("planned", "live", "manual"):
            continue
        name = db.item_name(int(row["item_id"]))
        if name:
            names.add(name)
    queued = [n for n in sorted(names) if db.request_orders(n)]
    waiting = _why_waiting(db)
    return jsonify({
        "queued": queued, "waiting": waiting,
        "note": ("Сборщик занят: " + "; ".join(waiting)) if waiting else
                (f"Читаю стаканы по {len(queued)} предмет(ам)."
                 if queued else "Нет ордеров, по которым смотреть."),
    })


@app.route("/journal")
def journal_page():
    return render_template("journal.html",
                           admin_required=bool(config.web.admin_token))


@app.route("/profit")
def profit_page():
    return render_template("profit.html",
                           admin_required=bool(config.web.admin_token))


@app.route("/api/profit")
def api_profit():
    """Earnings, worked out from the account's trades (see
    `src.profit_report`)."""
    from src import profit_report

    db = get_db()
    try:
        days = float(request.args.get("days") or 0)
    except (TypeError, ValueError):
        days = 0.0
    out = profit_report.build(db, _analysis_params(db).fee, days)
    out.update({
        "sync": _json_setting(db, "trades_sync_result"),
        "sync_at": db.get_setting("trades_sync_at") or None,
        "sync_pending": db.get_setting("trades_sync_requested") == "1",
    })
    return jsonify(out)


def _profit_settings(db) -> dict:
    from src import profit_report
    return profit_report.settings(db)


@app.route("/api/profit/settings", methods=["POST"])
def api_profit_settings():
    from src import profit_report

    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    errors = []
    if "since" in data:
        since = str(data.get("since") or "").strip()
        if since:
            try:
                datetime.strptime(since, "%Y-%m-%d")
            except ValueError:
                errors.append(f"дата «{since}» — нужен формат ГГГГ-ММ-ДД")
                since = None
        if since is not None:
            db.set_setting(profit_report.SINCE_KEY, since)
    if "estimate_days" in data:
        lo, hi = profit_report.DAYS_BOUNDS
        try:
            days = int(float(str(data["estimate_days"]).replace(",", ".")))
        except (TypeError, ValueError):
            days = None
        if days is None or not lo <= days <= hi:
            errors.append(f"срок оценки — целое от {lo} до {hi} дней")
        else:
            db.set_setting(profit_report.DAYS_KEY, str(days))
    conf = _profit_settings(db)
    return jsonify({"since": conf["since"], "estimate_days": conf["estimate_days"],
                    "errors": errors, "error": "; ".join(errors)}), \
        (400 if errors else 200)


@app.route("/api/profit/exclude", methods=["POST"])
def api_profit_exclude():
    """Take trades out of the earnings, or put them back. Several at once: a
    closed deal is its purchase and its sale, and leaving either in would
    show up as a half of it somewhere else."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    ids = data.get("trade_ids") or ([data["trade_id"]] if data.get("trade_id") else [])
    ids = [str(x) for x in ids if str(x).strip()]
    if not ids:
        abort(400, description="trade_id не указан")
    db = get_db()
    excluded = _profit_settings(db)["excluded"]
    if data.get("excluded", True):
        excluded |= set(ids)
    else:
        excluded -= set(ids)
    from src import profit_report
    db.set_setting(profit_report.EXCLUDED_KEY, json.dumps(sorted(excluded)))
    return jsonify({"excluded": len(excluded)})


@app.route("/api/profit/sync", methods=["POST"])
def api_profit_sync():
    """Ask the collector to read the account's trades now."""
    _require_admin()
    db = get_db()
    db.set_setting("trades_sync_requested", "1")
    waiting = _why_waiting(db)
    return jsonify({
        "queued": True, "waiting": waiting,
        "note": ("Сборщик занят: " + "; ".join(waiting)) if waiting else
                "Читаю сделки аккаунта — ответ через несколько секунд.",
    })


@app.route("/api/analysis/params", methods=["POST"])
def api_analysis_params():
    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    rejected = []
    # One loop over all three groups: a group left out of this list is a field
    # the page shows, accepts, and never saves.
    for keys, bounds in ((ANALYSIS_KEYS, ANALYSIS_BOUNDS),
                         (LIMIT_KEYS, LIMIT_BOUNDS),
                         (SCREEN_KEYS, SCREEN_BOUNDS)):
        for key, attr, cast in keys:
            if key not in data:
                continue
            raw = str(data[key]).strip().replace(",", ".")
            try:
                value = cast(float(raw))
            except (TypeError, ValueError):
                rejected.append(f"{key}: '{raw}' — не число")
                continue
            lo, hi = bounds[attr]
            if not lo <= value <= hi:
                rejected.append(f"{key}: {raw} вне диапазона {lo}–{hi}")
            db.set_setting(key, str(cast(min(max(value, lo), hi))))
    return jsonify({"params": _analysis_params(db).__dict__,
                    "limits": _analysis_limits(db).as_dict(),
                    "screen": _analysis_screen(db).as_dict(),
                    "rejected": rejected})


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


@app.route("/analysis")
def analysis_page():
    return render_template("analysis.html",
                           admin_required=bool(config.web.admin_token))


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
        pace_mult = min(max(float(db.get_setting("pace_multiplier") or 1.0), 1.0), PACE_MAX)
    except (TypeError, ValueError):
        pace_mult = 1.0

    try:
        quota_factor = max(float(db.get_setting("quota_factor") or 1.0), 1.0)
    except (TypeError, ValueError):
        quota_factor = 1.0
    # The same planner the collector schedules by (src/schedule.py), not a
    # copy of it: the copies had begun to disagree.
    from src import schedule as sch
    stretch = max(pace_mult, quota_factor)   # as Collector.stretch_factor
    settings_ = sch.read_settings(db, gmin)
    plan_block = None
    intervals: list[float] = []
    if adaptive_on:
        plans, settings_ = sch.plan_from_db(
            db, gmin, config.polling.gap_warning_min_overlap)
        tier_demand = sch.demand(plans)
        capacity_day = sch.capacity_per_day(spacing)
        rest_x = sch.rest_stretch(tier_demand, capacity_day)
        tiers, groups = {}, {}
        for p in plans:
            minutes = p.minutes * (rest_x if p.tier == "rest" else 1.0) * stretch
            intervals.append(minutes)
            per_day = 1440.0 / max(minutes, 0.1)
            for bucket, key in ((tiers, p.tier),
                                (groups, sch.liquidity_group(p.rate))):
                row = bucket.setdefault(key, {"items": 0, "per_day": 0.0,
                                              "expected": 0.0})
                row["items"] += 1
                row["per_day"] += per_day
                row["expected"] += p.expected
        plan_block = {
            "tiers": [{"tier": t, "label": sch.TIER_LABELS[t],
                       "items": tiers.get(t, {}).get("items", 0),
                       "per_day": round(tiers.get(t, {}).get("per_day", 0.0)),
                       "ceiling_minutes": settings_.ceiling(t)}
                      for t in sch.TIERS],
            "groups": [{"group": g, "label": sch.GROUP_LABELS[g],
                        "items": groups.get(g, {}).get("items", 0),
                        "per_day": round(groups.get(g, {}).get("per_day", 0.0)),
                        "sales_per_poll": round(
                            groups[g]["expected"] / groups[g]["items"], 1)
                        if groups.get(g) else None}
                       for g in ("liquid", "middle", "thin")],
            "demand_day": round(sum(1440.0 / max(m, 0.1) for m in intervals)),
            "capacity_day": round(capacity_day),
            "rest_stretch": round(rest_x, 2),
        }
    else:
        for it in active:
            lo = it.get("interval_min_minutes")
            hi = it.get("interval_max_minutes")
            intervals.append(max(((lo or gmin) + (hi or gmax)) / 2.0 * stretch, 0.1))
    reqs_per_min = sum(1.0 / m for m in intervals)
    budget = 60.0 / max(spacing, 0.01)

    now = datetime.now(timezone.utc)
    hour_iso = (now - timedelta(hours=1)).replace(microsecond=0).isoformat()
    day_iso = (now - timedelta(hours=24)).replace(microsecond=0).isoformat()

    # Global 429 pause, persisted by the collector process.
    cooldown_until = db.get_setting("cooldown_until") or None
    cooldown_left = _cooldown_left(db)
    if cooldown_until and not cooldown_left:
        try:
            datetime.fromisoformat(cooldown_until)
        except ValueError:
            cooldown_until = None

    stats_hour = db.poll_stats(hour_iso)
    stats_day = db.poll_stats(day_iso)
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

    # Why is nothing being polled? A pause with every route parked and no
    # direct connection is a dead stop, not a rate limit waiting to pass — and
    # it needs a different action from the user.
    routes = json.loads(db.get_setting("proxy_state") or "[]")
    usable = [r for r in routes if r.get("available")]
    has_direct = any(r.get("direct") for r in routes)
    all_parked = bool(routes) and not usable

    # With every route parked the 429 cooldown may well have expired, yet
    # nothing can be sent until the first route comes back. Reporting "пауза:
    # нет" next to a banner saying collection has stopped is not a pause the
    # user can act on — the real wait is whichever route frees up first.
    if all_parked:
        waits = [max(r.get("parked_sec") or 0, r.get("cooldown_sec") or 0)
                 for r in routes]
        cooldown_left = max(cooldown_left, min(waits) if waits else 0)

    # One-line health verdict for the dashboard.
    if stats_hour["auth_error"]:
        state, state_text = "auth", "Ошибка авторизации — обнови cookie в .env"
    elif all_parked and not has_direct:
        state, state_text = "blocked", (
            "Сбор остановлен: все прокси в карантине, а свой IP сервера выключен. "
            "Включи «использовать и собственный IP сервера» в блоке «Прокси» — "
            "иначе запросы делать не с чего.")
    elif all_parked:
        # Direct is in the pool but unavailable too — its own quota or 429.
        state, state_text = "blocked", (
            "Нет доступных маршрутов: прокси в карантине, а у IP сервера "
            f"кончилась квота. Осталось {cooldown_left / 60:.0f} мин.")
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
            "routes": routes,
            "proxies_text": db.get_setting("proxies") or "",
            **_key_ring_info(db),
            "use_direct": (db.get_setting("use_direct", "1") or "1") != "0",
            "rotating_daily_limit": int(db.get_setting("rotating_daily_limit")
                                        or ROTATING_DEFAULT_LIMIT),
            "account_ip_block_at": db.get_setting("account_ip_block_at") or None,
            "quarantine_clearing": bool(db.get_setting("quarantine_clear_requested")),
            "quota_limit": _num_setting(db, "rl_limit"),
            "quota_remaining": _num_setting(db, "rl_remaining"),
            "quota_usable": _num_setting(db, "rl_usable"),
            "quota_reset": _num_setting(db, "rl_reset"),
            "quota_factor": float(db.get_setting("quota_factor") or 1.0),
            "adaptive_enabled": adaptive_on,
            "ceilings": {t: settings_.ceiling(t) for t in ("orders", "analysis", "rest")},
            "plan": plan_block,
            "pace_multiplier": round(pace_mult, 2),
            "pace_max": PACE_MAX,
            "avg_interval_minutes": round(sum(intervals) / len(intervals), 1) if intervals else None,
            **_usage_forecast(db, reqs_per_min, day_iso, stats_day),
            "last_429_headers": db.get_setting("last_429_headers") or "",
            "last_429_body": db.get_setting("last_429_body") or "",
            "last_429_at": db.get_setting("last_429_at") or None,
            "cooldown_until": cooldown_until,
            "cooldown_remaining_sec": round(cooldown_left),
            "cooldown_consecutive": int(db.get_setting("cooldown_consecutive") or 0),
            "state": state,
            "state_text": state_text,
            "routes_total": len(routes),
            "routes_usable": len(usable),
            "has_direct": has_direct,
            "stale_minutes": stale_min,
            "last_update": last_ok,
            "stats_hour": stats_hour,
            "stats_day": stats_day,
            "gap_warnings": db.gap_warnings(day_iso, limit=50),
            "recent": db.recent_poll_log(limit=30),
        }
    )


def _usage_forecast(db, reqs_per_min: float, day_iso: str,
                    stats_day: dict | None = None) -> dict:
    """What the current schedule costs: requests and traffic per day/month.

    Request volume comes from the same per-item intervals the collector uses.
    Traffic is the measured average response size when there is one — a metered
    proxy bills for exactly these bytes — and a sample-based estimate before
    the first poll has been logged."""
    sizes = db.response_size_stats(day_iso)
    # Средний размер — по последним ответам, а не за сутки. Способ замера
    # менялся (разжатое тело -> провод, разница в 7 раз), и окно по времени
    # ещё долго отдаёт голос старым строкам, показывая трафик, которого нет.
    # Сумма за сутки ниже остаётся суммой за сутки: это факт, а не оценка.
    recent = db.recent_response_size()
    measured = recent["avg_bytes"] if recent["samples"] else None
    avg_bytes = measured if measured else ASSUMED_RESPONSE_BYTES

    per_day = reqs_per_min * 1440.0
    bytes_day = per_day * avg_bytes
    # What the schedule predicts vs what actually went out: they diverge after a
    # settings change, a restart, or a stretch of downtime.
    actual = stats_day.get("total", 0) if stats_day else 0
    from src.traffic import KINDS, LABELS
    try:
        measured_kinds = db.traffic_since(day_iso[:13] + ":00:00+00:00"
                                          if len(day_iso) >= 13 else day_iso)
    except Exception:  # noqa: BLE001 - an older database, or a stand-in
        measured_kinds = {}
    by_kind = [{"kind": k, "label": LABELS[k],
                "requests": measured_kinds.get(k, {}).get("requests", 0),
                "mb": round(measured_kinds.get(k, {}).get("bytes", 0) / 1_048_576, 1)}
               for k in KINDS]
    return {
        "traffic_by_kind": by_kind,
        "requests_day_actual": actual,
        "traffic_day_actual_mb": round(sizes["total_bytes"] / 1_048_576, 1),
        "requests_per_day": round(per_day),
        "requests_per_month": round(per_day * 30),
        "avg_response_bytes": round(avg_bytes),
        "response_samples": recent["samples"],
        "response_measured": bool(measured),
        "traffic_day_mb": round(bytes_day / 1_048_576, 1),
        "traffic_month_mb": round(bytes_day * 30 / 1_048_576, 1),
    }


@app.route("/api/load/key_proxies", methods=["POST"])
def api_set_key_proxies():
    """The analysis keys' own addresses (keys.txt). Empty puts them back on
    the main pool. The collector applies the list within ~30 s."""
    _require_admin()
    from src.proxies import parse_proxy_list, validate_proxy

    data = request.get_json(silent=True) or {}
    urls, bad, seen = [], [], set()
    for line in parse_proxy_list(str(data.get("key_proxies") or "")):
        ok, why = validate_proxy(line)
        if not ok:
            bad.append(f"{line} — {why}")
        elif line not in seen:
            seen.add(line)
            urls.append(line)
    if bad:
        abort(400, description="Некорректные строки:\n" + "\n".join(bad[:10]))
    db = get_db()
    db.set_setting("key_proxies", "\n".join(urls))
    log.info("Key proxy list updated via web: %d address(es)", len(urls))
    return jsonify({"ok": True, "count": len(urls)})


@app.route("/api/load/main_proxies", methods=["POST"])
def api_set_main_proxies():
    """The main key's own addresses. Empty: it takes a few fixed ones from the
    main list. The collector applies the list within ~30 s."""
    _require_admin()
    from src.proxies import parse_proxy_list, split_proxy_flags, validate_proxy

    data = request.get_json(silent=True) or {}
    urls, bad, seen = [], [], set()
    for line in parse_proxy_list(str(data.get("main_proxies") or "")):
        ok, why = validate_proxy(line)
        if not ok:
            bad.append(f"{line} — {why}")
        elif split_proxy_flags(line)[1]:
            bad.append(f"{line} — ротационный прокси: каждый запрос с нового IP, "
                       "для главного ключа это ровно то, за что CSFloat блокирует")
        elif line not in seen:
            seen.add(line)
            urls.append(line)
    if bad:
        abort(400, description="Некорректные строки:\n" + "\n".join(bad[:10]))
    db = get_db()
    db.set_setting("main_proxies", "\n".join(urls))
    log.info("Main key proxy list updated via web: %d address(es)", len(urls))
    return jsonify({"ok": True, "count": len(urls)})


def _key_ring_info(db) -> dict:
    """What the load page says about the analysis keys: how many there are,
    and where their addresses come from. The keys themselves never leave the
    file."""
    from src.keyring import read_keys
    keys = read_keys(config.http.keys_file) if config.http.keys_file else []
    try:
        routes = json.loads(db.get_setting("key_proxy_state") or "[]")
    except ValueError:
        routes = []
    try:
        ring = json.loads(db.get_setting("key_ring_state") or "[]")
    except ValueError:
        ring = []
    text = db.get_setting("key_proxies") or ""
    return {"keys": len(keys), "keys_file": bool(config.http.keys_file),
            "key_proxies_text": text, "key_routes": routes,
            "key_ring": ring,
            "main_key_state": _json_setting(db, "main_key_state") or [],
            "main_key_routes": _json_setting(db, "main_key_routes") or [],
            "main_proxies_text": db.get_setting("main_proxies") or "",
            "main_proxy_state": _json_setting(db, "main_proxy_state") or [],
            "main_key": bool(config.http.api_key)}


@app.route("/api/load/proxies", methods=["POST"])
def api_set_proxies():
    """Replace the proxy list (bulk paste, one per line). The collector applies
    it within ~30s, keeping the quota state of routes that stay."""
    _require_admin()
    from src.proxies import parse_proxy_list, validate_proxy

    data = request.get_json(silent=True) or {}
    raw = data.get("proxies")
    db = get_db()

    # Validate everything first, so a bad payload never half-applies.
    urls = None
    if raw is not None:
        urls, bad = [], []
        seen = set()
        for line in parse_proxy_list(str(raw)):
            ok, why = validate_proxy(line)
            if not ok:
                bad.append(f"{line} — {why}")
            elif line not in seen:
                seen.add(line)
                urls.append(line)
        if bad:
            abort(400, description="Некорректные строки:\n" + "\n".join(bad[:10]))

    count = len(urls) if urls is not None \
        else len(parse_proxy_list(db.get_setting("proxies") or ""))
    if "use_direct" in data and not data["use_direct"] and not count:
        abort(400, description="Нельзя отключить собственный IP, пока не задан "
                               "ни один прокси — иначе запросы делать неоткуда.")

    limit = None
    if "rotating_daily_limit" in data:
        try:
            limit = int(data["rotating_daily_limit"])
        except (TypeError, ValueError):
            abort(400, description="Дневной лимит ротационного прокси — целое число.")
        if not 1 <= limit <= 20000:
            abort(400, description="Дневной лимит должен быть от 1 до 20000.")

    if urls is not None:
        db.set_setting("proxies", "\n".join(urls))
    if "use_direct" in data:
        db.set_setting("use_direct", "1" if data["use_direct"] else "0")
    if limit is not None:
        db.set_setting("rotating_daily_limit", str(limit))

    log.info("Proxy list updated via web: %d proxy/proxies, direct=%s",
             count, db.get_setting("use_direct"))
    return jsonify({"ok": True, "count": count})


@app.route("/api/load/quarantine", methods=["POST"])
def api_clear_quarantine():
    """Lift the account-IP quarantine early.

    The park is the bot's own caution, not a block by CSFloat, so the operator
    can overrule it — the routes themselves were never refused. Applied by the
    collector, which owns the pool."""
    _require_admin()
    db = get_db()
    if not db.get_setting("account_ip_block_at"):
        return jsonify({"ok": True, "note": "Карантина сейчас нет."})
    db.set_setting("quarantine_clear_requested", utcnow_iso())
    log.warning("Quarantine clear requested from the dashboard")
    return jsonify({
        "ok": True,
        "note": "Карантин снимется в течение ~30 секунд. Следи за «429 за час» — "
                "если жалоба повторится, маршруты снова встанут на 6 часов.",
    })


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
                  "min_seconds_between_requests", "adaptive_max_minutes",
                  "ceiling_orders_minutes", "ceiling_analysis_minutes",
                  "ceiling_rest_minutes"):
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
    store("ceiling_orders_minutes", "ceiling_orders_minutes", 15, 10080,
          "Потолок: стоит наш ордер")
    store("ceiling_analysis_minutes", "ceiling_analysis_minutes", 15, 10080,
          "Потолок: в списке анализа")
    store("ceiling_rest_minutes", "ceiling_rest_minutes", 15, 10080,
          "Потолок: остальные")
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
            "digest": _digest_settings(db),
        }
    )


def _digest_settings(db) -> dict:
    from src import digest
    return digest.as_dict(db)


# Tables worth a line on the storage panel, biggest first in practice.
STORAGE_TABLES = ("sales", "poll_log", "order_events", "buy_orders",
                  "listing_depth", "book_history", "trades", "our_orders", "items")


@app.route("/api/settings/storage")
def api_settings_storage():
    """How big the database is, and where the room goes.

    Rows are counted by the largest rowid, which is instant on a table of
    millions; deleted rows make it an upper bound, hence "≈"."""
    import shutil

    db = get_db()
    path = Path(config.db_path)

    def size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    main = size(path)
    wal = size(Path(str(path) + "-wal"))
    backups = 0
    try:
        backups = sum(f.stat().st_size for f in Path(config.backups_dir).glob("*")
                      if f.is_file())
    except OSError:
        pass
    try:
        disk = shutil.disk_usage(path.parent)
        free, total = disk.free, disk.total
    except OSError:
        free = total = None
    tables = []
    # Bytes per table where SQLite was built with the dbstat table, which is
    # what says where the room actually goes; rows always.
    sizes: dict[str, int] = {}
    try:
        for row in db.conn.execute(
                "SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name"):
            sizes[row["name"]] = int(row["b"] or 0)
    except Exception:  # noqa: BLE001 - not compiled in: rows alone
        sizes = {}
    for name in STORAGE_TABLES:
        try:
            row = db.conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
            tables.append({"table": name, "rows": int(row["n"] or 0),
                           "bytes": sizes.get(name)})
        except Exception:  # noqa: BLE001 - an older DB lacks a table
            continue
    from src.collector import PRUNE_RESULT_KEY, VACUUM_RESULT_KEY
    from src.settings import keep_days
    return jsonify({"db": main, "wal": wal, "backups": backups,
                    "disk_free": free, "disk_total": total, "tables": tables,
                    "keep_days": keep_days(db),
                    "prune": _json_setting(db, PRUNE_RESULT_KEY),
                    "prune_pending": db.get_setting("db_prune_requested") == "1",
                    "vacuum": _json_setting(db, VACUUM_RESULT_KEY),
                    "vacuum_pending": db.get_setting("db_vacuum_requested") == "1"})


@app.route("/api/settings/storage", methods=["POST"])
def api_settings_storage_save():
    """How many days of logs to keep, and housekeeping on demand - both done
    by the collector, which is the database's main writer."""
    from src.settings import KEEP_DAYS_BOUNDS, KEEP_DAYS_KEY, keep_days

    _require_admin()
    data = request.get_json(silent=True) or {}
    db = get_db()
    if "keep_days" in data:
        try:
            value = float(str(data["keep_days"]).replace(",", "."))
        except (TypeError, ValueError):
            abort(400, description="дней — число")
        lo, hi = KEEP_DAYS_BOUNDS
        db.set_setting(KEEP_DAYS_KEY, str(min(max(value, lo), hi)))
    if data.get("prune"):
        db.set_setting("db_prune_requested", "1")
    if data.get("vacuum"):
        db.set_setting("db_vacuum_requested", "1")
    return jsonify({"keep_days": keep_days(db),
                    "prune_pending": db.get_setting("db_prune_requested") == "1",
                    "vacuum_pending": db.get_setting("db_vacuum_requested") == "1"})


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
    if "digest_time" in data:
        t = (data.get("digest_time") or "").strip()
        if t:
            try:
                hh, mm = [int(x) for x in t.split(":")]
                assert 0 <= hh < 24 and 0 <= mm < 60
            except (ValueError, AssertionError):
                abort(400, description="Время сводки — ЧЧ:ММ (МСК), пусто — выключить")
        from src.digest import DIGEST_TIME_KEY
        db.set_setting(DIGEST_TIME_KEY, t)
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
    import threading
    threading.Thread(target=_auto_fill_loop, name="auto-fill", daemon=True).start()
    log.info("Dashboard on http://%s:%d", config.web.host, config.web.port)
    app.run(host=config.web.host, port=config.web.port, threaded=True)
