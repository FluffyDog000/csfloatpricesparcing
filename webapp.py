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
import statistics

from flask import Flask, abort, g, jsonify, render_template, request

from src.config import load_config, load_items
from src.db import Database
from src.images import ImageService
from src.logging_setup import setup_logging
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
    images = ImageService(config, db)
    rows = db.items_summary()
    out = []
    folders: set[str] = set()
    for r in rows:
        name = r["market_hash_name"]
        icon = images.get_or_fetch(name)
        folder = r["folder"] or ""
        if folder:
            folders.add(folder)
        out.append(
            {
                "market_hash_name": name,
                "active": bool(r["active"]),
                "pattern_sensitive": bool(r["pattern_sensitive"]),
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
    icon = ImageService(config, db).get_or_fetch(name)
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
    if "folder" in data:
        kwargs["folder"] = (data.get("folder") or "").strip() or None
    if not get_db().update_item(name, **kwargs):
        abort(404, description=f"Item not tracked: {name}")
    log.info("Item updated via web: '%s' %s", name, kwargs)
    return jsonify({"ok": True})


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


@app.errorhandler(404)
def not_found(err):
    return jsonify({"error": str(getattr(err, "description", "not found"))}), 404


@app.errorhandler(400)
def bad_request(err):
    return jsonify({"error": str(getattr(err, "description", "bad request"))}), 400


@app.errorhandler(403)
def forbidden(err):
    return jsonify({"error": str(getattr(err, "description", "forbidden"))}), 403


if __name__ == "__main__":
    log.info("Dashboard on http://%s:%d", config.web.host, config.web.port)
    app.run(host=config.web.host, port=config.web.port, threaded=True)
