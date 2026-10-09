"""На какую сумму продаются флотовки. Только чтение базы.

Зона флота у каждого предмета своя: от самого низкого float вверх по сотым,
пока медиана сотой дороже обычной цены предмета на --premium % и больше.
Обычная цена — медиана продаж в худшей половине диапазона float.

    .venv/bin/python tools/float_volume.py
    .venv/bin/python tools/float_volume.py --days 60 --premium 3 --top 40
    .venv/bin/python tools/float_volume.py --width 3     # зона = ровно 3 сотые
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config  # noqa: E402
from src.db import Database  # noqa: E402
from src.float_volume import report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30, help="за сколько дней, по умолчанию 30")
    ap.add_argument("--premium", type=float, default=5,
                    help="сотая во флоте, пока её медиана дороже обычной цены на %%; "
                         "по умолчанию 5")
    ap.add_argument("--width", type=int, default=0,
                    help="зона флота ровно столько сотых (0 — по ценам, по умолчанию)")
    ap.add_argument("--top", type=int, default=25, help="сколько предметов показать")
    a = ap.parse_args()

    db = Database(load_config().db_path)
    r = report(db, days=a.days, premium=a.premium / 100, width=a.width)
    t, f = r["total"], r["float"]
    zone = (f"ровно {a.width} сотых от лучшего float" if a.width else
            f"сотые от лучшего float, пока дороже обычной цены на {a.premium:g}%+")
    print(f"За {a.days:g} дней · флот = {zone}\n")
    print(f"Все продажи:     {t['n']:>7} шт · ${t['usd']:>12,.2f}")
    print(f"Флотовки:        {f['n']:>7} шт · ${f['usd']:>12,.2f} · {f['share']}% оборота · "
          f"{f['items']} предметов · ~${f['per_day']:,.0f} в день")
    print(f"  из них переплата за float: ${f['over']:,.2f}\n")
    print("По обычной цене предмета:")
    print(f"  {'цена':>12} {'шт':>6} {'сумма, $':>12} {'переплата, $':>13}")
    for b in r["bands"]:
        print(f"  {b['band']:>12} {b['n']:>6} {b['usd']:>12,.2f} {b['over']:>13,.2f}")
    print(f"\nПредметы с наибольшей суммой флотовок (топ {a.top}):")
    print(f"  {'шт':>4} {'сумма, $':>10} {'обычная, $':>10} {'дороже':>7} {'зона флота':>15}  предмет")
    for it in r["items"][:a.top]:
        print(f"  {it['n']:>4} {it['usd']:>10,.2f} {it['base']:>10,.2f} {it['pct']:>6.0f}% "
              f"{it['floor']:>7.3f}–{it['edge']:<7.3f}  {it['name']}")
    db.close()


if __name__ == "__main__":
    main()
