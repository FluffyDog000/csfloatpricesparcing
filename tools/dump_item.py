"""Export everything the bot knows about one item, for working an example by hand.

Pricing decisions are argued from three tables at once — what the item has sold
for, who is bidding, and what is being asked — and reading them apart hides the
very thing the argument is about. This lays them side by side for one item.

Only item-scoped tables are read. The settings table holds credentials and is
never touched, so the output is safe to share.

    python3 tools/dump_item.py "AWP | Printstream (Field-Tested)"
    python3 tools/dump_item.py "AWP | Printstream (Field-Tested)" --days 90
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def find_db(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CSFLOAT_DB_PATH")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in ("data/csfloat.db", "csfloat.db", "data/prices.db"):
        path = os.path.join(here, candidate)
        if os.path.exists(path):
            return path
    sys.exit("cannot find the database; pass --db /path/to.db")


def resolve_item(con: sqlite3.Connection, name: str) -> tuple[int, str]:
    row = con.execute(
        "SELECT id, market_hash_name FROM items WHERE market_hash_name = ?",
        (name,)).fetchone()
    if row:
        return row[0], row[1]
    # A pasted name often differs by a space or the star; try a loose match
    # before giving up, and show what is there so the caller can pick.
    like = con.execute(
        "SELECT id, market_hash_name FROM items WHERE market_hash_name LIKE ?",
        (f"%{name.strip()}%",)).fetchall()
    if len(like) == 1:
        return like[0][0], like[0][1]
    if like:
        print("several items match:", file=sys.stderr)
        for _, found in like:
            print(f"  {found}", file=sys.stderr)
    else:
        print(f"no item matches {name!r}. Tracked items:", file=sys.stderr)
        for _, found in con.execute(
                "SELECT id, market_hash_name FROM items ORDER BY 2").fetchall():
            print(f"  {found}", file=sys.stderr)
    sys.exit(1)


def table(rows: list[sqlite3.Row], columns: list[str]) -> str:
    if not rows:
        return "  (пусто)\n"
    widths = [len(c) for c in columns]
    body = []
    for row in rows:
        cells = []
        for i, col in enumerate(columns):
            value = row[col]
            if value is None:
                text = ""
            elif isinstance(value, float):
                text = f"{value:.4f}".rstrip("0").rstrip(".")
            else:
                text = str(value)
            widths[i] = max(widths[i], len(text))
            cells.append(text)
        body.append(cells)
    out = ["  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)),
           "  " + "  ".join("-" * w for w in widths)]
    out.extend("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
               for cells in body)
    return "\n".join(out) + "\n"


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
    item_id, name = resolve_item(con, args.name)

    since = (datetime.now(timezone.utc)
             - timedelta(days=args.days)).replace(microsecond=0).isoformat()

    sales = con.execute(
        "SELECT sold_at, price, float_value, paint_seed, paint_index,"
        "       sold_at_estimated"
        "  FROM sales WHERE item_id = ? AND sold_at >= ?"
        " ORDER BY float_value", (item_id, since)).fetchall()

    # One snapshot per table: the book is replaced wholesale on every sweep,
    # and depth is kept newest-per-band, so both are read at their latest.
    book = con.execute(
        "SELECT price, qty, float_min, float_max, position, fetched_at"
        "  FROM buy_orders WHERE item_id = ?"
        " ORDER BY price DESC", (item_id,)).fetchall()

    depth = con.execute(
        "SELECT d.float_min, d.float_max, d.listings, d.cheapest,"
        "       d.median_age_days, d.oldest_days, d.fetched_at"
        "  FROM listing_depth d"
        "  JOIN (SELECT float_min, MAX(fetched_at) AS newest"
        "          FROM listing_depth WHERE item_id = ?"
        "         GROUP BY float_min) latest"
        "    ON d.float_min = latest.float_min AND d.fetched_at = latest.newest"
        " WHERE d.item_id = ? ORDER BY d.float_min", (item_id, item_id)).fetchall()

    moves = con.execute(
        "SELECT fetched_at, float_min, float_max, top_price, orders, qty"
        "  FROM book_history WHERE item_id = ?"
        " ORDER BY float_min, fetched_at", (item_id,)).fetchall()

    out_path = args.out or (name.replace("|", "-").replace("/", "-")
                            .replace(" ", "_") + ".txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n")
        fh.write(f"# выгружено {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        fh.write(f"# продажи за последние {args.days} дн.\n\n")

        fh.write(f"## ПРОДАЖИ ({len(sales)} шт., отсортированы по float)\n")
        fh.write(table(list(sales), ["sold_at", "price", "float_value",
                                     "paint_seed", "paint_index",
                                     "sold_at_estimated"]))

        fh.write(f"\n## СТАКАН ОРДЕРОВ ({len(book)} строк)\n")
        fh.write(table(list(book), ["price", "qty", "float_min", "float_max",
                                    "position", "fetched_at"]))

        fh.write(f"\n## ЛИСТИНГИ НА ПРОДАЖУ ({len(depth)} полос)\n")
        fh.write(table(list(depth), ["float_min", "float_max", "listings",
                                     "cheapest", "median_age_days",
                                     "oldest_days", "fetched_at"]))

        fh.write(f"\n## ИСТОРИЯ СТАКАНА ({len(moves)} строк)\n")
        fh.write(table(list(moves), ["fetched_at", "float_min", "float_max",
                                     "top_price", "orders", "qty"]))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"{name}")
    print(f"  продаж:            {len(sales)}")
    print(f"  строк стакана:     {len(book)}")
    print(f"  полос листингов:   {len(depth)}")
    print(f"  истории стакана:   {len(moves)}")
    print(f"\nзаписано в {out_path} ({size_kb:.0f} КБ)")
    if size_kb > 200:
        print("файл великоват для чата — уменьши окно, например --days 30")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
