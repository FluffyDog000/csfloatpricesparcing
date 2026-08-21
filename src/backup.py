"""Database backup / restore helpers.

* snapshot_db  — consistent single-file copy via SQLite's backup API (handles
                 WAL cleanly), used for the Telegram export and pre-restore
                 safety copies.
* validate_sqlite — sanity-check an uploaded/received file before trusting it.
* restore      — back up the current DB, then swap in the new file.
* generation marker — a small file next to the DB, bumped on every restore, so
                 a running collector can notice the swap and reopen its handle.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("csfloat.backup")


def _gen_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".gen")


def read_generation(db_path: Path) -> str:
    try:
        return _gen_path(db_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bump_generation(db_path: Path) -> None:
    try:
        _gen_path(db_path).write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("Could not write generation marker: %s", exc)


def snapshot_db(db_path: Path, dest: Path) -> Path:
    """Write a consistent copy of db_path to dest using the SQLite backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest


def make_backup(db_path: Path, backups_dir: Path, label: str = "backup") -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backups_dir / f"{db_path.stem}-{label}-{ts}.db"
    return snapshot_db(db_path, dest)


def prune_backups(backups_dir: Path, keep: int = 20) -> None:
    try:
        files = sorted(
            backups_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Backup prune failed: %s", exc)


def validate_sqlite(path: Path) -> tuple[bool, str]:
    """Check a file is a valid SQLite tracker DB (integrity + required tables)."""
    try:
        con = sqlite3.connect(str(path))
        try:
            res = con.execute("PRAGMA quick_check").fetchone()
            if not res or res[0] != "ok":
                return False, "integrity check failed"
            tables = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
    except sqlite3.Error as exc:
        return False, f"not a SQLite database ({exc})"
    missing = {"items", "sales"} - tables
    if missing:
        return False, f"missing tables: {', '.join(sorted(missing))}"
    return True, "ok"


def restore(db_path: Path, new_file: Path, backups_dir: Path) -> dict:
    """Validate new_file, back up the current DB, then swap new_file in.

    Callers MUST close their own DB connections first. Returns info incl. the
    pre-restore backup path. Raises ValueError if new_file is not a valid DB."""
    ok, why = validate_sqlite(new_file)
    if not ok:
        raise ValueError(f"файл не похож на базу трекера: {why}")

    backup_path = None
    if db_path.exists():
        backup_path = make_backup(db_path, backups_dir, label="pre-restore")
        log.info("Backed up current DB to %s before restore", backup_path)

    # Remove stale WAL/SHM of the current DB so the swapped-in file is used as-is.
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_file), str(db_path))
    bump_generation(db_path)
    prune_backups(backups_dir)
    log.info("Database restored from uploaded file.")
    return {"backup_path": str(backup_path) if backup_path else None}
