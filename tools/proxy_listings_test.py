"""Проверка прокси для листингов CSFloat: подходит ли адрес под то, что бот
читает через ключи анализа, и не занят ли он чужими запросами.

По каждому прокси из файла (по одному в строке, форматы как на «Нагрузке»:
http://user:pass@host:port, socks5://..., host:port:user:pass):

  1. адрес выхода и чья это сеть (ipinfo.io) — датацентр или провайдер;
  2. N запросов страницы листингов (до 50 лотов) с паузой — ответы, размер,
     время, 429;
  3. листинги одной полосы float (как при обходе стаканов);
  4. стакан ордеров одного лота — ждём «Disable your VPN» на датацентре;
  5. история продаж без ключа — сколько квоты адреса уже потрачено
     (x-ratelimit-remaining сразу низкий = адресом пользуется кто-то ещё).

Ключ берётся первый из keys.txt (или --key-file), печатается только его
отпечаток. Пароль прокси в выводе заменён на ***.

    .venv/bin/python tools/proxy_listings_test.py webshare.txt
    .venv/bin/python tools/proxy_listings_test.py webshare.txt --requests 30 --pause 2
"""
from __future__ import annotations

import argparse
import os
import re
import statistics as st
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from src.config import load_config  # noqa: E402
from src.keyring import fingerprint, read_keys  # noqa: E402
from src.proxies import normalize_proxy  # noqa: E402

BASE = "https://csfloat.com"
ITEM = "AK-47 | Redline (Field-Tested)"
LIMIT_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")


def masked(proxy: str) -> str:
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", proxy)


def limits(resp) -> str:
    vals = [f"{h.split('-')[-1]}={resp.headers.get(h)}" for h in LIMIT_HEADERS
            if resp.headers.get(h) is not None]
    return " ".join(vals) or "лимитов в ответе нет"


def wire(resp) -> int:
    declared = resp.headers.get("Content-Length")
    return int(declared) if declared and declared.isdigit() else len(resp.content)


def get(session, url, proxy, headers=None, timeout=20.0):
    started = time.monotonic()
    resp = session.get(url, headers=headers or {}, timeout=timeout,
                       proxies={"http": proxy, "https": proxy})
    return resp, time.monotonic() - started


def check(proxy: str, key: str, n: int, pause: float) -> None:
    print(f"\n=== {masked(proxy)} ===")
    s = requests.Session()
    auth = {"Authorization": key}

    try:
        r, _ = get(s, "https://ipinfo.io/json", proxy)
        info = r.json()
        print(f"  выход: {info.get('ip')} · {info.get('org', '?')} · "
              f"{info.get('city', '')}, {info.get('country', '')}")
    except Exception as exc:  # noqa: BLE001 - a dead proxy is the answer
        print(f"  не соединился: {exc}")
        return

    name = quote(ITEM, safe="")
    page = f"{BASE}/api/v1/listings?market_hash_name={name}&limit=50"
    codes, sizes, times, lot_id = {}, [], [], None
    for i in range(n):
        try:
            r, dt = get(s, page, proxy, auth)
        except Exception as exc:  # noqa: BLE001
            codes["сеть"] = codes.get("сеть", 0) + 1
            print(f"  листинги #{i + 1}: ошибка сети: {exc}")
            time.sleep(pause)
            continue
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
        if r.status_code == 200:
            sizes.append(wire(r))
            times.append(dt)
            if lot_id is None:
                try:
                    data = r.json()
                    rows = data.get("data") if isinstance(data, dict) else data
                    lot_id = rows[0]["id"] if rows else None
                except Exception:  # noqa: BLE001
                    pass
        elif r.status_code in (403, 429):
            print(f"  листинги #{i + 1}: HTTP {r.status_code} · {limits(r)} · "
                  f"{r.text[:120]!r}")
        time.sleep(pause)
    ok = codes.get(200, 0)
    print(f"  листинги (страница до 50 лотов) ×{n}: ответы {codes}")
    if ok:
        print(f"    размер ~{st.mean(sizes) / 1024:.1f} КБ, время ~{st.mean(times):.2f} с "
              f"(макс {max(times):.2f})")

    band = (f"{BASE}/api/v1/listings?market_hash_name={name}"
            f"&min_float=0.15&max_float=0.17&type=buy_now&sort_by=lowest_price&limit=50")
    try:
        r, _ = get(s, band, proxy, auth)
        print(f"  листинги полосы 0.15–0.17: HTTP {r.status_code} · {wire(r) / 1024:.1f} КБ · "
              f"{limits(r)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  листинги полосы: ошибка сети: {exc}")

    if lot_id:
        try:
            r, _ = get(s, f"{BASE}/api/v1/listings/{lot_id}/buy-orders?limit=10",
                       proxy, auth)
            vpn = "VPN" in r.text
            print(f"  стакан ордеров: HTTP {r.status_code}"
                  + (" — CSFloat не отдаёт стакан с этого адреса (датацентр/VPN)"
                     if vpn else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"  стакан ордеров: ошибка сети: {exc}")

    hist = f"{BASE}/api/v1/history/{name}/sales"
    try:
        r, _ = get(s, hist, proxy)
        print(f"  история продаж (без ключа): HTTP {r.status_code} · {limits(r)}")
        remaining = r.headers.get("x-ratelimit-remaining")
        cap = r.headers.get("x-ratelimit-limit")
        if remaining and cap and remaining.isdigit() and cap.isdigit() \
                and int(remaining) < int(cap) * 0.8:
            print("    ⚠ квота адреса уже заметно потрачена — им, похоже, пользуется "
                  "кто-то ещё")
    except Exception as exc:  # noqa: BLE001
        print(f"  история продаж: ошибка сети: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("proxies", help="файл с прокси, по одному в строке")
    ap.add_argument("--key-file", default=None, help="по умолчанию keys.txt из настроек")
    ap.add_argument("--requests", type=int, default=10, help="запросов листингов на прокси")
    ap.add_argument("--pause", type=float, default=3.0, help="пауза между ними, с")
    args = ap.parse_args()

    cfg = load_config()
    keys = read_keys(args.key_file or cfg.http.keys_file) or \
        ([cfg.http.api_key] if cfg.http.api_key else [])
    if not keys:
        sys.exit("Нет ключа: ни keys.txt, ни CSFLOAT_API_KEY")
    key = keys[0]
    print(f"Ключ {fingerprint(key)} · {args.requests} запросов листингов на прокси, "
          f"пауза {args.pause} с")
    with open(args.proxies, encoding="utf-8") as fh:
        proxies = [normalize_proxy(line.strip()) for line in fh
                   if line.strip() and not line.startswith("#")]
    for proxy in proxies:
        check(proxy, key, args.requests, args.pause)


if __name__ == "__main__":
    main()
