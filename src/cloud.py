"""Database backups to cloud storage, through rclone.

Telegram refuses documents over 50 MB, and the database outgrew that: the
daily export failed with "413 Request Entity Too Large" and there was no
backup at all. rclone talks to Google Drive, Yandex Disk, S3 and some seventy
others with one command line, so the bot does not carry a client for each,
and the credentials live in rclone's own config on the server - never in
the database, the dashboard or a log line.

Configured by two lines in .env:

    CSFLOAT_CLOUD_REMOTE=gdrive:csfloat-backup   # rclone remote and folder
    CSFLOAT_CLOUD_KEEP=30                         # copies to keep (default 30)

A copy is a consistent SQLite snapshot, gzipped - such a database packs five
to ten times smaller - named by its Moscow date and time, so the folder sorts
by age and pruning keeps the newest.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("csfloat.cloud")

PREFIX = "csfloat-"
SUFFIX = ".db.gz"
DEFAULT_KEEP = 30
TIMEOUT = 1800          # seconds for one rclone command: a slow link, a big file


def remote() -> str:
    """The rclone destination, e.g. "gdrive:csfloat-backup"; "" when unset."""
    return (os.environ.get("CSFLOAT_CLOUD_REMOTE") or "").strip().rstrip("/")


def keep() -> int:
    try:
        return max(int(os.environ.get("CSFLOAT_CLOUD_KEEP") or DEFAULT_KEEP), 1)
    except ValueError:
        return DEFAULT_KEEP


def rclone() -> str:
    return (os.environ.get("CSFLOAT_RCLONE") or "rclone").strip()


def configured() -> bool:
    return bool(remote())


def gzip_file(src: Path, dest: Path) -> Path:
    """Pack a file. Level 6: most of the gain of 9 at a fraction of the time."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    return dest


def gunzip_file(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    return dest


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([rclone(), *args], capture_output=True, text=True,
                          timeout=TIMEOUT)


def _fail(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    return text[-1][:300] if text else f"rclone вышел с кодом {proc.returncode}"


def upload(path: Path, name: str | None = None) -> tuple[bool, str]:
    """Copy one file to the remote. Returns (ok, what happened)."""
    dest = f"{remote()}/{name or path.name}"
    try:
        proc = _run("copyto", str(path), dest)
    except FileNotFoundError:
        return False, ("rclone не установлен — curl https://rclone.org/install.sh "
                       "| sudo bash")
    except subprocess.TimeoutExpired:
        return False, "загрузка не уложилась в 30 минут"
    if proc.returncode != 0:
        return False, _fail(proc)
    return True, dest


def listing() -> list[str]:
    """Our copies on the remote, oldest first."""
    proc = _run("lsf", "--files-only", remote())
    if proc.returncode != 0:
        raise RuntimeError(_fail(proc))
    names = [n.strip() for n in proc.stdout.splitlines()
             if n.strip().startswith(PREFIX) and n.strip().endswith(SUFFIX)]
    return sorted(names)


def prune(count: int | None = None) -> int:
    """Delete all but the newest `count` copies. Returns how many went."""
    count = count or keep()
    try:
        names = listing()
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list cloud backups to prune: %s", exc)
        return 0
    gone = 0
    for name in names[:-count] if len(names) > count else []:
        proc = _run("deletefile", f"{remote()}/{name}")
        if proc.returncode == 0:
            gone += 1
        else:
            log.warning("Could not delete old cloud backup %s: %s", name, _fail(proc))
    return gone


def download(name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = _run("copyto", f"{remote()}/{name}", str(dest))
    if proc.returncode != 0:
        raise RuntimeError(_fail(proc))
    return dest


def human(size: int) -> str:
    return f"{size / 1_048_576:.1f} МБ"
