"""Сколько байт на самом деле стоит один опрос, и можно ли просить меньше.

Дашборд показывает размер РАЗЖАТОГО тела, а прокси выставляет счёт за то, что
прошло по проводу. Разница на этом эндпоинте — больше чем в десять раз, и без
замера её видно не будет: 61 КБ в отчёте против примерно четырёх на канале.

Второе, что здесь проверяется: мы забираем сорок последних продаж, а между
опросами их набегает одна-две. Если эндпоинт понимает ограничение на число
записей, хвост списка (а это почти весь список) станет вчетверо дешевле. Ни в
документации, ни в ответах этого не написано — только спросить.

Запросов тратит по числу проверяемых параметров плюс один. Идёт через пул
прокси, как и сам бот: с адреса сервера CSFloat не отвечает.

    .venv/bin/python tools/payload_probe.py "AK-47 | Redline (Field-Tested)"
    .venv/bin/python tools/payload_probe.py "..." --params limit,count
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Имена, под которыми подобное ограничение встречается у CSFloat в соседних
# эндпоинтах и вообще в REST. Проверяются по одному запросу на каждое.
CANDIDATES = ("limit", "count", "per_page", "page_size", "take")
WANT = 5


def measure(session, url, timeout, proxy):
    """Один запрос: что пришло по проводу и что получилось после распаковки."""
    from src.csfloat_client import wire_bytes

    resp = session.get(url, timeout=timeout,
                       proxies={"http": proxy, "https": proxy} if proxy else None)
    decoded = len(resp.content)
    wire = wire_bytes(resp)
    try:
        body = resp.json()
    except ValueError:
        body = None
    records = len(body) if isinstance(body, list) else (
        len(body.get("data", [])) if isinstance(body, dict) else 0)
    return {
        "status": resp.status_code,
        "encoding": resp.headers.get("Content-Encoding", "нет"),
        "wire": wire,
        "decoded": decoded,
        "records": records,
        "body": body,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="market_hash_name, в кавычках")
    ap.add_argument("--params", help="какие имена проверить, через запятую")
    ap.add_argument("--want", type=int, default=WANT,
                    help=f"сколько записей просить (по умолчанию {WANT})")
    args = ap.parse_args()

    try:
        import requests

        from src.config import load_config
        from src.db import Database
        from src.proxies import ProxyPool, parse_proxy_list
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    db = Database(config.db_path)
    try:
        raw = db.get_setting("proxies")
        urls = (parse_proxy_list(raw) if raw is not None
                else list(config.http.proxies))
        direct = ((db.get_setting("use_direct", "1") or "1") != "0"
                  if raw is not None else config.http.use_direct)
    finally:
        db.close()

    pool = ProxyPool(urls, use_direct=direct)
    route = next(iter(pool.routes.values()), None)
    if route is None:
        print("в пуле нет ни одного маршрута", file=sys.stderr)
        return 1
    proxy = (route.proxies() or {}).get("https")
    print(f"маршрут: {route.key}\n")

    base = config.http.base_url + config.http.sales_path_template.format(
        name=quote(args.name, safe=""))
    session = requests.Session()
    # Ровно те заголовки, с которыми ходит сборщик: без куки и без ключа.
    session.headers.update({
        "User-Agent": config.http.user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": config.http.base_url + "/",
        "Origin": config.http.base_url,
    })
    print(f"объявляем Accept-Encoding: {session.headers.get('Accept-Encoding')}")

    try:
        full = measure(session, base, config.http.timeout_seconds, proxy)
    except Exception as exc:  # noqa: BLE001 - сообщить, а не упасть трассировкой
        print(f"запрос не дошёл: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\nКАК СЕЙЧАС   HTTP {full['status']}, записей {full['records']}")
    print(f"  сжатие: {full['encoding']}")
    print(f"  по проводу:  {full['wire'] / 1024:7.1f} КБ   ← за это платит прокси")
    print(f"  разжатое:    {full['decoded'] / 1024:7.1f} КБ   ← это показывает дашборд")
    if full["wire"] and full["decoded"] > full["wire"]:
        print(f"  сжатие в {full['decoded'] / full['wire']:.1f} раза — "
              "цифра в дашборде завышена во столько же раз")
    elif full["encoding"] == "нет":
        print("  СЖАТИЯ НЕТ — сервер отдаёт как есть. Вот это и надо чинить "
              "в первую очередь")
    if full["records"]:
        print(f"  на одну продажу: {full['wire'] / full['records']:.0f} Б по проводу")

    names = ([p.strip() for p in args.params.split(",") if p.strip()]
             if args.params else list(CANDIDATES))
    print(f"\nМОЖНО ЛИ ПРОСИТЬ МЕНЬШЕ (хотим {args.want} записей вместо "
          f"{full['records']})")
    works = []
    for param in names:
        url = f"{base}?{param}={args.want}"
        try:
            got = measure(session, url, config.http.timeout_seconds, proxy)
        except Exception as exc:  # noqa: BLE001
            print(f"  {param:<10} не дошло: {type(exc).__name__}")
            continue
        if got["status"] != 200:
            print(f"  {param:<10} HTTP {got['status']} — отвергнут")
            continue
        if got["records"] and got["records"] < full["records"]:
            saved = (1 - got["wire"] / full["wire"]) * 100 if full["wire"] else 0
            print(f"  {param:<10} РАБОТАЕТ: записей {got['records']}, "
                  f"{got['wire'] / 1024:.1f} КБ — на {saved:.0f}% меньше")
            works.append((param, saved))
        else:
            print(f"  {param:<10} принят, но записей столько же "
                  f"({got['records']}) — параметр игнорируется")

    print()
    if works:
        best = max(works, key=lambda w: w[1])
        print(f"ИТОГ: эндпоинт понимает «{best[0]}». Хвост списка (предметы, "
              f"которые продаются редко)\n  станет дешевле примерно на "
              f"{best[1]:.0f}%. Ликвидным это не поможет: у них окно и так\n"
              "  выбирается целиком, и урезать его — значит опрашивать чаще.")
    else:
        print("ИТОГ: сократить выдачу нечем — эндпоинт отдаёт своё окно целиком.\n"
              "  Остаётся сжатие, а оно уже включено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
