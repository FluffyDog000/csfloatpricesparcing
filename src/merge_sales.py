"""Дополнить историю продаж своих предметов из чужой базы.

Понимает два формата:

- база этого же бота: `sales` с `market_hash_name`;
- база cs2arb (FloatHistory): `sales` с оружием, скином и износом по
  отдельности, цена текстом; имя предмета берётся из её `market_items`.

Берутся только продажи CSFloat в долларах и только по предметам, которые уже
есть в своей базе. Фазы Doppler («… | Doppler Phase 2 (Factory New)») узнаются
по paint_index, как их узнаёт сборщик. Чужие предметы, сделки, аккаунты,
ключи и настройки не читаются вовсе.

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
ARB_KEYS = ("weapon", "skin", "wear", "stattrak", "souvenir", "star")
SAME_SALE_DAYS = 1.0


def _uri(path: str, *, readonly: bool = False) -> str:
    # Чужая база — только на чтение; рядом лежащий -wal тоже читается.
    return "file:" + quote(path) + ("?mode=ro" if readonly else "")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA src.table_info({table})")}


def _own_format(cols: set[str]) -> str:
    """SELECT в колонки COLUMNS из `src.sales` этого же бота."""
    missing = [c for c in REQUIRED if c not in cols]
    if missing or not ({"price", "price_cents"} & cols):
        raise ValueError("в чужой базе у sales нет нужных колонок: "
                         + ", ".join(missing or ["price"]))
    now = datetime.now(timezone.utc).isoformat()
    fallback = {"sold_at_estimated": "0", "scraped_at": repr(now),
                "price_cents": "CAST(ROUND(s.price * 100) AS INTEGER)",
                "price": "s.price_cents / 100.0"}
    return ("SELECT " + ", ".join(
        f"s.{c}" if c in cols else f"{fallback.get(c, 'NULL')} AS {c}"
        for c in COLUMNS) + " FROM src.sales s")


def _arb_format(conn: sqlite3.Connection, cols: set[str]) -> str:
    """SELECT в колонки COLUMNS из базы cs2arb: имя — из её market_items."""
    if "market_hash_name" not in _columns(conn, "market_items"):
        raise ValueError("в базе cs2arb нет market_items с именами предметов")
    conn.execute("CREATE TEMP TABLE arb_names AS SELECT "
                 + ", ".join(ARB_KEYS) + ", market_hash_name FROM src.market_items")
    conn.execute("CREATE INDEX temp.arb_names_k ON arb_names ("
                 + ", ".join(ARB_KEYS) + ")")
    price = "CAST(s.price AS REAL)"
    where = ["s.sold_at IS NOT NULL", "s.price IS NOT NULL"]
    if "platform" in cols:
        where.append("s.platform = 'csfloat'")
    if "currency" in cols:
        where.append("COALESCE(s.currency, 'USD') = 'USD'")
    scraped = "s.collected_at" if "collected_at" in cols else "s.sold_at"
    join = " AND ".join(f"n.{k} IS s.{k}" for k in ARB_KEYS)
    return (f"SELECT s.sale_id, n.market_hash_name, "
            f"CAST(ROUND({price} * 100) AS INTEGER) AS price_cents, "
            f"{price} AS price, s.float_value, s.paint_seed, s.paint_index, "
            f"s.sold_at, 0 AS sold_at_estimated, NULL AS stickers_json, "
            f"NULL AS raw_json, {scraped} AS scraped_at "
            f"FROM src.sales s JOIN arb_names n ON {join} "
            f"WHERE {' AND '.join(where)}")


def _stage(conn: sqlite3.Connection) -> str:
    """Чужие продажи — во временную таблицу `incoming` в своём формате.
    Возвращает название формата."""
    cols = _columns(conn, "sales")
    if not cols:
        raise ValueError("в чужой базе нет таблицы sales")
    if "market_hash_name" in cols:
        kind, select = "этот же бот", _own_format(cols)
    elif set(ARB_KEYS) <= cols:
        kind, select = "cs2arb (FloatHistory)", _arb_format(conn, cols)
    else:
        raise ValueError("незнакомый формат sales: " + ", ".join(sorted(cols)))
    conn.execute(f"CREATE TEMP TABLE incoming AS {select}")
    conn.execute("CREATE INDEX temp.incoming_name ON incoming (market_hash_name)")
    return kind


def _own_map(conn: sqlite3.Connection) -> None:
    """Свои предметы и под каким именем их искать в чужих продажах: фаза
    Doppler ищется по общему имени и своему paint_index."""
    from .phases import split

    rows = []
    for item_id, name in conn.execute("SELECT id, market_hash_name FROM main.items"):
        rows.append((item_id, name, name, None))
        base, index = split(name)
        if index is not None:
            rows.append((item_id, name, base, index))
    conn.execute("CREATE TEMP TABLE own_map (item_id INTEGER, name TEXT, "
                 "match_name TEXT, paint_index INTEGER)")
    conn.executemany("INSERT INTO own_map VALUES (?, ?, ?, ?)", rows)
    conn.execute("CREATE INDEX temp.own_map_name ON own_map (match_name)")


# Чужие продажи по своим предметам, которых у себя ещё нет.
_NEW = f"""
FROM incoming s
JOIN own_map i ON i.match_name = s.market_hash_name
              AND (i.paint_index IS NULL OR s.paint_index = i.paint_index)
WHERE s.sold_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM main.sales m WHERE m.sale_id = s.sale_id)
  AND NOT EXISTS (
      SELECT 1 FROM main.sales m
      WHERE m.item_id = i.item_id
        AND m.float_value IS s.float_value
        AND m.price_cents IS s.price_cents
        AND ABS(julianday(m.sold_at) - julianday(s.sold_at)) < {SAME_SALE_DAYS})
"""


def merge(own_path: str, other_path: str, apply: bool = False) -> dict:
    """Что даст (apply=False) или что дала (apply=True) чужая база."""
    conn = sqlite3.connect(_uri(own_path), uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("ATTACH DATABASE ? AS src", (_uri(other_path, readonly=True),))
        kind = _stage(conn)
        _own_map(conn)

        own_items = conn.execute("SELECT COUNT(*) FROM main.items").fetchone()[0]
        covered, other_first = conn.execute(
            "SELECT COUNT(DISTINCT i.item_id), MIN(s.sold_at) FROM incoming s "
            "JOIN own_map i ON i.match_name = s.market_hash_name "
            " AND (i.paint_index IS NULL OR s.paint_index = i.paint_index)").fetchone()
        per_item = conn.execute(
            f"SELECT i.name, COUNT(*), MIN(s.sold_at) {_NEW} "
            "GROUP BY i.item_id ORDER BY COUNT(*) DESC").fetchall()
        own_first = conn.execute("SELECT MIN(sold_at) FROM main.sales").fetchone()[0]
        result = {
            "format": kind,
            "other_sales": conn.execute("SELECT COUNT(*) FROM incoming").fetchone()[0],
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
            # A phase-specific match before the plain name, so a sale that
            # fits both lands on the phase.
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO main.sales (sale_id, item_id, "
                    + ", ".join(COLUMNS[1:]) + ") "
                    "SELECT s.sale_id, i.item_id, i.name, s.price_cents, s.price, "
                    "s.float_value, s.paint_seed, s.paint_index, s.sold_at, "
                    "s.sold_at_estimated, s.stickers_json, s.raw_json, s.scraped_at "
                    f"{_NEW} ORDER BY i.paint_index IS NULL")
                result["inserted"] = cur.rowcount
        return result
    finally:
        conn.close()
