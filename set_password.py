#!/usr/bin/env python3
"""Generate a password hash for the dashboard login.

Usage:
    python set_password.py                # prompts for the password
    python set_password.py "mypassword"   # hash given as an argument

Copy the printed line into your .env as DASHBOARD_PASSWORD_HASH=...
The plain password is never stored — only its hash.
"""
import getpass
import sys

from werkzeug.security import generate_password_hash


def main() -> int:
    if len(sys.argv) > 1:
        pw = sys.argv[1]
    else:
        pw = getpass.getpass("New dashboard password: ")
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if not pw:
        print("Empty password.", file=sys.stderr)
        return 1
    print("\nAdd this line to your .env:\n")
    print(f"DASHBOARD_PASSWORD_HASH={generate_password_hash(pw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
