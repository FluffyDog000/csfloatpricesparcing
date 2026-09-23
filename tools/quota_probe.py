"""Ask CSFloat whether the request quota is counted per API key or per IP.

Everything else about using 100 keys depends on this one fact, and we cannot
settle it by reading headers from a single key: seeing an independent budget on
each proxy proved the IP matters, not that the key does not.

The experiment separates the two. From ONE address we spend several requests on
key A, then make the first request on key B and read what CSFloat says is left:

    per key  -> B starts near the full limit, untouched by A's spending
    per IP   -> B continues from where A left off, short by A's requests

Keys are read from a file (one per line) so they never pass through a shell
history or a chat window, and only a short fingerprint is ever printed.

    python3 tools/quota_probe.py keys.txt
    python3 tools/quota_probe.py keys.txt --proxy http://user:pass@host:port
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time

import requests

# Documented, cheap, and authenticated by the API key alone: one listing row.
PROBE_URL = "https://csfloat.com/api/v1/listings?limit=1&type=buy_now"
SPEND = 5  # requests to burn on the first key before testing the second


def fingerprint(key: str) -> str:
    """Name a key in the output without disclosing it."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def read_keys(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        keys = [line.strip() for line in handle if line.strip()]
    if len(keys) < 2:
        sys.exit(f"{path}: need at least two keys to compare, found {len(keys)}")
    return keys


def quota(session: requests.Session, key: str, proxy: str | None) -> dict[str, int | None]:
    """One request on `key`; return whatever the quota headers say is left."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = session.get(
        PROBE_URL,
        headers={"Authorization": key, "Accept": "application/json"},
        proxies=proxies,
        timeout=30,
    )

    def num(name: str) -> int | None:
        raw = resp.headers.get(name)
        try:
            return int(float(raw)) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "status": resp.status_code,
        "limit": num("x-ratelimit-limit"),
        "remaining": num("x-ratelimit-remaining"),
        "reset": num("x-ratelimit-reset"),
    }


def report(label: str, seen: dict[str, int | None]) -> None:
    if seen["status"] != 200:
        print(f"  {label}: HTTP {seen['status']} — quota headers unreliable, see note below")
        return
    print(f"  {label}: remaining={seen['remaining']} of limit={seen['limit']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keyfile", help="file with one API key per line")
    ap.add_argument("--proxy", help="send every request through this proxy")
    ap.add_argument("--spend", type=int, default=SPEND,
                    help=f"requests to burn on the first key (default {SPEND})")
    ap.add_argument("--gap", type=float, default=2.5,
                    help="seconds between requests (default 2.5)")
    args = ap.parse_args()

    keys = read_keys(args.keyfile)
    first, second = keys[0], keys[1]
    session = requests.Session()

    print(f"probing from {'proxy' if args.proxy else 'this machine'}, "
          f"{args.spend + 1} requests on key {fingerprint(first)}, "
          f"then 1 on key {fingerprint(second)}\n")

    start = quota(session, first, args.proxy)
    report(f"key {fingerprint(first)}, request 1", start)
    if start["status"] != 200:
        print("\nThe first request did not come back 200. A 403 saying "
              "'Disable your VPN' is the exit address being refused, not the "
              "key — rerun through a residential proxy.")
        return 1
    if start["remaining"] is None:
        print("\nNo x-ratelimit-* headers came back, so this endpoint cannot "
              "settle the question. Try the probe against a different route.")
        return 1

    for n in range(args.spend):
        time.sleep(args.gap)
        spent = quota(session, first, args.proxy)
        report(f"key {fingerprint(first)}, request {n + 2}", spent)

    time.sleep(args.gap)
    other = quota(session, second, args.proxy)
    print()
    report(f"key {fingerprint(second)}, request 1", other)

    if other["status"] != 200 or other["remaining"] is None:
        print("\nThe second key did not answer cleanly; nothing to conclude.")
        return 1

    # A per-IP budget would have carried the first key's spending over.
    drop = start["remaining"] - other["remaining"]
    expected_if_shared = args.spend + 1
    print()
    if drop >= expected_if_shared:
        print(f"PER IP. The second key came up {drop} lower, matching the "
              f"{expected_if_shared} requests already spent from this address. "
              "More keys on one address buy nothing; more addresses do.")
    elif drop <= 1:
        print("PER KEY. The second key started on its own budget, untouched by "
              "the first key's spending. Keys can be worked in parallel from "
              "one address, each with its own quota.")
    else:
        print(f"UNCLEAR: the second key came up {drop} lower, which matches "
              f"neither a private budget (0-1) nor a shared one "
              f"({expected_if_shared}). Rerun with a larger --spend before "
              "building anything on the result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
