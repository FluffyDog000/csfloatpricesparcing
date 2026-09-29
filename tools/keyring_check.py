"""Why the key ring did or did not come up, without guessing.

Four things have to line up before sweeps run in parallel: the variable is
set, the file is readable, it holds at least two keys, and the proxy pool has
addresses to bind them to. A missing line in the log says one of the four
failed and not which, and an evening was spent today on exactly that kind of
silence.

Prints the state of each. Keys appear only as the eight-character digest the
rest of the bot logs.

    .venv/bin/python tools/keyring_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from src.config import load_config
        from src.db import Database
        from src.keyring import (DEFAULT_ROUTES_PER_KEY, KeyRing, fingerprint,
                                 read_keys)
        from src.parallel import MAX_WORKERS
        from src.proxies import ProxyPool, parse_proxy_list
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    path = config.http.keys_file

    print("1. CSFLOAT_KEYS_FILE")
    if not path:
        print("   не задан в .env — кольцо не собирается, работает один ключ")
        print("   пропиши:  CSFLOAT_KEYS_FILE=/root/csfloatpricesparcing/keys.txt")
        return 1
    print(f"   {path}")

    print("\n2. файл")
    if not os.path.exists(path):
        print("   НЕ СУЩЕСТВУЕТ по этому пути")
        return 1
    mode = oct(os.stat(path).st_mode & 0o777)
    print(f"   есть, права {mode}"
          + ("" if mode == "0o600" else "  ← стоит chmod 600, это доступы"))

    print("\n3. ключи")
    keys = read_keys(path)
    if not keys:
        print("   ни одного — пустой файл, или все строки закомментированы")
        return 1
    for key in keys:
        print(f"   {fingerprint(key)}")
    if len(keys) < 2:
        print("   один ключ — кольцо не нужно и не создаётся, это обычный путь")
        return 0

    print("\n4. адреса")
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
    print(f"   маршрутов в пуле: {len(pool.routes)}"
          + (" (включая свой IP)" if direct else ""))
    if not pool.routes:
        print("   пул пуст — привязывать ключи не к чему")
        return 1

    ring = KeyRing(list(keys), pool)
    print("\nИТОГ")
    print(f"   ключей {len(ring.live())}, по {ring.routes_per_key} адреса на "
          f"ключ, обходов разом {min(ring.concurrency(), MAX_WORKERS)}")
    if len(pool.routes) < len(keys) * DEFAULT_ROUTES_PER_KEY:
        share = len(keys) * DEFAULT_ROUTES_PER_KEY / len(pool.routes)
        print(f"   на один адрес приходится ~{share:.1f} ключ(а): адресов "
              "меньше, чем советует поддержка (2-4 на ключ). Ключи добавляют "
              "квоту и скорость, но число адресов у аккаунта не меняют")
    print("\nЕсли всё выше в порядке, а в журнале нет строки «Key ring» — "
          "сборщик не перезапускался после правки .env:")
    print("   systemctl restart csfloat-collector")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
