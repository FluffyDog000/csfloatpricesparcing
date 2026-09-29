"""Would one listing at the lowest float have shown us the whole book?

An order is scoped to a float range, and a listing shows every order whose
range covers it. So a listing at the bottom of the wear range reveals every
order that starts there - which on one item was 34 of 36 - and misses only
those scoped to start higher up. If that holds, the buy-order sweep collapses
from a request per 0.01 band to one.

The question worth answering is not what share of orders would be missed but
whether the bid would change: an order we never saw costs nothing if a dearer
one covers the same float anyway. So this compares, rung by rung, the best
rival in the whole book against the best rival in what one listing would have
shown, and reports where the two disagree.

Reads the stored book. No requests.

    .venv/bin/python tools/book_probe.py "AK-47 | Asiimov (Field-Tested)"
    .venv/bin/python tools/book_probe.py --all
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def covers(order: dict, value: float) -> bool:
    """Whether this order would appear on a listing at `value`.

    An order with no float scope takes anything, so it shows on every listing.
    """
    lo, hi = order.get("float_min"), order.get("float_max")
    if lo is None or hi is None:
        return True
    return float(lo) <= value <= float(hi)


def check(name: str, orders: list[dict], span, step: float = 0.01) -> dict:
    """One item: what a bottom-of-range listing shows, and what it costs."""
    from src.ladder import rival_bid

    lo, hi = span
    # The listing we would actually get: the cheapest lot CSFloat returns for
    # the band, which sits at the bottom of the range but not exactly on it.
    probe = round(lo + step / 2, 4)
    visible = [o for o in orders if covers(o, probe)]
    missed = [o for o in orders if o not in visible]

    rows = []
    top = round(lo + step, 4)
    while top <= hi + 1e-9:
        whole = rival_bid(orders, top)
        partial = rival_bid(visible, top)
        if abs(whole - partial) > 1e-9:
            rows.append({"top": top, "whole": whole, "partial": partial})
        top = round(top + step, 4)

    return {"item": name, "orders": len(orders), "visible": len(visible),
            "missed": missed, "wrong": rows, "probe": probe}


def report(found: dict) -> None:
    print(f"\n{found['item']}")
    print(f"  ордеров в книге: {found['orders']}, "
          f"видно с одного лота (float {found['probe']:.3f}): "
          f"{found['visible']}")
    if not found["orders"]:
        print("  книга пуста — сравнивать нечего")
        return

    missed = found["missed"]
    if missed:
        print(f"  не попали бы в выдачу: {len(missed)}")
        for order in sorted(missed, key=lambda o: -float(o["price"]))[:8]:
            lo, hi = order.get("float_min"), order.get("float_max")
            scope = (f"{float(lo):.4f}-{float(hi):.4f}"
                     if lo is not None and hi is not None else "без привязки")
            print(f"     ${float(order['price']):>8.2f}  {scope}")
        if len(missed) > 8:
            print(f"     … и ещё {len(missed) - 8}")

    wrong = found["wrong"]
    if not wrong:
        print("  СТАВКА НЕ ИЗМЕНИЛАСЬ НИ НА ОДНОЙ СТУПЕНИ")
        return
    print(f"  ставка разошлась бы на {len(wrong)} ступен(ях):")
    print(f"     {'верх':>6} {'соперник в книге':>18} {'по одному лоту':>16}")
    for row in wrong:
        seen = f"${row['partial']:.2f}" if row["partial"] else "никого"
        print(f"     {row['top']:6.2f} {'$%.2f' % row['whole']:>18} {seen:>16}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="market_hash_name, in quotes")
    ap.add_argument("--all", action="store_true",
                    help="every item whose book has been read")
    ap.add_argument("--db", help="path to the database")
    args = ap.parse_args()
    if not args.name and not args.all:
        ap.error("укажи предмет или --all")

    try:
        from src.config import load_config
        from src.db import Database
        from src.orders import wear_range
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    try:
        if args.all:
            names = [r["market_hash_name"] for r in db.conn.execute(
                "SELECT DISTINCT market_hash_name FROM items i JOIN buy_orders b"
                " ON b.item_id = i.id ORDER BY 1")]
        else:
            names = [args.name]
        if not names:
            print("ни у одного предмета книга ещё не читалась.")
            return 1

        totals = {"items": 0, "orders": 0, "visible": 0, "wrong": 0,
                  "clean": 0}
        for name in names:
            item_id = db.get_item_id(name)
            if item_id is None:
                print(f"{name}: не отслеживается")
                continue
            span = wear_range(name)
            if not span:
                print(f"{name}: без износа в названии")
                continue
            found = check(name, db.buy_orders(item_id), span)
            report(found)
            totals["items"] += 1
            totals["orders"] += found["orders"]
            totals["visible"] += found["visible"]
            totals["wrong"] += len(found["wrong"])
            totals["clean"] += 0 if found["wrong"] else 1

        if totals["items"] > 1:
            share = (totals["visible"] / totals["orders"] * 100
                     if totals["orders"] else 0)
            print(f"\nИТОГО по {totals['items']} предмет(ам)")
            print(f"  ордеров {totals['orders']}, видно с одного лота "
                  f"{totals['visible']} ({share:.0f}%)")
            print(f"  предметов без единого расхождения: {totals['clean']} "
                  f"из {totals['items']}")
            print(f"  ступеней с неверной ставкой: {totals['wrong']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
