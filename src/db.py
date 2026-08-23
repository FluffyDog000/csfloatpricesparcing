"""SQLite storage layer: schema, item registry, sale insertion with
deduplication, and query helpers used by the collector and the reporter.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
        """One row per item with a rollup for the item cards."""
        where = "WHERE i.active = 1" if active_only else ""
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
        for r in self.conn.execute(q).fetchall():
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
            "SELECT AVG(response_bytes) AS avg_bytes, COUNT(response_bytes) AS n "
            "FROM poll_log WHERE polled_at >= ? AND response_bytes IS NOT NULL",
            (since_iso,),
        ).fetchone()
        return {"avg_bytes": row["avg_bytes"], "samples": row["n"] or 0}

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
