#!/usr/bin/env python3
"""Посмотреть Buy Orders одного лота и понять, что отдаёт эндпоинт.

CSFloat отдаёт ордера по id ЛОТА, а не по названию предмета:
    GET /api/v1/listings/{listing_id}/buy-orders?limit=N

id берётся прямо из адресной строки страницы предмета:
    https://csfloat.com/item/1014141630426513781
                             ^^^^^^^^^^^^^^^^^^^

Usage:
    python check_orders.py 1014141630426513781
    python check_orders.py 1014141630426513781 --limit 100   # проверить, есть ли потолок
    python check_orders.py 1014141630426513781 --raw         # весь JSON как есть
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import requests

from src.config import load_config
from src.csfloat_client import CSFloatClient

ORDERS_PATH = "/api/v1/listings/{listing_id}/buy-orders"


def records(payload: Any) -> list[dict]:
    """Ордера могут прийти списком или завёрнутыми в объект."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "orders", "buy_orders", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def first(record: dict, *paths: str):
    """Значение по первому подходящему пути вида 'a.b.c'."""
    for path in paths:
        node: Any = record
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None:
            return node
    return None


def money(cents: Any) -> str:
    if isinstance(cents, (int, float)):
        return f"${cents / 100:.2f}" if cents > 1000 else f"${cents:.2f}"
    return "—"


def describe(payload: Any, limit_asked: int) -> None:
    rows = records(payload)
    print(f"\nОрдеров в ответе: {len(rows)} (просили limit={limit_asked})")
    if isinstance(payload, dict):
        print(f"Ключи верхнего уровня: {', '.join(list(payload)[:10])}")
        for key in ("total", "count", "total_count", "cursor", "next", "has_more"):
            if key in payload:
                print(f"  {key}: {payload[key]}")
    if not rows:
        print("Список ордеров не распознан — покажи вывод с --raw.")
        return

    print(f"Поля одного ордера: {', '.join(list(rows[0])[:14])}")
    print(f"\n{'цена':>10} {'кол-во':>7}  фильтры")
    print("-" * 62)
    scoped = 0
    for r in rows:
        price = first(r, "price", "market_price", "value")
        qty = first(r, "qty", "quantity", "count", "amount")
        lo = first(r, "expression.float_value.min", "min_float", "float_min",
                   "expression.min_float")
        hi = first(r, "expression.float_value.max", "max_float", "float_max",
                   "expression.max_float")
        seed = first(r, "expression.paint_seed", "paint_seed", "seed")
        bits = []
        if lo is not None or hi is not None:
            bits.append(f"float {lo if lo is not None else '—'}–{hi if hi is not None else '—'}")
            scoped += 1
        if seed is not None:
            bits.append(f"seed {seed}")
        expr = first(r, "expression")
        if not bits and isinstance(expr, (dict, list)) and expr:
            bits.append(f"expression: {json.dumps(expr, ensure_ascii=False)[:60]}")
        print(f"{money(price):>10} {str(qty or '—'):>7}  {'; '.join(bits) or 'без фильтров'}")

    print(f"\nТочечных (с фильтром по флоту/паттерну): {scoped} из {len(rows)}")
    if len(rows) >= limit_asked:
        print("Ответ упёрся в limit — есть что запросить дальше, попробуй больший --limit.")
    else:
        print("Ответ короче limit — похоже, это все ордера этого лота.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Buy Orders одного лота CSFloat")
    ap.add_argument("listing_id", help="id лота из адреса csfloat.com/item/<id>")
    ap.add_argument("--limit", type=int, default=10,
                    help="сколько запросить (по умолчанию 10, как в интерфейсе)")
    ap.add_argument("--raw", action="store_true", help="показать весь JSON")
    args = ap.parse_args()

    config = load_config()
    client = CSFloatClient(config.http, config.polling)
    if not client.has_credentials():
        print("В .env нет CSFLOAT_COOKIE — запрос будет отклонён.")
        return 1

    url = (config.http.base_url + ORDERS_PATH.format(listing_id=args.listing_id)
           + f"?limit={args.limit}")
    print(f"GET {url}")
    try:
        payload = client.fetch_json(url)
    except requests.HTTPError as exc:
        print(f"❌ {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {type(exc).__name__}: {exc}")
        return 1

    if args.raw:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:6000])
    describe(payload, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
