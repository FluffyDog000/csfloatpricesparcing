"""Дополнить историю продаж своих предметов из чужой базы того же бота.

Берутся только продажи предметов, которые уже есть в своей базе (по
market_hash_name); чужие предметы, сделки, ордера и настройки не трогаются.
Повтор отсеивается дважды: по sale_id (id продажи CSFloat — одинаковый в обеих
базах) и по самой продаже — тот же предмет, float, цена и время в пределах
суток, на случай если одна из баз записала её под другим id.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

COLUMNS = ("sale_id", "market_hash_name", "price_cents", "price", "float_value",
           "paint_seed", "paint_index", "sold_at", "sold_at_estimated",
           "stickers_json", "raw_json", "scraped_at")
REQUIRED = ("sale_id", "market_hash_name", "sold_at")
SAME_SALE_DAYS = 1.0


def _source_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA src.table_info(sales)").fetchall()
    if not rows:
        raise ValueError("в чужой базе нет таблицы sales")
    return {r[1] for r in rows}


def _select(cols: set[str]) -> str:
    """Колонки COLUMNS[1:] из чужой базы; недостающие — вычисленные или NULL."""
    missing = [c for c in REQUIRED if c not in cols]
    if missing or not ({"price", "price_cents"} & cols):
        raise ValueError("в чужой базе у sales нет нужных колонок: "
                         + ", ".join(missing or ["price"]))
    now = datetime.now(timezone.utc).isoformat()
    fallback = {"sold_at_estimated": "0", "scraped_at": repr(now),
                "price_cents": "CAST(ROUND(s.price * 100) AS INTEGER)",
                "price": "s.price_cents / 100.0"}
    return ", ".join(f"s.{c}" if c in cols else f"{fallback.get(c, 'NULL')} AS {c}"
                     for c in COLUMNS[1:])


# Продажи чужой базы по своим предметам, которых у себя ещё нет.
_NEW = """
FROM src.sales s
JOIN main.items i ON i.market_hash_name = s.market_hash_name
WHERE s.sold_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM main.sales m WHERE m.sale_id = s.sale_id)
  AND NOT EXISTS (
      SELECT 1 FROM main.sales m
      WHERE m.item_id = i.id
        AND m.float_value IS s.float_value
        AND m.price_cents IS {cents}
        AND ABS(julianday(m.sold_at) - julianday(s.sold_at)) < {days})
"""


def _uri(path: str, *, readonly: bool = False) -> str:
    # Чужая база — только на чтение; рядом лежащий -wal тоже читается.
    return "file:" + quote(path) + ("?mode=ro" if readonly else "")


def merge(own_path: str, other_path: str, apply: bool = False) -> dict:
    """Что даст (apply=False) или что дала (apply=True) чужая база."""
    conn = sqlite3.connect(_uri(own_path), uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("ATTACH DATABASE ? AS src", (_uri(other_path, readonly=True),))
        cols = _source_columns(conn)
        select = _select(cols)
        cents = "s.price_cents" if "price_cents" in cols else \
            "CAST(ROUND(s.price * 100) AS INTEGER)"
        new = _NEW.format(cents=cents, days=SAME_SALE_DAYS)

        own_items = conn.execute("SELECT COUNT(*) FROM main.items").fetchone()[0]
        covered = conn.execute(
            "SELECT COUNT(DISTINCT i.id) FROM main.items i "
            "WHERE EXISTS (SELECT 1 FROM src.sales s "
            "              WHERE s.market_hash_name = i.market_hash_name)").fetchone()[0]
        per_item = conn.execute(
            f"SELECT i.market_hash_name, COUNT(*), MIN(s.sold_at) {new} "
            "GROUP BY i.id ORDER BY COUNT(*) DESC").fetchall()
        own_first = conn.execute("SELECT MIN(sold_at) FROM main.sales").fetchone()[0]
        other_first = conn.execute(
            "SELECT MIN(s.sold_at) FROM src.sales s "
            "JOIN main.items i ON i.market_hash_name = s.market_hash_name").fetchone()[0]
        result = {
            "own_items": own_items,
            "covered_items": covered,
            "items_gaining": len(per_item),
            "new_sales": sum(n for _, n, _ in per_item),
            "own_first": own_first,
            "other_first": other_first,
            "top": [{"name": name, "new": n, "first": first}
                    for name, n, first in per_item[:15]],
            "inserted": 0,
        }
        if apply and result["new_sales"]:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO main.sales (sale_id, item_id, "
                    + ", ".join(COLUMNS[1:]) + ") "
                    f"SELECT s.sale_id, i.id, {select} {new}")
                result["inserted"] = cur.rowcount
        return result
    finally:
        conn.close()
