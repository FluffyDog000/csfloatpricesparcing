#!/usr/bin/env python3
"""Выгрузить историю одного предмета (продажи + стакан) отдельным файлом.

Вся база весит десятки мегабайт и её неудобно пересылать. Здесь выгружается
только один скин: продажи без сырых ответов, текущий стакан ордеров и немного
метаданных — обычно это сотни килобайт. Секретов (cookie, прокси, настройки)
в выгрузке нет: берутся только таблицы items / sales / buy_orders.

Файл кладётся в data/exports/ и, если Telegram настроен в .env, сразу
отправляется в бота.

Usage:
    python export_item.py "Broken Fang Gloves | Jade"   # хватает куска названия
    python export_item.py "Jade" --days 90              # только последние 90 дней
    python export_item.py "Jade" --no-telegram          # только файл на диске
    python export_item.py --list                        # что вообще есть в базе
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.telegram import TelegramClient


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(n) < 1024 or unit == "ГБ":
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024
    return f"{n:.1f} ГБ"


def slugify(name: str) -> str:
    """Имя файла из названия скина: только латиница/цифры, звёзды и | выкидываем."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "item"


def find_item(conn: sqlite3.Connection, needle: str) -> dict:
    """Точное совпадение, иначе поиск по куску названия (без учёта регистра)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, market_hash_name, added_at, active, last_polled_at, "
        "pattern_sensitive, listing_id FROM items WHERE market_hash_name = ?",
        (needle,))]
    if not rows:
        # LIKE has its own wildcards; a name like "AK-47 | Case Hardened" has none,
        # but escape them anyway so a stray % or _ can't match half the table.
        pattern = needle
        for ch in ("\\", "%", "_"):
            pattern = pattern.replace(ch, "\\" + ch)
        rows = [dict(r) for r in conn.execute(
            "SELECT id, market_hash_name, added_at, active, last_polled_at, "
            "pattern_sensitive, listing_id FROM items "
            "WHERE market_hash_name LIKE ? ESCAPE '\\' ORDER BY market_hash_name",
            (f"%{pattern}%",))]
    if not rows:
        raise SystemExit(f"Предмет не найден: {needle!r}. Список: python export_item.py --list")
    if len(rows) > 1:
        print(f"Под '{needle}' подходит несколько предметов — уточни:", file=sys.stderr)
        for r in rows[:20]:
            print(f"  {r['market_hash_name']}", file=sys.stderr)
        raise SystemExit(2)
    return rows[0]


def collect(conn: sqlite3.Connection, item: dict, days: int | None) -> dict:
    """Собрать выгрузку. raw_json намеренно не берём — это 85% веса базы."""
    where, params = "item_id = ?", [item["id"]]
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where += " AND (sold_at IS NULL OR sold_at >= ?)"
        params.append(since)

    sales = []
    for r in conn.execute(
        f"SELECT sale_id, price_cents, price, float_value, paint_seed, paint_index, "
        f"sold_at, sold_at_estimated, stickers_json, scraped_at "
        f"FROM sales WHERE {where} ORDER BY sold_at", params
    ):
        row = dict(r)
        stickers = row.pop("stickers_json", None)
        if stickers:
            try:
                row["stickers"] = json.loads(stickers)
            except ValueError:
                row["stickers"] = stickers
        row["sold_at_estimated"] = bool(row["sold_at_estimated"])
        sales.append({k: v for k, v in row.items() if v not in (None, False)})

    orders = [dict(r) for r in conn.execute(
        "SELECT price, qty, float_min, float_max, paint_seed, position, fetched_at "
        "FROM buy_orders WHERE item_id = ? ORDER BY position", (item["id"],))]

    prices = [s["price"] for s in sales if s.get("price") is not None]
    return {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "schema": "csfloat-item-export/1",
        "item": {
            "market_hash_name": item["market_hash_name"],
            "added_at": item["added_at"],
            "last_polled_at": item["last_polled_at"],
            "pattern_sensitive": bool(item["pattern_sensitive"]),
            "listing_id": item["listing_id"],
        },
        "window_days": days,
        "counts": {"sales": len(sales), "buy_orders": len(orders)},
        "price_range_usd": ([min(prices), max(prices)] if prices else None),
        "sales": sales,
        "buy_orders": orders,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Выгрузка одного предмета в файл/Telegram")
    ap.add_argument("name", nargs="?", help="название или его часть")
    ap.add_argument("--days", type=int, help="только продажи за последние N дней")
    ap.add_argument("--out", help="куда положить файл (по умолчанию data/exports/)")
    ap.add_argument("--no-telegram", action="store_true", help="не отправлять в бота")
    ap.add_argument("--list", action="store_true", help="показать предметы в базе")
    ap.add_argument("--db", help="путь к базе (по умолчанию из конфига)")
    args = ap.parse_args()

    config = load_config()
    path = str(args.db or config.db_path)
    if not os.path.exists(path):
        print(f"База не найдена: {path}")
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.list:
            for r in conn.execute(
                "SELECT i.market_hash_name, COUNT(s.sale_id) AS n FROM items i "
                "LEFT JOIN sales s ON s.item_id = i.id GROUP BY i.id "
                "ORDER BY n DESC"):
                print(f"  {r['n']:>6}  {r['market_hash_name']}")
            return 0
        if not args.name:
            ap.error("укажи название предмета (или --list)")

        item = find_item(conn, args.name)
        payload = collect(conn, item, args.days)
    finally:
        conn.close()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    dest = Path(args.out) if args.out else (
        config.backups_dir.parent / "exports"
        / f"{slugify(item['market_hash_name'])}-{ts}.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    size = dest.stat().st_size
    print(f"{item['market_hash_name']}")
    print(f"  продаж   : {payload['counts']['sales']:,}".replace(",", " ")
          + (f" (за {args.days} дн.)" if args.days else ""))
    print(f"  ордеров  : {payload['counts']['buy_orders']}")
    print(f"  файл     : {dest}  ({human(size)})")

    if args.no_telegram:
        return 0
    tg = TelegramClient(config.telegram)
    if not tg.configured():
        print("  Telegram не настроен (.env) — файл только на диске.")
        return 0
    caption = (f"{item['market_hash_name']} — "
               f"{payload['counts']['sales']} продаж, "
               f"{payload['counts']['buy_orders']} ордеров")
    if tg.send_document(dest, caption=caption):
        print("  ✅ отправлено в Telegram")
        return 0
    print("  ⚠ отправить в Telegram не удалось (см. лог)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
