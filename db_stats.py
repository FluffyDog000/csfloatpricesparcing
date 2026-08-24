#!/usr/bin/env python3
"""Where the database size actually went, and whether dedup is working.

Usage:
    python db_stats.py                # отчёт
    python db_stats.py --purge-raw    # выбросить raw_json и сжать файл
"""
from __future__ import annotations

import argparse
import os
import sqlite3

from src.config import load_config


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(n) < 1024 or unit == "ГБ":
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024
    return f"{n:.1f} ГБ"


def col_bytes(conn, table: str, column: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(SUM(LENGTH(CAST({column} AS BLOB))), 0) FROM {table}"
    ).fetchone()
    return int(row[0])


def report(conn, path: str) -> None:
    size = os.path.getsize(path)
    wal = path + "-wal"
    print(f"Файл базы : {human(size)}"
          + (f"  (+ WAL {human(os.path.getsize(wal))})" if os.path.exists(wal) else ""))

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print("\nСтроки по таблицам")
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<12} {n:>10,}".replace(",", " "))

    # Where the sales table's bytes sit.
    total_sales = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    if not total_sales:
        print("\nПродаж пока нет.")
        return

    raw = col_bytes(conn, "sales", "raw_json")
    stick = col_bytes(conn, "sales", "stickers_json")
    names = col_bytes(conn, "sales", "market_hash_name")
    ids = col_bytes(conn, "sales", "sale_id")
    rest = sum(col_bytes(conn, "sales", c) for c in
               ("price_cents", "price", "float_value", "paint_seed",
                "paint_index", "sold_at", "sold_at_estimated", "scraped_at"))
    known = raw + stick + names + ids + rest

    print(f"\nИз чего состоит таблица sales ({total_sales:,} строк)".replace(",", " "))
    for label, value in (("raw_json (сырой ответ)", raw), ("stickers_json", stick),
                         ("market_hash_name", names), ("sale_id", ids),
                         ("все полезные поля", rest)):
        share = 100.0 * value / known if known else 0
        bar = "#" * int(share / 3)
        print(f"  {label:<24} {human(value):>10}  {share:>5.1f}%  {bar}")
    print(f"  {'итого данных':<24} {human(known):>10}")
    print(f"  на одну продажу: {human(known / total_sales)} "
          f"(из них raw_json {human(raw / total_sales)})")

    # Is dedup actually holding? Same sale should never be stored twice.
    dup = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT item_id, price_cents, float_value, paint_seed, sold_at,
                   COUNT(*) AS c
            FROM sales
            GROUP BY item_id, price_cents, float_value, paint_seed, sold_at
            HAVING c > 1)
    """).fetchone()[0]
    extra = conn.execute("""
        SELECT COALESCE(SUM(c - 1), 0) FROM (
            SELECT COUNT(*) AS c FROM sales
            GROUP BY item_id, price_cents, float_value, paint_seed, sold_at
            HAVING c > 1)
    """).fetchone()[0]
    print("\nДедупликация")
    if dup:
        print(f"  ⚠ повторов: {dup:,} групп, лишних строк {extra:,} "
              f"({100.0 * extra / total_sales:.1f}%)".replace(",", " "))
        print("    одна и та же продажа записана несколько раз — это баг, покажи вывод")
    else:
        print("  ✅ повторов нет: каждая продажа записана один раз")

    # Growth: how fast is it filling up?
    rows = conn.execute(
        "SELECT DATE(scraped_at) AS d, COUNT(*) FROM sales "
        "WHERE scraped_at >= DATE('now', '-7 day') GROUP BY d ORDER BY d").fetchall()
    if rows:
        print("\nНовых продаж по дням (последняя неделя)")
        per_row = known / total_sales
        for day, n in rows:
            print(f"  {day}  {n:>7,}".replace(",", " ") + f"  ≈ {human(n * per_row)}")
        avg = sum(n for _, n in rows) / len(rows)
        print(f"  в среднем {avg:,.0f}/сут ≈ {human(avg * per_row)}/сут "
              f"≈ {human(avg * per_row * 30)}/мес".replace(",", " "))
        if raw:
            saved = raw / total_sales
            print(f"  без raw_json было бы ≈ {human(avg * (per_row - saved))}/сут")


def purge_raw(conn, path: str) -> None:
    before = os.path.getsize(path)
    n = conn.execute("SELECT COUNT(*) FROM sales WHERE raw_json IS NOT NULL").fetchone()[0]
    if not n:
        print("raw_json уже пуст — чистить нечего.")
        return
    print(f"Очищаю raw_json у {n:,} продаж...".replace(",", " "))
    conn.execute("UPDATE sales SET raw_json = NULL")
    conn.commit()
    print("Сжимаю файл (VACUUM), это может занять минуту...")
    conn.execute("VACUUM")
    after = os.path.getsize(path)
    print(f"Было {human(before)} -> стало {human(after)} "
          f"(освобождено {human(before - after)})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Что занимает место в базе")
    ap.add_argument("--purge-raw", action="store_true",
                    help="удалить сохранённые сырые ответы и сжать файл")
    ap.add_argument("--db", help="путь к базе (по умолчанию из конфига)")
    args = ap.parse_args()

    path = args.db or load_config().db_path
    if not os.path.exists(path):
        print(f"База не найдена: {path}")
        return 1
    conn = sqlite3.connect(path)
    try:
        if args.purge_raw:
            purge_raw(conn, path)
        else:
            report(conn, path)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
