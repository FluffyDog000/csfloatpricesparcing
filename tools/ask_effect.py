"""Measure what the stored lot prices are worth, without asking CSFloat anything.

Prices every rung twice from the database as it stands: once with every lot's
price, and once with the band reduced to what a row written before the `asks`
column holds - a count and a minimum. The difference is what the column buys.

Reads only. Runs while the account is rate limited, flagged or offline.

    .venv/bin/python tools/ask_effect.py                  # every item that has prices
    .venv/bin/python tools/ask_effect.py --item "AK-47 | Inheritance (Minimal Wear)"
    .venv/bin/python tools/ask_effect.py --all-rungs      # not just the ones that moved
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ask_effect import compare, summarise  # noqa: E402


def money(value) -> str:
    return "—" if value is None else f"${value:.2f}"


def percent(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def project_imports():
    """The bot's own modules, or an answer that names the real problem."""
    try:
        from src.config import load_config
        from src.db import Database
        return load_config, Database
    except ModuleNotFoundError as exc:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        venv = os.path.join(root, ".venv", "bin", "python")
        print(f"не хватает модуля '{exc.name}' — это системный python3, "
              f"а зависимости стоят в окружении проекта.", file=sys.stderr)
        if os.path.exists(venv):
            print(f"\nзапусти так:\n  {os.path.relpath(venv, os.getcwd())} "
                  f"{os.path.relpath(__file__, os.getcwd())} "
                  f"{' '.join(sys.argv[1:])}".rstrip(), file=sys.stderr)
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--item", action="append", default=[],
                    help="only this item (repeatable)")
    ap.add_argument("--all-rungs", action="store_true",
                    help="print every rung, not only the ones that moved")
    ap.add_argument("--db", help="path to the database")
    args = ap.parse_args()

    load_config, Database = project_imports()
    config = load_config()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    try:
        from src.orders import wear_range
        from src.settings import params as read_params

        params = read_params(db)
        names = args.item or [
            r["market_hash_name"] for r in db.conn.execute(
                "SELECT DISTINCT market_hash_name FROM items i JOIN "
                "listing_depth d ON d.item_id = i.id ORDER BY 1").fetchall()]
        if not names:
            print("в базе нет ни одного предмета со стаканом продаж.")
            return 1

        from src.pricing import Params as PricingParams  # noqa: F401
        from src import ladder as _ladder

        p = _ladder.Params(fee=params.fee, min_margin=params.min_margin,
                           window_days=params.window_days,
                           min_sample=params.min_sample)
        print(f"параметры: комиссия {percent(params.fee)}, "
              f"мин. маржа {percent(params.min_margin)}, "
              f"окно {params.window_days:.0f} дн, "
              f"порог выборки {params.min_sample}\n")

        totals = {"rungs": 0, "moved": 0, "opened": 0, "closed": 0}
        for name in names:
            item_id = db.get_item_id(name)
            if item_id is None:
                print(f"{name}: не отслеживается")
                continue
            depth = db.listing_depth(item_id)
            if not any(b.get("asks") for b in depth):
                print(f"{name}: цен лотов нет — сравнивать не с чем\n")
                continue
            sales = _sales(db, item_id, params.window_days)
            orders = db.buy_orders(item_id)
            changes = compare(sales, orders, wear_range(name), depth, p)
            found = summarise(changes)
            for key in totals:
                totals[key] += found[key]

            print(f"{name}")
            print(f"  полос со стаканом: {len(depth)}, "
                  f"продаж в окне: {len(sales)}, чужих ордеров: {len(orders)}")
            print(f"  ступеней: {found['rungs']}, сдвинулось: {found['moved']}, "
                  f"открылось: {found['opened']}, закрылось: {found['closed']}")
            if found["moved"]:
                print(f"  лучший сдвиг потолка: {money(found['best_gain'])}, "
                      f"худший: {money(found['worst_gain'])}")
            print(f"  проходных ступеней было {found['take_before']}, "
                  f"стало {found['take_after']}")

            shown = [c for c in changes
                     if args.all_rungs or abs(c.ceiling_gain) > 1e-9 or c.flipped]
            if shown:
                print(f"    {'верх':>6} {'лотов':>6} {'с ценой':>8} "
                      f"{'выход было':>11} {'стало':>9} "
                      f"{'потолок было':>13} {'стало':>9} {'маржа':>14}")
            for c in shown:
                flag = f"  <- {c.flipped}" if c.flipped else ""
                print(f"    {c.top:6.2f} {c.lots:6d} {c.priced:8d} "
                      f"{money(c.blind_exit):>11} {money(c.full_exit):>9} "
                      f"{money(c.blind_ceiling):>13} {money(c.full_ceiling):>9} "
                      f"{percent(c.blind_margin):>6} -> {percent(c.full_margin):>5}"
                      f"{flag}")
            print()

        print(f"ИТОГО по {len(names)} предмет(ам): ступеней {totals['rungs']}, "
              f"сдвинулось {totals['moved']}, открылось {totals['opened']}, "
              f"закрылось {totals['closed']}.")
        if not totals["moved"]:
            print("Цены лотов не изменили ни одной ступени: очередь на этих "
                  "полосах уходит за блокировку, и потолок держит медиана "
                  "истории, а не стакан.")
        return 0
    finally:
        db.close()


def _sales(db, item_id: int, window_days: float) -> list[dict]:
    """Sales with the age the pricing reads, same as the dashboard builds."""
    import datetime as dt

    rows = [dict(r) for r in db.conn.execute(
        "SELECT price, float_value, sold_at FROM sales WHERE item_id = ? "
        "AND float_value IS NOT NULL AND price IS NOT NULL", (item_id,))]
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for row in rows:
        try:
            when = dt.datetime.fromisoformat(str(row["sold_at"]))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        row["age_days"] = (now - when).total_seconds() / 86400
        out.append(row)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
