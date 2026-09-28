"""Print the sell-side bands exactly as they sit in the database.

The pricing reads these rows and reports a lot count; when that count does not
match what the market shows, the question is which of the two is wrong - and
there is no way to tell without seeing the stored row itself.

    .venv/bin/python tools/show_depth.py "AK-47 | Inheritance (Minimal Wear)"
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="market_hash_name, in quotes")
    ap.add_argument("--db", help="path to the database")
    args = ap.parse_args()

    try:
        from src.config import load_config
        from src.db import Database
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    try:
        item_id = db.get_item_id(args.name)
        if item_id is None:
            print(f"{args.name}: не отслеживается")
            return 1
        rows = db.listing_depth(item_id)
        if not rows:
            print("стакан продаж не собран")
            return 1
        for band in rows:
            asks = band.get("asks") or []
            print(f"\npolosa {band['float_min']:.4f}-{band['float_max']:.4f}"
                  f"  listings={band['listings']}"
                  f"  cheapest={band['cheapest']}"
                  f"  прочитано {band['fetched_at']}")
            print(f"  сохранено цен: {len(asks)}")
            for price, f in asks:
                # A lot with no float recorded is the case that defeats the
                # float filter: it is kept, because unknown counts against us.
                print(f"    ${float(price):>8.2f}   float="
                      f"{'НЕ ЗАПИСАН' if f is None else f'{float(f):.6f}'}")
        return 0
    finally:
        db.close()
if __name__ == "__main__":
    raise SystemExit(main())
