"""На какую сумму продаются флотовки: продажи в лучших сотых float, которые
ушли заметно дороже обычной цены предмета. Только чтение базы.

    .venv/bin/python tools/float_volume.py
    .venv/bin/python tools/float_volume.py --days 14 --best 1 --premium 20 --top 40
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
    ap.add_argument("--best", type=int, default=2,
                    help="сколько лучших сотых float считать флотом, по умолчанию 2")
    ap.add_argument("--premium", type=float, default=10,
                    help="дороже обычной цены хотя бы на %%, по умолчанию 10")
    ap.add_argument("--top", type=int, default=25, help="сколько предметов показать")
    a = ap.parse_args()

    db = Database(load_config().db_path)
    r = report(db, days=a.days, best=a.best, premium=a.premium / 100)
    t, f = r["total"], r["float"]
    print(f"За {a.days:g} дней · флот = {a.best} лучших сотых float, дороже обычной "
          f"цены на {a.premium:g}%+\n")
    print(f"Все продажи:     {t['n']:>7} шт · ${t['usd']:>12,.2f}")
    print(f"Флотовки:        {f['n']:>7} шт · ${f['usd']:>12,.2f} · {f['share']}% оборота · "
          f"{f['items']} предметов · ~${f['per_day']:,.0f} в день")
    print(f"  из них переплата за float: ${f['over']:,.2f}\n")
    print("По обычной цене предмета:")
    print(f"  {'цена':>12} {'шт':>6} {'сумма, $':>12} {'переплата, $':>13}")
    for b in r["bands"]:
        print(f"  {b['band']:>12} {b['n']:>6} {b['usd']:>12,.2f} {b['over']:>13,.2f}")
    print(f"\nПредметы с наибольшей суммой флотовок (топ {a.top}):")
    print(f"  {'шт':>4} {'сумма, $':>10} {'обычная, $':>10} {'дороже':>7}  предмет")
    for it in r["items"][:a.top]:
        print(f"  {it['n']:>4} {it['usd']:>10,.2f} {it['base']:>10,.2f} {it['pct']:>6.0f}%  "
              f"{it['name']}")
    db.close()


if __name__ == "__main__":
    main()
