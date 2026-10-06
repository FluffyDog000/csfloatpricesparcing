"""Every response counted by kind, not only the sales polls."""
import os
import tempfile
from datetime import datetime, timezone

from src.db import Database
from src.traffic import Traffic


def test_counts_add_up_by_hour_and_kind_and_survive_a_flush():
    t = Traffic()
    at = datetime(2026, 10, 6, 14, 25, tzinfo=timezone.utc)
    t.add("book", 1000, at)
    t.add("book", 500, at)
    t.add("listings", 20000, at)
    t.add("weird", 10, at)                      # anything else on the ring
    rows = sorted(t.drain())
    assert rows == [("2026-10-06T14:00:00+00:00", "book", 2, 1500),
                    ("2026-10-06T14:00:00+00:00", "listings", 1, 20000),
                    ("2026-10-06T14:00:00+00:00", "ring", 1, 10)]
    assert t.drain() == []

    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.add_traffic(rows)
    db.add_traffic([("2026-10-06T14:00:00+00:00", "book", 1, 100)])
    got = db.traffic_since("2026-10-06T00:00:00+00:00")
    assert got["book"] == {"requests": 3, "bytes": 1600}
    assert db.traffic_since("2026-10-07T00:00:00+00:00") == {}
    db.close()


def test_the_client_counts_ring_reads_and_writes():
    from tests.test_defence import _collector
    col, db = _collector()

    class Resp:
        status_code = 200
        headers = {"Content-Length": "1234"}
        raw = None
        text = "{}"
        content = b"{}"
        url = ""

        def json(self):
            return {}

    col.client.session.request = lambda *a, **k: Resp()
    col.client.session.get = lambda *a, **k: Resp()
    try:
        col.client.send_json("POST", col.config.http.base_url + "/api/v1/buy-orders", {})
    except Exception:  # noqa: BLE001 - only the count matters here
        pass
    rows = col.client.traffic.drain()
    assert ("account" in {k for _, k, _, _ in rows}), rows
    assert sum(b for _, k, _, b in rows if k == "account") == 1234
    col.client.traffic.add("history", 300)
    col.flush_traffic()
    assert db.traffic_since("2000-01-01")["history"]["bytes"] == 300
    db.close()


def test_the_load_page_reports_traffic_by_kind():
    from tests.test_analysis_page import _app
    import webapp
    c = _app([])
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    from src.traffic import hour_of
    db.add_traffic([(hour_of(), "listings", 10, 5 * 1_048_576)])
    db.close()
    body = c.get("/api/load").get_json() if c.get("/api/load").status_code == 200 else None
    assert body is not None
    kinds = {k["kind"]: k for k in body["traffic_by_kind"]}
    assert kinds["listings"]["requests"] == 10 and kinds["listings"]["mb"] == 5.0
