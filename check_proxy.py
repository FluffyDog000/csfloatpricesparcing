#!/usr/bin/env python3
"""Check one proxy line: does it reach CSFloat, and how long does its IP live?

The TTL you write into a sticky-session login (…-ttl-30) is a request, not a
guarantee — a provider may clamp it to the plan's maximum, or ignore it and
hand out a new IP per request. This measures what actually happens.

Usage:
    python check_proxy.py "host:8888:user:pass"              # one probe
    python check_proxy.py "http://user:pass@host:8888"       # same, URL form
    python check_proxy.py "host:8888:user:pass" --watch 60   # 60 min of probes
    python check_proxy.py --direct                           # the server itself

Reads CSFLOAT_COOKIE from .env so the CSFloat probe is the real request the
collector makes. Prints nothing secret: logins and passwords stay out of the
output.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import requests

from src.config import load_config
from src.proxies import mask_proxy, split_proxy_flags

# Several, because any single echo service may be blocked or down.
IP_ECHOES = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.co/json",
    "https://api.myip.com",
)
PROBE_ITEM = "AWP | Printstream (Field-Tested)"


def exit_ip(proxies, timeout: float) -> str:
    """Which IP the world sees for this route."""
    last = None
    for url in IP_ECHOES:
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("ip") or data.get("query") or "?"
        except (requests.RequestException, ValueError) as exc:
            last = exc
    raise requests.RequestException(f"ни один из сервисов определения IP не ответил: {last}")


def probe_csfloat(config, proxies, timeout: float) -> None:
    """The real request the collector makes, so a proxy that Cloudflare
    challenges is caught here rather than in production."""
    from src.csfloat_client import CSFloatClient

    client = CSFloatClient(config.http, config.polling)
    url = client.sales_url(PROBE_ITEM)
    resp = requests.get(url, headers=client._base_headers(),
                        proxies=proxies, timeout=timeout)

    limits = {k: v for k, v in resp.headers.items()
              if k.lower().startswith(("x-ratelimit", "retry-after"))}
    print(f"  CSFloat      : HTTP {resp.status_code}")
    if limits:
        print("  лимиты       : " + " · ".join(f"{k}={v}" for k, v in limits.items()))

    body = (resp.text or "")[:200].lstrip()
    if resp.status_code == 200 and body.startswith(("[", "{")):
        try:
            print(f"  вердикт      : ✅ годится (продаж в ответе: {len(resp.json())})")
        except ValueError:
            print("  вердикт      : ⚠ HTTP 200, но тело не JSON")
    elif body.lower().startswith("<!doctype") or "cf-mitigated" in resp.headers:
        print("  вердикт      : ❌ Cloudflare challenge — бот такой не пройдёт")
    elif resp.status_code == 429:
        print(f"  вердикт      : ❌ 429 сразу — с этого IP уже кто-то долбил CSFloat"
              f"\n  ответ        : {body[:120]}")
    elif resp.status_code in (401, 403):
        print(f"  вердикт      : ❌ HTTP {resp.status_code} — IP забанен либо кука протухла")
    else:
        print(f"  вердикт      : ❌ неожиданный ответ: {body[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка прокси для CSFloat")
    ap.add_argument("proxy", nargs="?", help="строка прокси (любой из форматов дашборда)")
    ap.add_argument("--direct", action="store_true", help="проверить сам сервер, без прокси")
    ap.add_argument("--watch", type=float, default=0, metavar="МИН",
                    help="следить за выходным IP столько минут и засечь, когда он сменится")
    ap.add_argument("--every", type=float, default=2.0, metavar="МИН",
                    help="как часто опрашивать IP при --watch (по умолчанию 2 мин)")
    args = ap.parse_args()

    if not args.proxy and not args.direct:
        ap.error("укажи строку прокси или --direct")

    config = load_config()
    timeout = config.http.timeout_seconds

    if args.direct:
        proxies, label = None, "прямое соединение (IP сервера)"
    else:
        url, rotating = split_proxy_flags(args.proxy)
        proxies = {"http": url, "https": url}
        label = mask_proxy(url) + (" [помечен #rotating]" if rotating else "")

    print(f"Маршрут: {label}\n")
    try:
        first = exit_ip(proxies, timeout)
    except requests.RequestException as exc:
        print(f"  ❌ прокси не отвечает: {exc}")
        return 1
    print(f"  выходной IP  : {first}")

    try:
        probe_csfloat(config, proxies, timeout)
    except requests.RequestException as exc:
        print(f"  ❌ запрос к CSFloat не прошёл: {exc}")
        return 1

    if not args.watch:
        return 0

    print(f"\nСлежу за IP {args.watch:.0f} мин (опрос раз в {args.every:.0f} мин)."
          f" Ctrl+C — прервать.\n")
    started = time.monotonic()
    current, changes, last_change = first, 0, started
    deadline = started + args.watch * 60.0
    while time.monotonic() < deadline:
        time.sleep(min(args.every * 60.0, max(deadline - time.monotonic(), 0)) or 1)
        try:
            now_ip = exit_ip(proxies, timeout)
        except requests.RequestException as exc:
            print(f"  {datetime.now():%H:%M:%S}  ошибка опроса: {exc}")
            continue
        stamp = f"  {datetime.now():%H:%M:%S}"
        if now_ip == current:
            print(f"{stamp}  тот же IP ({(time.monotonic() - last_change) / 60:.0f} мин держится)")
        else:
            held = (time.monotonic() - last_change) / 60.0
            changes += 1
            print(f"{stamp}  ⟳ IP сменился после ~{held:.0f} мин")
            current, last_change = now_ip, time.monotonic()

    watched = (time.monotonic() - started) / 60.0
    print(f"\nИтог за {watched:.0f} мин: смен IP — {changes}.")
    if changes == 0:
        print("  IP держится дольше окна наблюдения — sticky работает,"
              " можно убрать пометку #rotating.")
    else:
        print(f"  В среднем IP живёт ~{watched / changes:.0f} мин."
              " Меньше часа — оставляй #rotating.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
