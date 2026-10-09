"""Дополнить историю продаж своих предметов из базы знакомого: этого же бота
или cs2arb (FloatHistory, arb.db из архива).

Знакомому не нужно отдавать всю базу (там его сделки и настройки) — только
продажи, отдельным файлом, на работающем боте:

    cd /root/csfloatpricesparcing && .venv/bin/python -c "
    import sqlite3; d = sqlite3.connect('/root/sales_export.db')
    d.execute(\"ATTACH 'data/csfloat_sales.db' AS s\")
    d.execute('CREATE TABLE sales AS SELECT sale_id, market_hash_name, price_cents, price, '
              'float_value, paint_seed, paint_index, sold_at, sold_at_estimated, '
              'stickers_json, scraped_at FROM s.sales'); d.commit()"

Сначала без --apply: покажет, сколько продаж и по скольким предметам добавится,
ничего не меняя. Чужие предметы, сделки, ордера и настройки не копируются.

    .venv/bin/python tools/merge_sales.py /root/friend.db
    sudo systemctl stop csfloat-collector
    cp data/csfloat_sales.db data/before_merge.db
    .venv/bin/python tools/merge_sales.py /root/friend.db --apply
    sudo systemctl start csfloat-collector
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config  # noqa: E402
from src.merge_sales import merge  # noqa: E402


def day(iso: str | None) -> str:
    return (iso or "—")[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("other", help="копия базы знакомого (csfloat_sales.db)")
    ap.add_argument("--apply", action="store_true", help="записать в свою базу")
    args = ap.parse_args()
    if not os.path.isfile(args.other):
        sys.exit(f"Нет файла {args.other}")
    own = str(load_config().db_path)
    if os.path.realpath(own) == os.path.realpath(args.other):
        sys.exit("Это твоя же база")

    r = merge(own, args.other, apply=args.apply)
    print(f"Формат чужой базы: {r['format']}, продаж CSFloat в ней: {r['other_sales']}")
    print(f"Твоих предметов: {r['own_items']}, из них есть у знакомого: {r['covered_items']}")
    print(f"Твои продажи начинаются с {day(r['own_first'])}, "
          f"у знакомого по твоим предметам — с {day(r['other_first'])}")
    o = r["overlap"]
    print(f"Сверка общих продаж (есть в обеих базах): {o['both']}, цена совпала: "
          f"{o['same_price']}, float совпал: {o['same_float']}")
    if o["both"] and min(o["same_price"], o["same_float"]) < 0.98 * o["both"]:
        print("   ⚠ цены или float расходятся больше чем у 2% общих продаж — "
              "не записывай, пришли этот вывод")
    print(f"Новых продаж: {r['new_sales']} по {r['items_gaining']} предметам")
    for t in r["top"]:
        print(f"   +{t['new']:>5}  с {day(t['first'])}  {t['name']}")
    if args.apply:
        print(f"\nЗаписано: {r['inserted']}")
    else:
        print("\nНичего не записано. Чтобы добавить — тот же запуск с --apply "
              "(сборщик остановить, базу скопировать).")


if __name__ == "__main__":
    main()
