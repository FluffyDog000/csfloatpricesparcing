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


def decode(body: bytes, encoding: str) -> bytes:
    """Распаковать тело тем же кодеком, которым его упаковал сервер."""
    enc = (encoding or "").lower()
    if enc == "gzip":
        import gzip
        return gzip.decompress(body)
    if enc == "deflate":
        import zlib
        try:
            return zlib.decompress(body)
        except zlib.error:                      # без zlib-обёртки, голый deflate
            return zlib.decompress(body, -zlib.MAX_WBITS)
    if enc == "br":
        import brotli
        return brotli.decompress(body)
    if enc == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompress(body)
    return body


def measure(session, url, timeout, proxy, offer=None):
    """Один запрос: что пришло по проводу и что получилось после распаковки.

    Байты считаются здесь, а не берутся у urllib3: его счётчик сжатого потока
    на сервере вернул ноль, замер молча свалился в длину разжатого тела, и обе
    графы показали одно число. Поток читается сырым (`decode_content=False`),
    распаковывается тем же кодеком, что назвал сервер, — и тогда обе величины
    измерены, а не выведены одна из другой.
    """
    resp = session.get(url, timeout=timeout, stream=True,
                       headers={"Accept-Encoding": offer} if offer else None,
                       proxies={"http": proxy, "https": proxy} if proxy else None)
    try:
        packed = resp.raw.read(decode_content=False)
    finally:
        resp.close()

    encoding = resp.headers.get("Content-Encoding", "")
    try:
        plain = decode(packed, encoding)
        failed = ""
    except Exception as exc:  # noqa: BLE001 - сказать, чем именно не распаковалось
        plain, failed = packed, f"{type(exc).__name__}: {exc}"

    try:
        body = json.loads(plain)
    except (ValueError, UnicodeDecodeError):
        body = None
    records = len(body) if isinstance(body, list) else (
        len(body.get("data", [])) if isinstance(body, dict) else 0)
    return {
        "status": resp.status_code,
        "encoding": encoding or "нет",
        "wire": len(packed),
        "decoded": len(plain),
        "declared": resp.headers.get("Content-Length"),
        "chunked": resp.headers.get("Transfer-Encoding", ""),
        "failed": failed,
        "records": records,
        "body": body,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="market_hash_name, в кавычках")
    ap.add_argument("--params", help="какие имена проверить, через запятую; "
                                     "пустая строка — только замер размера, "
                                     "один запрос")
    ap.add_argument("--codecs", action="store_true",
                    help="сравнить упаковщики на этом же ответе: три запроса")
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
    print(f"  сжатие: {full['encoding']}"
          + (f", Content-Length {full['declared']}" if full["declared"] else
             ", без Content-Length")
          + (f", {full['chunked']}" if full["chunked"] else ""))
    if full["failed"]:
        print(f"  НЕ РАСПАКОВАЛОСЬ: {full['failed']}")
        print("  сервер назвал кодек, которым тело не упаковано — обычно так\n"
              "  делает прокси, распаковавший ответ и забывший снять заголовок")
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

    # Пустая строка -- это «ничего не проверять», а не «не задано»: замер
    # размера стоит один запрос, перебор имён -- по одному на имя.
    if args.codecs:
        # Тот же ответ, упакованный по-разному. Сервер выбирает из того, что
        # мы объявили, поэтому объявляем по одному кодеку за раз.
        print("\nЧЕМ УПАКОВАНО   (один и тот же ответ, три запроса)")
        base_wire = None
        for offer, label in (("br", "brotli"), ("gzip", "gzip"),
                             ("identity", "без сжатия")):
            try:
                got = measure(session, base, config.http.timeout_seconds, proxy,
                              offer=offer)
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<12} не дошло: {type(exc).__name__}")
                continue
            got_enc = got["encoding"]
            if offer != "identity" and got_enc != offer:
                print(f"  {label:<12} сервер не дал ({got_enc}) — "
                      "предлагать нечего")
                continue
            if base_wire is None:
                base_wire = got["wire"]
            delta = ((got["wire"] / base_wire - 1) * 100) if base_wire else 0
            print(f"  {label:<12} {got['wire'] / 1024:6.1f} КБ"
                  + (f"   +{delta:.0f}% к brotli" if delta > 0.5 else
                     "   ← лучший" if base_wire == got["wire"] else ""))

    names = ([p.strip() for p in args.params.split(",") if p.strip()]
             if args.params is not None else list(CANDIDATES))
    if not names:
        return 0
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
