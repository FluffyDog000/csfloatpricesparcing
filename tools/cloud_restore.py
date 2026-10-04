"""Restore the database from a cloud backup (see src/cloud.py).

    .venv/bin/python tools/cloud_restore.py --list
    .venv/bin/python tools/cloud_restore.py --get latest
    .venv/bin/python tools/cloud_restore.py --get csfloat-2026-10-05_04-00.db.gz --apply

Without --apply the copy is only downloaded, unpacked and checked, and the
path is printed. With --apply it replaces the live database - the current
one is kept in data/backups first. Stop the bot before applying:

    sudo systemctl stop csfloat-collector csfloat-web
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать копии в облаке")
    ap.add_argument("--get", metavar="ИМЯ", help="скачать копию: имя или latest")
    ap.add_argument("--apply", action="store_true",
                    help="заменить ею рабочую базу (бот должен быть остановлен)")
    args = ap.parse_args()

    from src import cloud
    from src.backup import restore, validate_sqlite
    from src.config import load_config

    if not cloud.configured():
        print("CSFLOAT_CLOUD_REMOTE не задан в .env", file=sys.stderr)
        return 1
    try:
        names = cloud.listing()
    except Exception as exc:  # noqa: BLE001
        print(f"не удалось прочитать облако: {exc}", file=sys.stderr)
        return 1

    if args.list or not args.get:
        if not names:
            print(f"в {cloud.remote()} копий нет")
        for name in names:
            print(name)
        return 0

    name = names[-1] if args.get == "latest" and names else args.get
    if name not in names:
        print(f"такой копии нет: {args.get}", file=sys.stderr)
        return 1
    config = load_config()
    packed = cloud.download(name, config.backups_dir / name)
    db_file = cloud.gunzip_file(packed, config.backups_dir / name[:-len(".gz")])
    packed.unlink(missing_ok=True)
    ok, why = validate_sqlite(db_file)
    if not ok:
        print(f"копия повреждена: {why}", file=sys.stderr)
        return 1
    print(f"скачано и проверено: {db_file}")
    if not args.apply:
        print("чтобы заменить рабочую базу, запусти с --apply (остановив бота)")
        return 0
    info = restore(config.db_path, db_file, config.backups_dir)
    print(f"база восстановлена из {name}"
          + (f"; прежняя сохранена в {info['backup_path']}"
             if info.get("backup_path") else ""))
    print("запусти бота: sudo systemctl start csfloat-collector csfloat-web")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
