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

    def close(self) -> None:
        self.conn.close()

    # -- items ---------------------------------------------------------------

    def upsert_item(self, market_hash_name: str, active: bool = True) -> int:
        """Ensure an item row exists; return its id. Updates active flag."""
        cur = self.conn.execute(
            "SELECT id FROM items WHERE market_hash_name = ?", (market_hash_name,)
        )
        row = cur.fetchone()
        if row:
            self.conn.execute(
                "UPDATE items SET active = ? WHERE id = ?",
                (1 if active else 0, row["id"]),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO items (market_hash_name, added_at, active) VALUES (?, ?, ?)",
            (market_hash_name, utcnow_iso(), 1 if active else 0),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def deactivate_missing_items(self, keep_names: Iterable[str]) -> None:
        """Mark items not in the current items.yaml as inactive (kept in DB
        so their history is not lost)."""
        keep = list(keep_names)
        placeholders = ",".join("?" for _ in keep) or "''"
        self.conn.execute(
            f"UPDATE items SET active = 0 "
            f"WHERE market_hash_name NOT IN ({placeholders})",
            keep,
        )
        self.conn.commit()

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
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO poll_log (
                item_id, market_hash_name, polled_at, fetched_count,
                new_count, overlap_count, status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        paint_seed: int | None = None,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM sales WHERE item_id = ?"
        params: list[Any] = [item_id]
        if since_iso:
            q += " AND sold_at >= ?"
            params.append(since_iso)
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
        q += " ORDER BY sold_at DESC"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

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
