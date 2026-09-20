"""SQLite storage layer: schema, item registry, sale insertion with
deduplication, and query helpers used by the collector and the reporter.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Sale

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT    NOT NULL UNIQUE,
    added_at            TEXT    NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    last_polled_at      TEXT
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id             TEXT    PRIMARY KEY,   -- API id, else deterministic hash
    item_id             INTEGER NOT NULL REFERENCES items(id),
    market_hash_name    TEXT    NOT NULL,
    price_cents         INTEGER,               -- price in USD cents (integer)
    price               REAL,                  -- price in USD (dollars)
    float_value         REAL,
    paint_seed          INTEGER,
    paint_index         INTEGER,
    sold_at             TEXT,                  -- ISO-8601 UTC timestamp
    sold_at_estimated   INTEGER NOT NULL DEFAULT 0,  -- 1 if derived from "N ago"
    stickers_json       TEXT,                  -- JSON array or NULL
    raw_json            TEXT,                  -- full raw record (debugging)
    scraped_at          TEXT    NOT NULL       -- when our script recorded it
);

CREATE INDEX IF NOT EXISTS idx_sales_item        ON sales(item_id);
CREATE INDEX IF NOT EXISTS idx_sales_sold_at      ON sales(sold_at);
CREATE INDEX IF NOT EXISTS idx_sales_paint_seed   ON sales(paint_seed);
CREATE INDEX IF NOT EXISTS idx_sales_float        ON sales(float_value);

CREATE TABLE IF NOT EXISTS settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT
);

CREATE TABLE IF NOT EXISTS buy_orders (
    item_id             INTEGER NOT NULL REFERENCES items(id),
    price               REAL    NOT NULL,   -- USD
    qty                 INTEGER NOT NULL DEFAULT 1,
    float_min           REAL,               -- set for float-scoped orders
    float_max           REAL,
    paint_seed          INTEGER,
    position            INTEGER NOT NULL,   -- rank in the book, best bid = 0
    fetched_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_item ON buy_orders(item_id);

-- buy_orders holds one snapshot per item, replaced on every sweep, so how
-- fast a band's top bid moves was never recorded. That rate decides whether
-- an order placed there can be defended: outbid faster than it fills and the
-- position is worth nothing. Kept as a per-band profile, it is about a dozen
-- rows per sweep rather than the whole book.
CREATE TABLE IF NOT EXISTS book_history (
    item_id             INTEGER NOT NULL REFERENCES items(id),
    fetched_at          TEXT    NOT NULL,
    float_min           REAL    NOT NULL,
    float_max           REAL    NOT NULL,
    top_price           REAL,               -- best bid competing for the band
    orders              INTEGER NOT NULL,   -- how many orders overlap it
    qty                 INTEGER NOT NULL,   -- total quantity behind them
    PRIMARY KEY (item_id, fetched_at, float_min)
);

CREATE INDEX IF NOT EXISTS idx_book_hist_item
    ON book_history(item_id, float_min, fetched_at);

-- The sell half of the cycle. Time-to-resell was estimated as one over the
-- band's sale rate, which assumes you are the only seller and put $300 gloves
-- back on the market in seven hours. The live listings say how long the queue
-- actually is and how long its lots have been sitting.
CREATE TABLE IF NOT EXISTS listing_depth (
    item_id             INTEGER NOT NULL REFERENCES items(id),
    fetched_at          TEXT    NOT NULL,
    float_min           REAL    NOT NULL,
    float_max           REAL    NOT NULL,
    listings            INTEGER NOT NULL,   -- lots asking less than you
    cheapest            REAL,               -- the real exit price, in USD
    median_age_days     REAL,               -- how long they have sat unsold
    oldest_days         REAL,
    offerable           INTEGER NOT NULL DEFAULT 0,  -- reachable below ask
    best_offer          REAL,
    PRIMARY KEY (item_id, fetched_at, float_min)
);

CREATE INDEX IF NOT EXISTS idx_depth_item
    ON listing_depth(item_id, float_min, fetched_at);

-- Our own buy orders, as we believe them to stand. CSFloat removes an order
-- when it tries to execute it and the balance will not cover it, and says
-- nothing, so what we placed and what is live can drift apart: this table is
-- the "what we placed" half, and reconciling it against the site is how a
-- silent removal is noticed at all.
CREATE TABLE IF NOT EXISTS our_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER NOT NULL REFERENCES items(id),
    float_min           REAL    NOT NULL,
    float_max           REAL    NOT NULL,
    price               REAL    NOT NULL,   -- what we are bidding now
    ceiling             REAL    NOT NULL,   -- we withdraw rather than pass it
    remote_id           TEXT,               -- CSFloat's id, once placed
    state               TEXT    NOT NULL,   -- planned/live/cancelled/filled/gone
    placed_at           TEXT,
    updated_at          TEXT    NOT NULL,
    note                TEXT
);

CREATE INDEX IF NOT EXISTS idx_our_orders_item
    ON our_orders(item_id, state);

-- our_orders holds where a position stands; this holds how it got there.
-- "placed at 12:49, raised at 13:55 because someone outbid us" is not in the
-- first table at all - an update overwrites the price and the reason with it -
-- and it is the only record that says whether the bot is working.
CREATE TABLE IF NOT EXISTS order_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    at                  TEXT    NOT NULL,
    item_id             INTEGER,
    market_hash_name    TEXT    NOT NULL,
    float_min           REAL,
    float_max           REAL,
    kind                TEXT    NOT NULL,   -- place/raise/cancel/keep
    price               REAL,
    was                 REAL,               -- the price before a raise
    ceiling             REAL,
    remote_id           TEXT,
    ok                  INTEGER NOT NULL,
    dry                 INTEGER NOT NULL,   -- a rehearsal, nothing was sent
    source              TEXT    NOT NULL,   -- plan/defence
    reason              TEXT,               -- why the bot decided it
    detail              TEXT                -- what came back
);

CREATE INDEX IF NOT EXISTS idx_order_events_at
    ON order_events(at DESC);

CREATE TABLE IF NOT EXISTS poll_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER,
    market_hash_name    TEXT,
    polled_at           TEXT    NOT NULL,
    fetched_count       INTEGER,
    new_count           INTEGER,
    overlap_count       INTEGER,
    status              TEXT,                  -- ok/auth_error/rate_limited/error
    note                TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Sentinel so update_item can distinguish "set folder to None" from "leave as is".
_UNSET = object()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations (safe to run repeatedly). Adds columns the web
        dashboard needs without touching existing data."""
        cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(items)").fetchall()
        }
        if "icon_url" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN icon_url TEXT")
        if "image_cached_at" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN image_cached_at TEXT")
        if "pattern_sensitive" not in cols:
            self.conn.execute(
                "ALTER TABLE items ADD COLUMN pattern_sensitive INTEGER NOT NULL "
                "DEFAULT 1"
            )
        if "folder" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN folder TEXT")
        if "interval_min_minutes" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN interval_min_minutes REAL")
        if "interval_max_minutes" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN interval_max_minutes REAL")
        if "hidden" not in cols:
            self.conn.execute(
                "ALTER TABLE items ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
            )

        if "orders_requested_at" not in cols:
            # Set by the dashboard's order button; cleared once fetched.
            self.conn.execute("ALTER TABLE items ADD COLUMN orders_requested_at TEXT")
        if "listing_id" not in cols:
            # Cached id of one of the item's listings: buy orders are keyed by
            # listing, so a fetch needs one and re-resolving costs a request.
            self.conn.execute("ALTER TABLE items ADD COLUMN listing_id TEXT")

        if "poll_requested_at" not in cols:
            # Set by the dashboard's "poll now" button; the collector clears it
            # once the item has actually been polled.
            self.conn.execute("ALTER TABLE items ADD COLUMN poll_requested_at TEXT")

        log_cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(poll_log)").fetchall()
        }
        if "response_bytes" not in log_cols:
            # Measured response size, so the dashboard can report real traffic
            # instead of a guess (it is what a metered proxy bills for).
            self.conn.execute("ALTER TABLE poll_log ADD COLUMN response_bytes INTEGER")

    def close(self) -> None:
        self.conn.close()

    # -- settings (key/value) ------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- items ---------------------------------------------------------------

    def upsert_item(
        self,
        market_hash_name: str,
        active: bool = True,
        pattern_sensitive: bool = True,
        folder: str | None = None,
        interval_min_minutes: float | None = None,
        interval_max_minutes: float | None = None,
    ) -> int:
        """Ensure an item row exists; return its id. On an existing row, updates
        active/pattern/folder/intervals to the given values."""
        cur = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        )
        row = cur.fetchone()
        if row:
            self.conn.execute(
                "UPDATE items SET active = ?, pattern_sensitive = ?, folder = ?, "
                "interval_min_minutes = ?, interval_max_minutes = ? WHERE id = ?",
                (1 if active else 0, 1 if pattern_sensitive else 0, folder,
                 interval_min_minutes, interval_max_minutes, row["id"]),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO items (market_hash_name, added_at, active, pattern_sensitive, "
            "folder, interval_min_minutes, interval_max_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (market_hash_name, utcnow_iso(), 1 if active else 0,
             1 if pattern_sensitive else 0, folder,
             interval_min_minutes, interval_max_minutes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- item management (DB is the source of truth for the tracked list) -----

    def add_item(
        self,
        market_hash_name: str,
        folder: str | None = None,
        pattern_sensitive: bool = True,
    ) -> int:
        """Add a new active item, or re-activate an existing one. Returns id."""
        existing = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE items SET active = 1, pattern_sensitive = ?, folder = ? "
                "WHERE id = ?",
                (1 if pattern_sensitive else 0, folder, existing["id"]),
            )
            self.conn.commit()
            return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO items (market_hash_name, added_at, active, pattern_sensitive, "
            "folder) VALUES (?, ?, 1, ?, ?)",
            (market_hash_name, utcnow_iso(), 1 if pattern_sensitive else 0, folder),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_item(
        self,
        market_hash_name: str,
        *,
        active: bool | None = None,
        pattern_sensitive: bool | None = None,
        hidden: bool | None = None,
        folder: object = _UNSET,
    ) -> bool:
        """Patch selected fields of an item. Returns True if the item existed."""
        row = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        ).fetchone()
        if not row:
            return False
        sets, params = [], []
        if active is not None:
            sets.append("active = ?")
            params.append(1 if active else 0)
        if pattern_sensitive is not None:
            sets.append("pattern_sensitive = ?")
            params.append(1 if pattern_sensitive else 0)
        if hidden is not None:
            sets.append("hidden = ?")
            params.append(1 if hidden else 0)
        if folder is not _UNSET:
            sets.append("folder = ?")
            params.append(folder or None)
        if sets:
            params.append(row["id"])
            self.conn.execute(
                f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params
            )
            self.conn.commit()
        return True

    def delete_item(self, market_hash_name: str, purge_history: bool = True) -> bool:
        """Delete an item. With purge_history, its sales are removed too
        (default). Returns True if the item existed."""
        row = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        ).fetchone()
        if not row:
            return False
        if purge_history:
            self.conn.execute("DELETE FROM sales WHERE item_id = ?", (row["id"],))
            self.conn.execute("DELETE FROM poll_log WHERE item_id = ?", (row["id"],))
        self.conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))
        self.conn.commit()
        return True

    def items_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"])

    def request_poll(self, market_hash_name: str) -> bool:
        """Queue an out-of-band poll for one item (the dashboard's button).

        A flag in the DB rather than a request from the web process: only the
        collector holds the proxy pool, the spacing and the 429 cooldown, so it
        is the only place a request can be made without double-spending quota."""
        cur = self.conn.execute(
            "UPDATE items SET poll_requested_at = ? "
            "WHERE market_hash_name = ? AND active = 1",
            (utcnow_iso(), market_hash_name),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_poll_requests(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, market_hash_name, pattern_sensitive, interval_min_minutes, "
            "interval_max_minutes, poll_requested_at FROM items "
            "WHERE poll_requested_at IS NOT NULL AND active = 1 "
            "ORDER BY poll_requested_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_poll_request(self, item_id: int) -> None:
        self.conn.execute(
            "UPDATE items SET poll_requested_at = NULL WHERE id = ?", (item_id,)
        )
        self.conn.commit()

    # -- buy orders (snapshot per item, replaced on each fetch) --------------

    def request_orders(self, market_hash_name: str) -> bool:
        cur = self.conn.execute(
            "UPDATE items SET orders_requested_at = ? WHERE market_hash_name = ?",
            (utcnow_iso(), market_hash_name),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_order_requests(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, market_hash_name, listing_id FROM items "
            "WHERE orders_requested_at IS NOT NULL ORDER BY orders_requested_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_order_request(self, item_id: int) -> None:
        self.conn.execute(
            "UPDATE items SET orders_requested_at = NULL WHERE id = ?", (item_id,))
        self.conn.commit()

    def set_listing_id(self, item_id: int, listing_id: str | None) -> None:
        self.conn.execute("UPDATE items SET listing_id = ? WHERE id = ?",
                          (listing_id, item_id))
        self.conn.commit()

    def replace_buy_orders(self, item_id: int, orders: list[dict[str, Any]]) -> int:
        """Store the current book, dropping the previous snapshot."""
        now = utcnow_iso()
        self.conn.execute("DELETE FROM buy_orders WHERE item_id = ?", (item_id,))
        self.conn.executemany(
            "INSERT INTO buy_orders (item_id, price, qty, float_min, float_max, "
            "paint_seed, position, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(item_id, o["price"], o.get("qty") or 1, o.get("float_min"),
              o.get("float_max"), o.get("paint_seed"), i, now)
             for i, o in enumerate(orders)],
        )
        self.conn.commit()
        return len(orders)

    def record_book_profile(self, item_id: int,
                            profile: list[dict[str, Any]]) -> int:
        """Append one sweep's per-band tops, keeping what the snapshot drops."""
        if not profile:
            return 0
        now = utcnow_iso()
        self.conn.executemany(
            "INSERT OR REPLACE INTO book_history (item_id, fetched_at, "
            "float_min, float_max, top_price, orders, qty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(item_id, now, b["float_min"], b["float_max"], b["top_price"],
              b["orders"], b["qty"]) for b in profile],
        )
        self.conn.commit()
        return len(profile)

    def book_history(self, item_id: int,
                     since: str | None = None) -> list[dict[str, Any]]:
        """Per-band tops over time, oldest first, for measuring how fast the
        book moves."""
        sql = ("SELECT fetched_at, float_min, float_max, top_price, orders, qty "
               "FROM book_history WHERE item_id = ?")
        args: list[Any] = [item_id]
        if since:
            sql += " AND fetched_at >= ?"
            args.append(since)
        sql += " ORDER BY fetched_at, float_min"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def record_order_event(self, *, name: str, kind: str, ok: bool,
                           dry: bool, source: str,
                           item_id: int | None = None,
                           float_min: float | None = None,
                           float_max: float | None = None,
                           price: float | None = None,
                           was: float | None = None,
                           ceiling: float | None = None,
                           remote_id: str | None = None,
                           reason: str | None = None,
                           detail: str | None = None) -> int:
        """Append one thing that happened to one order. Never updated."""
        cur = self.conn.execute(
            "INSERT INTO order_events (at, item_id, market_hash_name, "
            "float_min, float_max, kind, price, was, ceiling, remote_id, ok, "
            "dry, source, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow_iso(), item_id, name, float_min, float_max, kind, price,
             was, ceiling, remote_id, 1 if ok else 0, 1 if dry else 0, source,
             reason, detail))
        self.conn.commit()
        return int(cur.lastrowid)

    def order_events(self, limit: int = 200, name: str | None = None,
                     include_dry: bool = True,
                     since: str | None = None) -> list[dict[str, Any]]:
        """The log, newest first."""
        sql = ("SELECT id, at, item_id, market_hash_name, float_min, float_max, "
               "kind, price, was, ceiling, remote_id, ok, dry, source, reason, "
               "detail FROM order_events")
        where, args = [], []
        if name:
            where.append("market_hash_name = ?")
            args.append(name)
        if not include_dry:
            where.append("dry = 0")
        if since:
            where.append("at >= ?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        rows = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        for r in rows:
            r["ok"] = bool(r["ok"])
            r["dry"] = bool(r["dry"])
        return rows

    def item_name(self, item_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT market_hash_name FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return row["market_hash_name"] if row else None

    def our_orders(self, item_id: int | None = None,
                   live_only: bool = True) -> list[dict[str, Any]]:
        sql = ("SELECT id, item_id, float_min, float_max, price, ceiling, "
               "remote_id, state, placed_at, updated_at, note FROM our_orders")
        where, args = [], []
        if item_id is not None:
            where.append("item_id = ?")
            args.append(item_id)
        if live_only:
            where.append("state IN ('planned', 'live')")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY item_id, float_min"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def upsert_our_order(self, item_id: int, float_min: float, float_max: float,
                         price: float, ceiling: float, state: str,
                         remote_id: str | None = None,
                         note: str | None = None) -> int:
        """One row per item and float band: two orders on the same band would
        only bid against each other."""
        now = utcnow_iso()
        row = self.conn.execute(
            "SELECT id, placed_at FROM our_orders WHERE item_id = ? "
            "AND float_min = ? AND float_max = ? AND state IN ('planned','live')",
            (item_id, float_min, float_max)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE our_orders SET price = ?, ceiling = ?, state = ?, "
                "remote_id = COALESCE(?, remote_id), note = ?, updated_at = ? "
                "WHERE id = ?",
                (price, ceiling, state, remote_id, note, now, row["id"]))
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO our_orders (item_id, float_min, float_max, price, "
            "ceiling, remote_id, state, placed_at, updated_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, float_min, float_max, price, ceiling, remote_id, state,
             now if state == "live" else None, now, note))
        self.conn.commit()
        return int(cur.lastrowid)

    def set_our_order_state(self, order_id: int, state: str,
                            note: str | None = None) -> None:
        self.conn.execute(
            "UPDATE our_orders SET state = ?, note = COALESCE(?, note), "
            "updated_at = ? WHERE id = ?",
            (state, note, utcnow_iso(), order_id))
        self.conn.commit()

    def record_listing_depth(self, item_id: int,
                             profile: list[dict[str, Any]]) -> int:
        """Append one sell-side sweep: the queue per band and its age."""
        if not profile:
            return 0
        now = utcnow_iso()
        self.conn.executemany(
            "INSERT OR REPLACE INTO listing_depth (item_id, fetched_at, "
            "float_min, float_max, listings, cheapest, median_age_days, "
            "oldest_days, offerable, best_offer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(item_id, now, b["float_min"], b["float_max"], b["listings"],
              b["cheapest"], b["median_age_days"], b["oldest_days"],
              b["offerable"], b["best_offer"]) for b in profile],
        )
        self.conn.commit()
        return len(profile)

    def listing_depth(self, item_id: int,
                      latest_only: bool = True) -> list[dict[str, Any]]:
        """Per-band sell-side depth, newest sweep by default."""
        sql = ("SELECT fetched_at, float_min, float_max, listings, cheapest, "
               "median_age_days, oldest_days, offerable, best_offer "
               "FROM listing_depth WHERE item_id = ?")
        args: list[Any] = [item_id]
        if latest_only:
            sql += (" AND fetched_at = (SELECT MAX(fetched_at) FROM "
                    "listing_depth WHERE item_id = ?)")
            args.append(item_id)
        sql += " ORDER BY fetched_at, float_min"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def buy_orders(self, item_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT price, qty, float_min, float_max, paint_seed, fetched_at "
            "FROM buy_orders WHERE item_id = ? ORDER BY position", (item_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_items(self) -> list[dict[str, Any]]:
        """Active items for the collector to poll (source of truth = DB)."""
        rows = self.conn.execute(
            "SELECT id, market_hash_name, pattern_sensitive, interval_min_minutes, "
            "interval_max_minutes FROM items WHERE active = 1 "
            "ORDER BY market_hash_name"
        ).fetchall()
        return [dict(r) for r in rows]

    def items_needing_icon(self, cutoff_iso: str, limit: int = 1) -> list[str]:
        """Active items with no cached image whose last attempt (if any) is
        older than cutoff_iso. Oldest attempt first."""
        rows = self.conn.execute(
            "SELECT market_hash_name FROM items "
            "WHERE active = 1 AND (icon_url IS NULL OR icon_url = '') "
            "AND (image_cached_at IS NULL OR image_cached_at <= ?) "
            "ORDER BY image_cached_at IS NOT NULL, image_cached_at "
            "LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()
        return [r["market_hash_name"] for r in rows]

    def get_item_meta(self, market_hash_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT active, pattern_sensitive, folder FROM items "
            "WHERE market_hash_name = ?",
            (market_hash_name,),
        ).fetchone()
        return dict(row) if row else None

    def set_last_polled(self, item_id: int, when_iso: str | None = None) -> None:
        self.conn.execute(
            "UPDATE items SET last_polled_at = ? WHERE id = ?",
            (when_iso or utcnow_iso(), item_id),
        )
        self.conn.commit()

    def item_sales_count(self, item_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS c FROM sales WHERE item_id = ?", (item_id,)
        )
        return int(cur.fetchone()["c"])

    # -- sales ---------------------------------------------------------------

    def existing_sale_ids(self, sale_ids: Iterable[str]) -> set[str]:
        ids = list(sale_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"SELECT sale_id FROM sales WHERE sale_id IN ({placeholders})", ids
        )
        return {r["sale_id"] for r in cur.fetchall()}

    def insert_sales(self, sales: list[Sale]) -> int:
        """Insert sales with INSERT OR IGNORE (dedup on sale_id PK).
        Returns the number of rows actually inserted (new sales)."""
        if not sales:
            return 0
        before = self._total_sales()
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO sales (
                sale_id, item_id, market_hash_name, price_cents, price,
                float_value, paint_seed, paint_index, sold_at,
                sold_at_estimated, stickers_json, raw_json, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.sale_id,
                    s.item_id,
                    s.market_hash_name,
                    s.price_cents,
                    s.price,
                    s.float_value,
                    s.paint_seed,
                    s.paint_index,
                    s.sold_at,
                    1 if s.sold_at_estimated else 0,
                    json.dumps(s.stickers, ensure_ascii=False)
                    if s.stickers is not None
                    else None,
                    s.raw_json,
                    s.scraped_at,
                )
                for s in sales
            ],
        )
        self.conn.commit()
        return self._total_sales() - before

    def _total_sales(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM sales").fetchone()["c"])

    def log_poll(
        self,
        *,
        item_id: int | None,
        market_hash_name: str,
        fetched_count: int,
        new_count: int,
        overlap_count: int,
        status: str,
        note: str = "",
        response_bytes: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO poll_log (
                item_id, market_hash_name, polled_at, fetched_count,
                new_count, overlap_count, status, note, response_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                market_hash_name,
                utcnow_iso(),
                fetched_count,
                new_count,
                overlap_count,
                status,
                note,
                response_bytes,
            ),
        )
        self.conn.commit()

    # -- reporting queries ---------------------------------------------------

    def get_item_id(self, market_hash_name: str) -> int | None:
        cur = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None

    def list_item_names(self, active_only: bool = False) -> list[str]:
        q = "SELECT market_hash_name FROM items"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY market_hash_name"
        return [r["market_hash_name"] for r in self.conn.execute(q).fetchall()]

    def query_sales(
        self,
        item_id: int,
        since_iso: str | None = None,
        until_iso: str | None = None,
        paint_seed: int | None = None,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM sales WHERE item_id = ?"
        params: list[Any] = [item_id]
        if since_iso:
            q += " AND sold_at >= ?"
            params.append(since_iso)
        if until_iso:
            q += " AND sold_at <= ?"
            params.append(until_iso)
        if paint_seed is not None:
            q += " AND paint_seed = ?"
            params.append(paint_seed)
        q += " ORDER BY sold_at DESC"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    # -- web dashboard helpers ----------------------------------------------

    def items_summary(self, active_only: bool = False) -> list[dict[str, Any]]:
        """One row per item with a rollup for the item cards.

        Recent windows as well as the all-time count: an item added last week
        has few stored sales however briskly it trades, so the total says more
        about when it was added than about how liquid it is."""
        where = "WHERE i.active = 1" if active_only else ""
        now = datetime.now(timezone.utc)
        cut7 = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
        cut30 = (now - timedelta(days=30)).replace(microsecond=0).isoformat()
        q = f"""
            SELECT
                i.id                       AS item_id,
                i.market_hash_name         AS market_hash_name,
                i.active                   AS active,
                i.pattern_sensitive        AS pattern_sensitive,
                i.hidden                   AS hidden,
                i.folder                   AS folder,
                i.icon_url                 AS icon_url,
                i.last_polled_at           AS last_polled_at,
                COUNT(s.sale_id)           AS total_sales,
                SUM(CASE WHEN s.sold_at >= :cut7 THEN 1 ELSE 0 END)  AS sales_7d,
                SUM(CASE WHEN s.sold_at >= :cut30 THEN 1 ELSE 0 END) AS sales_30d,
                AVG(s.price)               AS avg_price,
                MIN(s.price)               AS min_price,
                MAX(s.price)               AS max_price,
                MAX(s.sold_at)             AS last_sold_at
            FROM items i
            LEFT JOIN sales s ON s.item_id = i.id
            {where}
            GROUP BY i.id
            ORDER BY i.market_hash_name
        """
        out = []
        for r in self.conn.execute(q, {"cut7": cut7, "cut30": cut30}).fetchall():
            d = dict(r)
            d["avg_price"] = round(d["avg_price"], 2) if d["avg_price"] is not None else None
            out.append(d)
        return out

    def sales_in_float_range(
        self,
        item_id: int,
        lo: float,
        hi: float,
        since_iso: str | None = None,
        until_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Sales whose float_value is in [lo, hi). Newest first."""
        q = (
            "SELECT * FROM sales WHERE item_id = ? "
            "AND float_value >= ? AND float_value < ?"
        )
        params: list[Any] = [item_id, lo, hi]
        if since_iso:
            q += " AND sold_at >= ?"
            params.append(since_iso)
        if until_iso:
            q += " AND sold_at <= ?"
            params.append(until_iso)
        q += " ORDER BY sold_at DESC"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def poll_stats(self, since_iso: str) -> dict[str, Any]:
        """Aggregate poll_log since a cutoff: counts per status + new sales."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS c, COALESCE(SUM(new_count), 0) AS n "
            "FROM poll_log WHERE polled_at >= ? GROUP BY status",
            (since_iso,),
        ).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        total = sum(by_status.values())
        new_sales = sum(r["n"] for r in rows)
        return {
            "total": total,
            "ok": by_status.get("ok", 0),
            "rate_limited": by_status.get("rate_limited", 0),
            "auth_error": by_status.get("auth_error", 0),
            "proxy_blocked": by_status.get("proxy_blocked", 0),
            "error": by_status.get("error", 0),
            "new_sales": new_sales,
        }

    def gap_warnings(self, since_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT market_hash_name, polled_at, overlap_count, note FROM poll_log "
            "WHERE note LIKE '%MISSED%' AND polled_at >= ? ORDER BY id DESC LIMIT ?",
            (since_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def response_size_stats(self, since_iso: str) -> dict[str, Any]:
        """Average measured response size since a cutoff, for traffic estimates."""
        row = self.conn.execute(
            "SELECT AVG(response_bytes) AS avg_bytes, COUNT(response_bytes) AS n, "
            "COALESCE(SUM(response_bytes), 0) AS total FROM poll_log "
            "WHERE polled_at >= ? AND response_bytes IS NOT NULL",
            (since_iso,),
        ).fetchone()
        return {"avg_bytes": row["avg_bytes"], "samples": row["n"] or 0,
                "total_bytes": row["total"] or 0}

    def recent_poll_log(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT market_hash_name, polled_at, fetched_count, new_count, "
            "overlap_count, status, note FROM poll_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def active_items_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE active = 1"
        ).fetchone()["c"])

    def sales_window(self, item_id: int, since_iso: str) -> dict[str, Any]:
        """Sales count and the earliest sale timestamp within a window — used to
        estimate how fast an item actually sells."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c, MIN(sold_at) AS first_sold, MAX(sold_at) AS last_sold "
            "FROM sales WHERE item_id = ? AND sold_at >= ?",
            (item_id, since_iso),
        ).fetchone()
        return dict(row) if row else {"c": 0, "first_sold": None, "last_sold": None}

    def sales_rates(self, since_iso: str) -> dict[int, tuple[int, str | None]]:
        """{item_id: (sales_in_window, earliest_sold_at)} for every active item."""
        rows = self.conn.execute(
            "SELECT i.id AS item_id, COUNT(s.sale_id) AS c, MIN(s.sold_at) AS first_sold "
            "FROM items i LEFT JOIN sales s "
            "  ON s.item_id = i.id AND s.sold_at >= ? "
            "WHERE i.active = 1 GROUP BY i.id",
            (since_iso,),
        ).fetchall()
        return {int(r["item_id"]): (int(r["c"] or 0), r["first_sold"]) for r in rows}

    def last_successful_poll(self, item_id: int | None = None) -> str | None:
        """Timestamp of the most recent successful poll (global or per item)."""
        if item_id is None:
            row = self.conn.execute(
                "SELECT MAX(polled_at) AS t FROM poll_log WHERE status = 'ok'"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(polled_at) AS t FROM poll_log "
                "WHERE status = 'ok' AND item_id = ?",
                (item_id,),
            ).fetchone()
        return row["t"] if row else None

    def get_icon(self, market_hash_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT icon_url, image_cached_at FROM items WHERE market_hash_name = ?",
            (market_hash_name,),
        ).fetchone()
        return dict(row) if row else None

    def set_icon(self, market_hash_name: str, icon_url: str | None) -> None:
        self.conn.execute(
            "UPDATE items SET icon_url = ?, image_cached_at = ? "
            "WHERE market_hash_name = ?",
            (icon_url, utcnow_iso(), market_hash_name),
        )
        self.conn.commit()
