"""One request to CSFloat, and exactly what came back.

Three explanations have been in play for the same refusal and they call for
different actions: the key in `.env` is not the one being sent, the account
behind that key is flagged, or the address is. Each sweep tries all of it at
once through the pool and reports a single line, which cannot separate them.

This sends ONE request, directly, with the key the config actually loaded, and
prints the status, the quota headers and the body. Never the key itself - only
a fingerprint, so two runs can be compared without the key passing through a
terminal history or a chat window.

    .venv/bin/python tools/key_probe.py
    .venv/bin/python tools/key_probe.py --proxy http://user:pass@host:port
    .venv/bin/python tools/key_probe.py --pool     # every route in the pool
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE = "/api/v1/listings?limit=1&type=buy_now"


def fingerprint(key: str) -> str:
    """Name a key in the output without disclosing it."""
    return hashlib.sha256(key.encode()).hexdigest()[:8] if key else "НЕ ЗАДАН"


def report(label: str, resp) -> None:
    quota = {k: v for k, v in resp.headers.items()
             if k.lower().startswith(("x-ratelimit", "retry-after", "cf-ray"))}
    print(f"\n{label}")
    print(f"  HTTP {resp.status_code}")
    if quota:
        for k, v in sorted(quota.items()):
            print(f"  {k}: {v}")
    else:
        print("  заголовков квоты нет")
    body = (resp.text or "").strip().replace("\n", " ")
    print(f"  ответ: {body[:200] or '(пусто)'}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", help="send through this proxy instead of direct")
    ap.add_argument("--pool", action="store_true",
                    help="try every route the bot has, one request each")
    args = ap.parse_args()

    try:
        import requests

        from src.config import load_config
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    key = config.http.api_key or ""
    url = config.http.base_url + PROBE
    print(f"ключ из .env: {fingerprint(key)}"
          + ("" if key else "  ← CSFLOAT_API_KEY пуст, запрос уйдёт без ключа"))
    print(f"адрес: {url}")

    headers = {"Authorization": key} if key else {}
    headers["Accept"] = "application/json"
    session = requests.Session()

    routes: list[tuple[str, str | None]] = []
    if args.pool:
        # The bot's own routes, named by key rather than by address: a proxy
        # string holds a password.
        from src.proxies import ProxyPool

        pool = ProxyPool(list(config.http.proxies),
                         use_direct=config.http.use_direct)
        for state in pool.routes.values():
            proxies = state.proxies()
            routes.append((state.key, (proxies or {}).get("https")))
    else:
        routes.append(("прокси" if args.proxy else "прямой", args.proxy))

    for label, proxy in routes:
        try:
            resp = session.get(
                url, headers=headers, timeout=config.http.timeout_seconds,
                proxies={"http": proxy, "https": proxy} if proxy else None)
        except Exception as exc:  # noqa: BLE001 - one route is not the probe
            print(f"\n{label}\n  не дошло: {type(exc).__name__}: {exc}")
            continue
        report(label, resp)

    print("\nчто это значит:")
    print("  HTTP 200                      — ключ и адрес в порядке")
    print("  429 «too many IPs»            — помечен аккаунт этого ключа")
    print("  429 с x-ratelimit-remaining 0 — обычная квота, надо ждать сброса")
    print("  403 «Disable your VPN»        — отказан адрес, не ключ")
    print("  401/403 про ключ              — ключ не принят: отозван или чужой")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
