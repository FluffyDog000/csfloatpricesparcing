"""The /dump command end to end, with the network stubbed out.

Covers the wiring rather than the rendering: that the command reaches the file
path at all, that a missing or unknown argument answers with words instead of
silence, and that the caption tells the reader what is in the file before they
open it.
"""
import os
import tempfile
import types
from pathlib import Path

NAME = "AWP | Printstream (Field-Tested)"


class FakeTelegram:
    def __init__(self):
        self.documents = []
        self.messages = []

    def configured(self):
        return True

    def send_document(self, path, caption=""):
        self.documents.append((Path(path), caption))
        return True

    def send_message(self, text, parse_mode=None):
        self.messages.append(text)
        return True


def service():
    import logging

    logging.disable(logging.WARNING)
    from src.backup_service import BackupService
    from src.config import load_config
    from src.db import Database

    os.environ["CSFLOAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    cfg = load_config()
    cfg.db_path = Path(os.environ["CSFLOAT_DB_PATH"])
    cfg.backups_dir = Path(tempfile.mkdtemp())
    db = Database(cfg.db_path)
    item_id = db.add_item(NAME)
    db.conn.execute(
        "INSERT INTO sales (sale_id,item_id,market_hash_name,price,float_value,"
        "sold_at,scraped_at) VALUES (?,?,?,?,?,?,?)",
        ("s1", item_id, NAME, 141.5, 0.2312, "2026-09-25T10:00:00+00:00",
         "2026-09-25T10:00:00+00:00"))
    db.set_setting("proxies", "http://user:sup3rsecret@proxy.example.com:8000")
    db.conn.commit()

    svc = BackupService(cfg, types.SimpleNamespace(db=db))
    svc.tg = FakeTelegram()
    return svc, db


def test_dump_sends_a_file_named_after_the_item():
    svc, db = service()
    svc._answer_command(f"/dump {NAME}")
    assert len(svc.tg.documents) == 1, "the command must answer with a file"
    path, caption = svc.tg.documents[0]
    assert path.exists()
    assert "Printstream" in path.name
    assert NAME in caption
    db.close()


def test_the_caption_says_what_is_inside_before_it_is_opened():
    svc, db = service()
    svc._answer_command(f"/dump {NAME}")
    _, caption = svc.tg.documents[0]
    assert "ПРОДАЖИ" in caption and "СТАКАН" in caption
    db.close()


def test_the_sent_file_holds_no_credentials():
    svc, db = service()
    svc._answer_command(f"/dump {NAME}")
    path, _ = svc.tg.documents[0]
    assert "sup3rsecret" not in path.read_text(encoding="utf-8")
    db.close()


def test_dump_without_an_item_explains_itself():
    svc, db = service()
    svc._answer_command("/dump")
    assert not svc.tg.documents
    assert svc.tg.messages, "silence reads like a broken bot"
    assert "/dump" in svc.tg.messages[0]
    db.close()


def test_an_unknown_item_lists_what_there_is():
    svc, db = service()
    svc._answer_command("/dump Butterfly Knife")
    assert not svc.tg.documents
    assert NAME in svc.tg.messages[0], "say what can be asked for instead"
    db.close()


def test_a_partial_name_is_enough():
    svc, db = service()
    svc._answer_command("/dump Printstream (Field")
    assert len(svc.tg.documents) == 1
    db.close()


def test_other_commands_still_answer_with_text():
    """The file branch must not swallow the rest of the commands."""
    svc, db = service()
    svc._answer_command("/help")
    assert not svc.tg.documents
    assert svc.tg.messages
    db.close()
