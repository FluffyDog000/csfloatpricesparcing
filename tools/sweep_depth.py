"""Refresh the sell side so every lot's price is stored, not just the cheapest.

Runs on the API key and the documented listings endpoint - it does not touch
the cookie or the residential route the order book needs, so it can be run
while the collector is working and will not compete for that quota.

By default it sweeps only the items whose stored bands counted lots but kept no
prices, which is what the rows written before the `asks` column look like. Run
it again after a rate limit: the items it finished are skipped the second time.

    python3 tools/sweep_depth.py                    # the analysis list
    python3 tools/sweep_depth.py --all              # every active item
    python3 tools/sweep_depth.py --item "AWP | Printstream (Field-Tested)"
    python3 tools/sweep_depth.py --dry-run          # say what it would read
    python3 tools/sweep_depth.py --force --limit 20 # re-read, 20 items at most
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.depth_sweep import pick, sweep  # noqa: E402

ANALYSIS_KEY = "analysis_items"


def analysis_items(db) -> list[str]:
    try:
        names = json.loads(db.get_setting(ANALYSIS_KEY) or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)]


def active_items(db) -> list[str]:
    rows = db.conn.execute(
        "SELECT market_hash_name FROM items WHERE active = 1 "
        "ORDER BY market_hash_name").fetchall()
    return [r["market_hash_name"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--item", action="append", default=[],
                    help="sweep this item (repeatable); overrides the list")
    ap.add_argument("--all", action="store_true",
                    help="every active item, not only the analysis list")
    ap.add_argument("--force", action="store_true",
                    help="re-read items whose prices are already stored")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many items (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be read and exit")
    ap.add_argument("--db", help="path to the database")
    args = ap.parse_args()

    from src.config import load_config
    from src.db import Database

    config = load_config()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    try:
        if args.item:
            names = args.item
        elif args.all:
            names = active_items(db)
        else:
            names = analysis_items(db)
            if not names:
                print("Список анализа пуст — добавь предметы на вкладке "
                      "«Анализ» или запусти с --all.")
                return 1

        wanted, skipped = pick(db, names, force=args.force)
        for name, why in skipped:
            print(f"  пропуск  {name}: {why}")
        if args.limit and len(wanted) > args.limit:
            print(f"  ограничение --limit {args.limit}: "
                  f"{len(wanted) - args.limit} предмет(ов) осталось на потом")
            wanted = wanted[:args.limit]
        if not wanted:
            print("Нечего обходить: у всех предметов цены лотов уже сохранены.")
            return 0

        print(f"Обход продажной стороны: {len(wanted)} предмет(ов), "
              f"по ключу API.")
        if args.dry_run:
            for name, _ in wanted:
                print(f"  прочитал бы  {name}")
            return 0

        from src.collector import Collector
        from src.csfloat_client import CSFloatClient

        collector = Collector(config, db, CSFloatClient(config.http, config.polling))

        def report(name: str, result: dict) -> None:
            if result.get("bands"):
                print(f"  {name}: {result['bands']} полос, "
                      f"{result['listings']} лотов, "
                      f"{result['requests']} запрос(ов)"
                      + (f" — {result['error']}" if result.get("error") else ""))
            else:
                print(f"  {name}: НЕ ПРОЧИТАНО — "
                      f"{result.get('error') or 'полос не прочитано'}")

        out = sweep(collector, wanted, report)
        print(f"\nПрочитано {len(out['swept'])} из {len(wanted)}: "
              f"{out['listings']} лотов за {out['requests']} запрос(ов).")
        if out["failed"]:
            print(f"Не прочитано: {len(out['failed'])}")
        if out["stopped"]:
            print(out["stopped"])
            return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
