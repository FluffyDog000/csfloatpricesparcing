"""Backups to the cloud through rclone, tested against a stand-in rclone
that keeps the "remote" in a local folder."""
import gzip
import logging
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

logging.disable(logging.WARNING)

FAKE = r'''#!{python}
import os, shutil, sys
root = os.environ["FAKE_REMOTE_DIR"]
def local(p):
    if ":" in p and not os.path.isabs(p):
        p = p.split(":", 1)[1]
        return os.path.join(root, p)
    return p
cmd, *a = sys.argv[1:]
if cmd == "copyto":
    src, dst = local(a[0]), local(a[1])
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(src, dst)
elif cmd == "lsf":
    d = local(a[-1])
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            print(n)
elif cmd == "deletefile":
    os.remove(local(a[0]))
else:
    sys.exit(3)
'''


def _fake_rclone(tmp: Path) -> None:
    script = tmp / "rclone"
    script.write_text(FAKE.replace("{python}", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    os.environ["CSFLOAT_RCLONE"] = str(script)
    os.environ["FAKE_REMOTE_DIR"] = str(tmp / "remote")
    os.environ["CSFLOAT_CLOUD_REMOTE"] = "gdrive:csfloat-backup"


def _config(tmp: Path):
    from src.config import load_config
    from src.db import Database

    cfg = load_config()
    cfg.db_path = tmp / "t.db"
    cfg.backups_dir = tmp / "backups"
    cfg.telegram.bot_token = ""
    cfg.telegram.chat_id = ""
    db = Database(cfg.db_path)
    db.add_item("AK-47 | Redline (Field-Tested)")
    db.close()
    return cfg


def teardown_function(_):
    for k in ("CSFLOAT_RCLONE", "FAKE_REMOTE_DIR", "CSFLOAT_CLOUD_REMOTE",
              "CSFLOAT_CLOUD_KEEP"):
        os.environ.pop(k, None)


def test_an_export_lands_in_the_cloud_packed_and_whole():
    from src.backup_service import export_db

    tmp = Path(tempfile.mkdtemp())
    _fake_rclone(tmp)
    cfg = _config(tmp)
    assert export_db(cfg, reason="test")

    sent = list((tmp / "remote" / "csfloat-backup").iterdir())
    assert len(sent) == 1 and sent[0].name.endswith(".db.gz")
    unpacked = tmp / "back.db"
    unpacked.write_bytes(gzip.decompress(sent[0].read_bytes()))
    names = [r[0] for r in sqlite3.connect(unpacked).execute(
        "SELECT market_hash_name FROM items")]
    assert names == ["AK-47 | Redline (Field-Tested)"]


def test_only_the_newest_copies_are_kept():
    from src import cloud

    tmp = Path(tempfile.mkdtemp())
    _fake_rclone(tmp)
    os.environ["CSFLOAT_CLOUD_KEEP"] = "3"
    for day in range(1, 7):
        f = tmp / f"csfloat-2026-10-0{day}_04-00.db.gz"
        f.write_bytes(b"x")
        assert cloud.upload(f)[0]
    assert cloud.prune() == 3
    assert cloud.listing() == [f"csfloat-2026-10-0{d}_04-00.db.gz" for d in (4, 5, 6)]


def test_without_rclone_the_failure_says_how_to_install_it():
    from src import cloud

    os.environ["CSFLOAT_CLOUD_REMOTE"] = "gdrive:x"
    os.environ["CSFLOAT_RCLONE"] = "/nonexistent/rclone"
    ok, why = cloud.upload(Path(__file__))
    assert not ok and "rclone.org/install.sh" in why


def test_nothing_configured_is_no_export():
    from src.backup_service import export_db

    tmp = Path(tempfile.mkdtemp())
    cfg = _config(tmp)
    assert export_db(cfg) is False
