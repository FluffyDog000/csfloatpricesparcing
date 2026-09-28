"""Export everything the bot knows about one item, for working an example by hand.

The extraction lives in src/item_dump.py, shared with the bot's /dump command
so a file fetched from the phone and one written on the server cannot disagree.

    .venv/bin/python tools/dump_item.py "AWP | Printstream (Field-Tested)"
    .venv/bin/python tools/dump_item.py "AWP | Printstream (Field-Tested)" --days 90
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.item_dump import ItemNotFound, render, resolve, safe_filename  # noqa: E402


def find_db(explicit: str | None) -> str:
    """Where the bot keeps its database.

    Asked of the project's own config loader rather than guessed: a list of
    likely filenames here missed the real one (data/csfloat_sales.db) and left
    the caller with nothing to go on. The loader is the single place that
    knows, and it already reads CSFLOAT_DB_PATH and config.yaml.
    """
    if explicit:
        if not os.path.exists(explicit):
            sys.exit(f"{explicit}: нет такого файла")
        return explicit

    tried = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from src.config import load_config
        configured = str(load_config().db_path)
        if os.path.exists(configured):
            return configured
        tried.append(configured)
    except Exception as exc:  # noqa: BLE001 - a broken config must not hide the path
        tried.append(f"(конфиг не прочитался: {type(exc).__name__}: {exc})")

    fallback = os.path.join(root, "data", "csfloat_sales.db")
    if os.path.exists(fallback):
        return fallback
    tried.append(fallback)

    print("не нашёл базу. Искал:", file=sys.stderr)
    for path in dict.fromkeys(tried):     # same path twice reads like a bug
        print(f"  {path}", file=sys.stderr)
    print("\nукажи путь явно:  --db /путь/до.db", file=sys.stderr)
    print("найти её можно так: find ~ -name '*.db' -size +1k 2>/dev/null",
          file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="market_hash_name, in quotes")
    ap.add_argument("--db", help="path to the database")
    ap.add_argument("--days", type=int, default=60,
                    help="how much sales history to include (default 60)")
    ap.add_argument("--out", help="write here instead of <item>.txt")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{find_db(args.db)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        item_id, name = resolve(con, args.name)
    except ItemNotFound as miss:
        print(f"не нашёл предмет {miss.name!r}. Есть такие:", file=sys.stderr)
        for candidate in miss.candidates:
            print(f"  {candidate}", file=sys.stderr)
        return 1

    body = render(con, item_id, name, args.days)
    out_path = args.out or safe_filename(name, args.days)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)

    size_kb = os.path.getsize(out_path) / 1024
    print(name)
    for line in body.splitlines():
        if line.startswith("## "):
            print("  " + line[3:])
    print(f"\nзаписано в {out_path} ({size_kb:.0f} КБ)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
