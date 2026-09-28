"""How many orders there are to place across the whole database, and where.

Scores every tracked item, not just the ones on the analysis list, and says how
many rungs clear the margin floor, what they would cost, why the rest were
refused, and whether the answer rests on two items or twenty.

Reads the database only. Runs while the account is rate limited or flagged.

    .venv/bin/python tools/census.py
    .venv/bin/python tools/census.py --top 15      # show more items
    .venv/bin/python tools/census.py --blind       # include the unswept ones
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.census import census, look  # noqa: E402


def project_imports():
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


def block(title: str, found: dict, note: str = "") -> None:
    print(f"{title}")
    if note:
        print(f"  {note}")
    if not found["items"]:
        print("  нет таких предметов\n")
        return
    print(f"  предметов: {found['items']}, из них с ордерами: "
          f"{found['with_orders']}")
    print(f"  ступеней оценено: {found['rungs']}, проходных: {found['taken']}")
    print(f"  капитал: ${found['capital']:,.2f}")
    if found["capital"]:
        print(f"  доля крупнейшего предмета: "
              f"{found['concentration'] * 100:.0f}%")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10,
                    help="how many items to list by capital (default 10)")
    ap.add_argument("--blind", action="store_true",
                    help="also list the items with no order book on record")
    ap.add_argument("--db", help="path to the database")
    args = ap.parse_args()

    load_config, Database = project_imports()
    config = load_config()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    try:
        from src.orders import wear_range
        from src.settings import limits as read_limits
        from src.settings import params as read_params

        params = read_params(db)
        limits = read_limits(db)
        names = [r["market_hash_name"] for r in db.conn.execute(
            "SELECT DISTINCT market_hash_name FROM items ORDER BY 1")]
        if not names:
            print("в базе нет предметов.")
            return 1

        print(f"параметры: комиссия {params.fee * 100:.1f}%, "
              f"мин. маржа {params.min_margin * 100:.1f}%, "
              f"окно {params.window_days:.0f} дн, "
              f"порог выборки {params.min_sample}")
        print(f"лимиты: капитал ${limits.total_capital:,.0f}, "
              f"ордеров {limits.max_orders}, "
              f"на предмет {limits.max_orders_per_item}\n")

        items = []
        for name in names:
            item_id = db.get_item_id(name)
            if item_id is None:
                continue
            sales = _sales(db, item_id)
            orders = db.buy_orders(item_id)
            try:
                depth = db.listing_depth(item_id)
            except Exception:  # noqa: BLE001 - an older DB has no such table
                depth = []
            items.append(look(name, sales, orders, depth,
                              wear_range(name), params))

        found = census(items)
        block("СО СТАКАНОМ — конкуренция измерена, цифрам можно верить",
              found["measured"])
        block("БЕЗ СТАКАНА — конкуренция неизвестна, потолок берётся целиком",
              found["blind"],
              "эти предметы ни разу не обходились: ставка выходит завышенной, "
              "и складывать их с верхними нельзя")

        listed = [i for i in items if not i.error and (args.blind or not i.blind)]
        listed.sort(key=lambda i: (-i.capital, i.name))
        if listed:
            print(f"ПРЕДМЕТЫ (до {args.top} по капиталу)")
            print(f"  {'проходн':>7} {'ступ':>5} {'капитал':>11} "
                  f"{'ранг':>8} {'продаж':>7} {'ордеров':>8}  предмет")
            for item in listed[:args.top]:
                mark = " ?" if item.blind else "  "
                print(f"  {item.taken:7d} {item.rungs:5d} "
                      f"${item.capital:10,.2f} {item.best_rank:8.4f} "
                      f"{item.sales:7d} {item.orders:8d}{mark}{item.name}")
            if args.blind:
                print("  ? — стакан не читался, конкуренция неизвестна")
            print()

        print("ПОЧЕМУ СТУПЕНИ ОТКЛОНЕНЫ")
        total = sum(found["reasons"].values()) or 1
        for label, count in found["reasons"].items():
            print(f"  {count:5d} ({count / total * 100:4.1f}%)  {label}")
        print()

        if found["failed"]:
            print("НЕ ОЦЕНЕНО")
            for name, why in found["failed"]:
                print(f"  {name}: {why}")
            print()

        capital = found["measured"]["capital"]
        if limits.total_capital and capital:
            share = capital / limits.total_capital
            print(f"Проходные ордера по измеренным предметам заняли бы "
                  f"${capital:,.2f} — {share * 100:.0f}% лимита "
                  f"${limits.total_capital:,.0f}.")
            if share < 0.5:
                print("Лимит не связывает: ранг сейчас решает только порядок "
                      "показа, а не состав. Узкое место — не деньги, а число "
                      "предметов со стаканом.")
        return 0
    finally:
        db.close()


def _sales(db, item_id: int) -> list[dict]:
    """Sales with the age the pricing reads."""
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for row in db.conn.execute(
            "SELECT price, float_value, sold_at FROM sales WHERE item_id = ? "
            "AND float_value IS NOT NULL AND price IS NOT NULL", (item_id,)):
        row = dict(row)
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
