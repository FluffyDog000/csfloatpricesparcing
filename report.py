#!/usr/bin/env python3
"""Entrypoint: generate sales statistics for a tracked item.

Examples:
    # Float-bucket + seed summary for the last 7 days, printed as tables
    python report.py --item "★ Specialist Gloves | Lt. Commander (Field-Tested)"

    # Custom period and bucket size
    python report.py --item "AK-47 | Redline (Field-Tested)" --period 30d --bucket 0.02

    # Only sales with a specific paint seed
    python report.py --item "..." --seed 13

    # Export the exact per-sale rows to CSV
    python report.py --item "..." --period 30d --csv out.csv

    # List everything currently tracked
    python report.py --list
"""
from __future__ import annotations

import argparse
import sys

from tabulate import tabulate

from src.config import load_config
from src.db import Database
from src.report import build_report, exact_rows, rows_to_csv


def _print_table(title: str, rows: list[dict], empty: str = "(no data)") -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print(empty)
        return
    print(tabulate(rows, headers="keys", tablefmt="github", floatfmt=".2f"))


def main() -> int:
    parser = argparse.ArgumentParser(description="CSFloat sales report")
    parser.add_argument("--item", help="market_hash_name to report on")
    parser.add_argument("--period", help="Time window: e.g. 24h, 7d, 2w, all")
    parser.add_argument("--bucket", type=float, help="Float bucket size (e.g. 0.01)")
    parser.add_argument("--seed", type=int, help="Filter to a single paint seed")
    parser.add_argument("--csv", metavar="PATH", help="Write exact per-sale rows to CSV")
    parser.add_argument("--list", action="store_true", help="List tracked items and exit")
    args = parser.parse_args()

    config = load_config()
    db = Database(config.db_path)

    if args.list:
        names = db.list_item_names()
        if not names:
            print("No items tracked yet.")
        else:
            print("Tracked items:")
            for n in names:
                print(f"  - {n}")
        db.close()
        return 0

    if not args.item:
        parser.error("--item is required (or use --list)")

    period = args.period or config.reporting.default_period
    bucket = args.bucket or config.reporting.float_bucket_size

    try:
        rep = build_report(db, args.item, period, bucket, paint_seed=args.seed)
    except KeyError:
        print(f"Item not tracked: {args.item}", file=sys.stderr)
        print("Use --list to see tracked items.", file=sys.stderr)
        db.close()
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        db.close()
        return 1

    # Header
    print(f"\nItem:   {rep['item']}")
    print(f"Period: {rep['period']} (since {rep['since'] or 'beginning'})")
    if rep["paint_seed_filter"] is not None:
        print(f"Seed filter: {rep['paint_seed_filter']}")
    print(f"Total sales: {rep['total_sales']}")
    ov = rep["overall"]
    if rep["total_sales"]:
        print(
            f"Overall price  avg={ov['avg_price']}  median={ov['median_price']}  "
            f"min={ov['min_price']}  max={ov['max_price']}"
        )

    _print_table("Float buckets", rep["buckets"])
    _print_table("Paint seeds", rep["seeds"])

    if args.csv:
        rows = exact_rows(rep["rows"])
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write(rows_to_csv(rows))
        print(f"\nWrote {len(rows)} rows to {args.csv}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
