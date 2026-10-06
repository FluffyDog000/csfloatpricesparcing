"""Where inside the order's float range the bot's purchases landed.

    .venv/bin/python tools/fill_floats.py
    .venv/bin/python tools/fill_floats.py --list     # every purchase

0% is the range's bottom, 100% its top. If most land near the top, pricing
the ceiling at the top is right; if they spread evenly, it is too cautious.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config  # noqa: E402
from src.db import Database  # noqa: E402
from src.fill_floats import report  # noqa: E402


def show(name: str, b: dict) -> None:
    if not b["count"]:
        print(f"{name}: покупок нет")
        return
    bars = " ".join(f"{i * 10}-{i * 10 + 10}%:{n}" for i, n in enumerate(b["deciles"]))
    print(f"{name}: {b['count']} покупок · медиана {b['median']}% · среднее {b['mean']}% · "
          f"в верхней половине {b['upper_half']}% · в последней сотой {b['top_hundredth']}%")
    print(f"   по десяткам процентов: {bars}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    db = Database(cfg.db_path)
    rows, s, buys = report(db)
    print(f"Покупок на аккаунте: {buys}, из них сопоставлено с ордерами бота: {len(rows)}")
    print("Положение float внутри полосы ордера: 0% — низ, 100% — верх (худший float)\n")
    show("Все", s["all"])
    show("Широкие полосы (шире 0.02)", s["wide"])
    show("Узкие полосы (до 0.02)", s["narrow"])
    if args.list:
        print()
        for r in sorted(rows, key=lambda r: r["position"]):
            print(f"{r['position'] * 100:5.0f}%  {r['float']:.4f} в {r['lo']:.2f}–{r['hi']:.2f}"
                  f"  ${r['price']:.2f}  {r['item']}")
    db.close()


if __name__ == "__main__":
    main()
