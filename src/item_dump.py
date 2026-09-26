"""One item's three tables, side by side, as text.

A pricing decision is argued from what the item has sold for, who is bidding
and what is being asked, all at once, so they are rendered together.

Shared by the CLI (tools/dump_item.py) and the bot's /dump command. One
implementation on purpose: two would drift, and then an example worked from
the phone would disagree with the same example worked on the server.

Only item-scoped tables are read. The settings table holds credentials -
collector.py writes the whole proxy list there, passwords and all - so it is
never touched and the output is safe to send.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


class ItemNotFound(Exception):
    """Carries the candidates, so a caller can show what to pick instead."""

    def __init__(self, name: str, candidates: list[str]):
        super().__init__(f"no item matches {name!r}")
        self.name = name
        self.candidates = candidates


def resolve(conn: sqlite3.Connection, name: str) -> tuple[int, str]:
    """The item's id and exact name, tolerating a loosely typed one.

    A name pasted from the site or typed on a phone differs by a space or the
    star more often than not, so an exact miss falls back to a substring
    match before giving up.
    """
    wanted = (name or "").strip()
    row = conn.execute(
        "SELECT id, market_hash_name FROM items WHERE market_hash_name = ?",
        (wanted,)).fetchone()
    if row:
        return row[0], row[1]

    like = conn.execute(
        "SELECT id, market_hash_name FROM items WHERE market_hash_name LIKE ?"
        " ORDER BY LENGTH(market_hash_name)", (f"%{wanted}%",)).fetchall()
    if len(like) == 1:
        return like[0][0], like[0][1]
    if like:
        raise ItemNotFound(wanted, [r[1] for r in like])
    everything = conn.execute(
        "SELECT market_hash_name FROM items ORDER BY 1").fetchall()
    raise ItemNotFound(wanted, [r[0] for r in everything])


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


def counts(conn: sqlite3.Connection, item_id: int, days: int) -> dict[str, int]:
    """Row counts per section, for a one-line summary before the file itself."""
    since = _since(days)
    one = lambda sql, args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    return {
        "sales": one("SELECT COUNT(*) FROM sales WHERE item_id = ? AND sold_at >= ?",
                     (item_id, since)),
        "book": one("SELECT COUNT(*) FROM buy_orders WHERE item_id = ?", (item_id,)),
        "depth": one("SELECT COUNT(DISTINCT float_min) FROM listing_depth"
                     " WHERE item_id = ?", (item_id,)),
        "moves": one("SELECT COUNT(*) FROM book_history WHERE item_id = ?", (item_id,)),
    }


def _since(days: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).replace(microsecond=0).isoformat()


def render(conn: sqlite3.Connection, item_id: int, name: str,
           days: int = 60) -> str:
    since = _since(days)

    sales = conn.execute(
        "SELECT sold_at, price, float_value, paint_seed, paint_index,"
        "       sold_at_estimated"
        "  FROM sales WHERE item_id = ? AND sold_at >= ?"
        " ORDER BY float_value", (item_id, since)).fetchall()

    # The book is replaced wholesale on every sweep, so its newest state is
    # simply whatever is in the table.
    book = conn.execute(
        "SELECT price, qty, float_min, float_max, position, fetched_at"
        "  FROM buy_orders WHERE item_id = ? ORDER BY price DESC",
        (item_id,)).fetchall()

    # Depth is kept newest-per-band rather than newest-sweep, so each band is
    # read at its own latest timestamp.
    depth = conn.execute(
        "SELECT d.float_min, d.float_max, d.listings, d.cheapest,"
        "       d.median_age_days, d.oldest_days, d.fetched_at"
        "  FROM listing_depth d"
        "  JOIN (SELECT float_min, MAX(fetched_at) AS newest"
        "          FROM listing_depth WHERE item_id = ?"
        "         GROUP BY float_min) latest"
        "    ON d.float_min = latest.float_min AND d.fetched_at = latest.newest"
        " WHERE d.item_id = ? ORDER BY d.float_min",
        (item_id, item_id)).fetchall()

    moves = conn.execute(
        "SELECT fetched_at, float_min, float_max, top_price, orders, qty"
        "  FROM book_history WHERE item_id = ?"
        " ORDER BY float_min, fetched_at", (item_id,)).fetchall()

    parts = [
        f"# {name}",
        f"# выгружено {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# продажи за последние {days} дн.",
        "",
        f"## ПРОДАЖИ ({len(sales)} шт., отсортированы по float)",
        table(list(sales), ["sold_at", "price", "float_value", "paint_seed",
                            "paint_index", "sold_at_estimated"]),
        f"\n## СТАКАН ОРДЕРОВ ({len(book)} строк)",
        table(list(book), ["price", "qty", "float_min", "float_max",
                           "position", "fetched_at"]),
        f"\n## ЛИСТИНГИ НА ПРОДАЖУ ({len(depth)} полос)",
        table(list(depth), ["float_min", "float_max", "listings", "cheapest",
                            "median_age_days", "oldest_days", "fetched_at"]),
        f"\n## ИСТОРИЯ СТАКАНА ({len(moves)} строк)",
        table(list(moves), ["fetched_at", "float_min", "float_max",
                            "top_price", "orders", "qty"]),
    ]
    return "\n".join(parts)


def safe_filename(name: str, days: int) -> str:
    cleaned = (name.replace("|", "-").replace("/", "-").replace("\\", "-")
               .replace(":", "-").replace(" ", "_").replace("★", "star"))
    return f"{cleaned}-{days}d.txt"
