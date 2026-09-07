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

from src.orders import (ORDERS_PATH, first, price_to_dollars, records,
                        PRICE_PATHS, QTY_PATHS, FLOAT_MIN_PATHS,
                        FLOAT_MAX_PATHS, SEED_PATHS)


def money(value: Any) -> str:
    dollars = price_to_dollars(value)
    return f"${dollars:.2f}" if dollars is not None else "—"


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
        price = first(r, *PRICE_PATHS)
        qty = first(r, *QTY_PATHS)
        lo = first(r, *FLOAT_MIN_PATHS)
        hi = first(r, *FLOAT_MAX_PATHS)
        seed = first(r, *SEED_PATHS)
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


def show_failure(resp) -> None:
    """Print who actually refused.

    A bare status cannot tell CSFloat rejecting a credential from Cloudflare
    rejecting the exit IP, and those need opposite fixes — the body and a
    couple of headers settle it."""
    if resp is None:
        print("   (ответ недоступен — запрос не дошёл до сервера)")
        return
    ctype = resp.headers.get("Content-Type", "—")
    print(f"   статус       : {resp.status_code}")
    print(f"   content-type : {ctype}")
    for key in ("cf-ray", "cf-mitigated", "server", "x-ratelimit-remaining"):
        if key in resp.headers:
            print(f"   {key:<13}: {resp.headers[key]}")
    try:
        body = (resp.text or "").strip()[:300]
    except Exception:  # noqa: BLE001
        body = ""
    print(f"   тело         : {body or '(пусто)'}")

    lowered = body.lower()
    if "disable your vpn" in lowered or '"code": 170' in lowered:
        print("\n   ВЕРДИКТ: CSFloat не отдаёт ордера с IP датацентра или VPN.")
        print("   Кука и лимиты ни при чём — нужен резидентский прокси.")
        print("   С IP сервера этот эндпоинт недоступен в принципе.")
        return

    html = body.lstrip().lower().startswith(("<!doctype", "<html"))
    if html or "cf-mitigated" in resp.headers:
        print("\n   ВЕРДИКТ: отказывает Cloudflare — выходной IP не проходит.")
        print("   Лечится сменой прокси/IP, кука тут ни при чём.")
    elif resp.status_code in (401, 403):
        print("\n   ВЕРДИКТ: отказывает сам CSFloat — дело в учётных данных.")
        print("   Проверь CSFLOAT_COOKIE в .env (нужна свежая сессия).")


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
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {type(exc).__name__}: {exc}")
        show_failure(getattr(exc, "response", None))
        return 1

    if args.raw:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:6000])
    describe(payload, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
