#!/usr/bin/env python3
"""Local web dashboard for the CSFloat sales tracker.

Reads the SAME SQLite database the collector writes to (WAL mode => concurrent
read while the collector keeps writing, no "database is locked"). Serves an
item list, per-item aggregation pages with expandable rows, and JSON endpoints
the frontend polls for live updates.

Run alongside the collector:
    python run_collector.py      # in one terminal (background writer)
    python webapp.py             # in another (this dashboard)
Then open http://localhost:5000
"""
from __future__ import annotations

import json
import logging

from flask import Flask, abort, g, jsonify, render_template, request

from src.config import load_config
from src.db import Database
from src.images import ImageService
from src.logging_setup import setup_logging
from src.report import (
    aggregate_buckets,
    aggregate_seeds,
    period_to_since_iso,
)

config = load_config()
setup_logging(config.log_path)
log = logging.getLogger("csfloat.web")

app = Flask(__name__)


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


def _period() -> str:
    p = (request.args.get("period") or config.reporting.default_period).lower()
    return p if p in _ALLOWED_PERIODS else config.reporting.default_period


def _require_item(name: str) -> int:
    item_id = get_db().get_item_id(name)
    if item_id is None:
        abort(404, description=f"Item not tracked: {name}")
    return item_id


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


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/item/<path:name>")
def item_page(name: str):
    _require_item(name)
    return render_template(
        "item.html",
        item_name=name,
        default_bucket=config.reporting.float_bucket_size,
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.route("/api/items")
def api_items():
    db = get_db()
    images = ImageService(config, db)
    rows = db.items_summary()
    out = []
    for r in rows:
        # Resolve (and cache) the item image URL; cheap after first fetch.
        icon = images.get_or_fetch(r["market_hash_name"])
        out.append(
            {
                "market_hash_name": r["market_hash_name"],
                "active": bool(r["active"]),
                "icon_url": icon,
                "total_sales": r["total_sales"],
                "avg_price": r["avg_price"],
                "min_price": round(r["min_price"], 2) if r["min_price"] is not None else None,
                "max_price": round(r["max_price"], 2) if r["max_price"] is not None else None,
                "last_polled_at": r["last_polled_at"],
                "last_sold_at": r["last_sold_at"],
            }
        )
    return jsonify({"items": out, "last_update": db.last_successful_poll()})


@app.route("/api/item/aggregates")
def api_aggregates():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    period = _period()
    try:
        bucket = float(request.args.get("bucket", config.reporting.float_bucket_size))
    except ValueError:
        bucket = config.reporting.float_bucket_size
    since = period_to_since_iso(None if period == "all" else period)

    rows = db.query_sales(item_id, since_iso=since)
    prices = [r["price"] for r in rows if r["price"] is not None]
    import statistics

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
            "period": period,
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
    period = _period()
    try:
        lo = float(request.args["bucket_lo"])
        size = float(request.args.get("bucket_size", config.reporting.float_bucket_size))
    except (KeyError, ValueError):
        abort(400, description="bucket_lo (and optional bucket_size) required")
    since = period_to_since_iso(None if period == "all" else period)
    rows = db.sales_in_float_range(item_id, lo, lo + size, since_iso=since)
    return jsonify({"sales": [_serialize_sale(r) for r in rows]})


@app.route("/api/item/seed_sales")
def api_seed_sales():
    name = request.args.get("item", "")
    item_id = _require_item(name)
    db = get_db()
    period = _period()
    seed_raw = request.args.get("seed", "")
    seed = None if seed_raw in ("", "none", "(none)") else int(seed_raw)
    since = period_to_since_iso(None if period == "all" else period)
    rows = db.query_sales(item_id, since_iso=since, paint_seed=seed)
    return jsonify({"sales": [_serialize_sale(r) for r in rows]})


@app.route("/api/status")
def api_status():
    db = get_db()
    return jsonify({"last_update": db.last_successful_poll()})


@app.errorhandler(404)
def not_found(err):
    return jsonify({"error": str(getattr(err, "description", "not found"))}), 404


@app.errorhandler(400)
def bad_request(err):
    return jsonify({"error": str(getattr(err, "description", "bad request"))}), 400


if __name__ == "__main__":
    log.info("Dashboard on http://%s:%d", config.web.host, config.web.port)
    app.run(host=config.web.host, port=config.web.port, threaded=True)
