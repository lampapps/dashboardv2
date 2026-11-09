import types
from contextlib import closing

import pytest
from pydantic import ValidationError

from app import config, db, logs
from app.config import Config
from app.web import api_ui, create_app

NOW = 1_800_000_000.0


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def batch(*lines, log="app", batch_id=None):
    return logs.LogBatch(log=log, batch_id=batch_id, lines=list(lines))


def store(conn, *lines, agent="nas1", cfg=None, now=NOW, **kw):
    return logs.store_batch(conn, cfg or Config(), agent, batch(*lines, **kw), now)


def read(conn, **kw):
    return logs.read_lines(conn, "nas1", kw.pop("log", "app"), **kw)


def texts(page):
    return [line["text"] for line in page["lines"]]


class TestLevels:
    @pytest.mark.parametrize("raw,expected", [
        ("warn", "WARNING"), ("Fatal", "CRITICAL"), (" error ", "ERROR"), ("INFO", "INFO"),
        ("trace", None), ("", None), (None, None)])
    def test_normalize(self, raw, expected):
        assert logs.normalize_level(raw) == expected

    @pytest.mark.parametrize("text,expected", [
        ("2026-05-01 10:00:00 - ERROR - boom", "ERROR"),
        ("[WARNING] low disk", "WARNING"),
        ("INFO: started", "INFO"),
        ("level=debug nothing", None),
        ("an INFORMAL note", None),
        ("plain text", None)])
    def test_detect_from_text(self, text, expected):
        assert logs.detect_level(text) == expected

    def test_explicit_level_wins_over_text(self, conn):
        store(conn, {"message": "ERROR - looks bad", "level": "info"})
        assert read(conn)["lines"][0]["level"] == "INFO"


class TestStore:
    def test_plain_strings_and_objects(self, conn):
        assert store(conn, "one", {"message": "two", "level": "warn", "ts": NOW - 5}) == {"success": True, "stored": 2}
        lines = read(conn)["lines"]
        assert texts(read(conn)) == ["one", "two"]
        assert (lines[0]["ts"], lines[0]["level"]) == (NOW, None)
        assert (lines[1]["ts"], lines[1]["level"]) == (NOW - 5, "WARNING")

    def test_future_timestamps_are_clamped(self, conn):
        store(conn, {"message": "later", "ts": NOW + 9999})
        assert read(conn)["lines"][0]["ts"] == NOW

    def test_newlines_split_and_nul_is_dropped(self, conn):
        result = store(conn, "a\r\nb\x00c\n\nd")
        assert texts(read(conn)) == ["a", "bc", "", "d"]
        assert result["stored"] == 4

    def test_empty_message_is_one_empty_line(self, conn):
        store(conn, "")
        assert texts(read(conn)) == [""]

    def test_long_lines_are_truncated(self, conn):
        store(conn, "x" * (logs.MAX_LINE_LENGTH + 500))
        assert len(read(conn)["lines"][0]["text"]) == logs.MAX_LINE_LENGTH

    def test_split_total_is_capped(self, conn):
        store(conn, *["\n".join(["l"] * 10)] * 900)
        assert conn.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0] == logs.MAX_LINES_AFTER_SPLIT

    def test_duplicate_batch_id_is_acknowledged_not_stored(self, conn):
        store(conn, "once", batch_id="b1")
        again = store(conn, "once", batch_id="b1")
        assert again == {"success": True, "stored": 0, "duplicate": True}
        assert texts(read(conn)) == ["once"]

    def test_batch_id_is_scoped_to_agent_and_log(self, conn):
        store(conn, "x", batch_id="b1")
        store(conn, "x", batch_id="b1", log="other")
        store(conn, "x", batch_id="b1", agent="nas2")
        assert conn.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0] == 3

    def test_limit_on_logs_per_agent(self, conn):
        for i in range(logs.MAX_LOGS_PER_AGENT):
            store(conn, "x", log=f"log{i}")
        with pytest.raises(logs.LogError, match="too many logs"):
            store(conn, "x", log="one-too-many")
        store(conn, "more", log="log0")  # an existing log is still accepted
        store(conn, "x", log="one-too-many", agent="nas2")  # the limit is per agent

    def test_bumps_log_rev_only(self, conn):
        rev, log_rev = db.get_meta(conn, "rev"), db.get_meta(conn, "log_rev")
        store(conn, "x")
        assert db.get_meta(conn, "rev") == rev
        assert db.get_meta(conn, "log_rev") != log_rev


class TestValidation:
    @pytest.mark.parametrize("body", [
        {"log": "app", "lines": []},
        {"log": "app"},
        {"log": "../etc", "lines": ["x"]},
        {"log": "", "lines": ["x"]},
        {"log": "app", "lines": ["x"] * (logs.MAX_LINES_PER_POST + 1)},
        {"log": "app", "lines": [{"level": "info"}]},
        {"log": "app", "batch_id": "b" * 200, "lines": ["x"]}])
    def test_rejects_bad_payloads(self, body):
        with pytest.raises(ValidationError):
            logs.LogBatch(**body)


class TestCap:
    def test_oldest_lines_are_dropped_once_well_over_the_cap(self, conn):
        cfg = Config(retention={"log_lines_per_log": 100})
        store(conn, *[f"l{i}" for i in range(100 + logs.CAP_SLACK)], cfg=cfg)
        assert conn.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0] == 100 + logs.CAP_SLACK  # within slack
        store(conn, "newest", cfg=cfg)
        rows = texts(read(conn, limit=logs.MAX_PAGE))
        assert len(rows) == 100 and rows[-1] == "newest"
        assert rows[0] == f"l{100 + logs.CAP_SLACK - 99}"

    def test_cap_is_per_stream(self, conn):
        cfg = Config(retention={"log_lines_per_log": 100})
        store(conn, *["a"] * (100 + logs.CAP_SLACK + 1), cfg=cfg)
        store(conn, "b", cfg=cfg, log="other")
        assert texts(read(conn, log="other")) == ["b"]

    def test_purge_old_removes_aged_lines_and_batches(self, conn):
        cfg = Config(retention={"log_days": 30})
        store(conn, {"message": "old", "ts": NOW - 31 * 86400}, "fresh", batch_id="b1", now=NOW - 3 * 86400)
        store(conn, "new", batch_id="b2")
        logs.purge_old(conn, cfg, NOW)
        assert texts(read(conn)) == ["fresh", "new"]
        assert [r["batch_id"] for r in conn.execute("SELECT batch_id FROM log_batches")] == ["b2"]


class TestRead:
    @pytest.fixture
    def filled(self, conn):
        store(conn, *[{"message": f"line {i}", "level": ("ERROR" if i % 5 == 0 else "INFO" if i % 5 else None)}
                      for i in range(20)])
        store(conn, "plain", "100% done_ok", "back\\slash")
        return conn

    def test_default_is_the_newest_page_oldest_first(self, filled):
        page = read(filled, limit=5)
        assert texts(page) == ["line 18", "line 19", "plain", "100% done_ok", "back\\slash"]
        assert page["has_more_before"] is True

    def test_before_pages_backwards_until_exhausted(self, filled):
        first = read(filled, limit=15)
        older = read(filled, limit=15, before=first["lines"][0]["id"])
        assert texts(older) == [f"line {i}" for i in range(8)]
        assert older["has_more_before"] is False
        assert texts(read(filled, limit=15, before=older["lines"][0]["id"])) == []

    def test_after_returns_only_newer_lines_oldest_first(self, filled):
        cursor = read(filled, limit=3)["lines"][0]["id"]
        page = read(filled, after=cursor)
        assert texts(page) == ["100% done_ok", "back\\slash"] or texts(page)[-1] == "back\\slash"
        assert all(line["id"] > cursor for line in page["lines"])
        assert page["has_more_before"] is False
        assert read(filled, after=page["lines"][-1]["id"])["lines"] == []

    def test_limit_is_clamped(self, filled):
        assert len(read(filled, limit=0)["lines"]) == 1
        assert len(read(filled, limit=10**6)["lines"]) == 23

    def test_level_filter_with_other(self, filled):
        assert {line["level"] for line in read(filled, levels=["ERROR"])["lines"]} == {"ERROR"}
        mixed = read(filled, levels=["ERROR", "other"])["lines"]
        assert {line["level"] for line in mixed} == {"ERROR", None}
        assert read(filled, levels=["bogus"])["lines"] != []  # unknown names are ignored

    def test_search_is_case_insensitive_and_literal(self, filled):
        assert texts(read(filled, query="LINE 1")) == [f"line {i}" for i in [1, *range(10, 20)]]
        assert texts(read(filled, query="100%")) == ["100% done_ok"]
        assert texts(read(filled, query="_ok")) == ["100% done_ok"]
        assert texts(read(filled, query="%")) == ["100% done_ok"]
        assert texts(read(filled, query="\\s")) == ["back\\slash"]

    def test_filters_apply_to_paging_cursors(self, filled):
        errors = read(filled, levels=["ERROR"], limit=2)
        assert texts(errors) == ["line 10", "line 15"] and errors["has_more_before"] is True
        assert texts(read(filled, levels=["ERROR"], before=errors["lines"][0]["id"])) == ["line 0", "line 5"][-2:]

    def test_unknown_stream_is_empty(self, filled):
        assert read(filled, log="nope") == {"lines": [], "has_more_before": False}

    def test_export_text(self, filled):
        text = logs.export_text(filled, "nas1", "app")
        assert text.startswith("line 0\nline 1\n") and text.endswith("back\\slash\n")
        assert logs.export_text(filled, "nas1", "nope") == ""


class TestListLogs:
    def test_counts_spans_and_ordering(self, conn):
        store(conn, {"message": "a", "level": "error", "ts": NOW - 50}, {"message": "b", "ts": NOW - 40}, log="old")
        store(conn, {"message": "c", "level": "info"}, "d", {"message": "e", "level": "info"}, log="new", agent="nas2")
        first, second = logs.list_logs(conn)
        assert (first["agent"], first["log"], first["lines"]) == ("nas2", "new", 3)
        assert first["counts"] == {"INFO": 2, "other": 1}
        assert (second["first_ts"], second["last_ts"], second["counts"]) == (NOW - 50, NOW - 40, {"ERROR": 1, "other": 1})

    def test_empty(self, conn):
        assert logs.list_logs(conn) == []


class TestHttp:
    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        monkeypatch.setattr(api_ui, "time", types.SimpleNamespace(time=lambda: NOW))
        monkeypatch.setattr("app.web.api_ingest.time", types.SimpleNamespace(time=lambda: NOW))
        return create_app(config.load_config()).test_client()

    @pytest.fixture
    def key(self, home):
        return config.add_agent("nas1")

    def post(self, client, key, body, **kw):
        return client.post("/api/v2/logs", json=body, headers={"X-API-Key": key}, **kw)

    def test_agent_posts_lines_and_viewer_reads_them(self, client, key):
        resp = self.post(client, key, {"log": "nas-sync", "lines": ["INFO - hi", {"message": "bad", "level": "error"}]})
        assert (resp.status_code, resp.get_json()) == (201, {"success": True, "stored": 2})
        listing = client.get("/api/ui/logs").get_json()
        assert set(listing) == {"logs", "now", "timezone"}
        assert [(e["agent"], e["log"], e["lines"]) for e in listing["logs"]] == [("nas1", "nas-sync", 2)]
        page = client.get("/api/ui/logs/lines?agent=nas1&log=nas-sync&levels=ERROR").get_json()
        assert [line["text"] for line in page["lines"]] == ["bad"]

    def test_duplicate_batch_returns_200(self, client, key):
        body = {"log": "app", "batch_id": "b1", "lines": ["x"]}
        assert self.post(client, key, body).status_code == 201
        resp = self.post(client, key, body)
        assert (resp.status_code, resp.get_json()["duplicate"]) == (200, True)

    def test_requires_a_valid_key(self, client, key):
        assert client.post("/api/v2/logs", json={"log": "a", "lines": ["x"]}).status_code == 401
        assert self.post(client, "wrong", {"log": "a", "lines": ["x"]}).status_code == 403

    @pytest.mark.parametrize("body", [{}, {"log": "a"}, {"log": "a", "lines": []}, {"log": "bad name", "lines": ["x"]}])
    def test_bad_payloads_are_rejected(self, client, key, body):
        assert self.post(client, key, body).status_code == 400

    def test_too_many_logs_is_a_400(self, client, key):
        for i in range(logs.MAX_LOGS_PER_AGENT):
            assert self.post(client, key, {"log": f"l{i}", "lines": ["x"]}).status_code == 201
        assert self.post(client, key, {"log": "extra", "lines": ["x"]}).status_code == 400

    def test_posting_counts_as_hearing_from_the_agent(self, client, key):
        self.post(client, key, {"log": "a", "lines": ["x"]})
        with closing(db.connect()) as conn:
            assert conn.execute("SELECT last_heartbeat FROM agent_state WHERE name = 'nas1'").fetchone()[0] == NOW

    def test_list_etag_follows_log_rev_not_rev(self, client, key):
        first = client.get("/api/ui/logs")
        assert client.get("/api/ui/logs", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        self.post(client, key, {"log": "a", "lines": ["x"]})
        assert client.get("/api/ui/logs", headers={"If-None-Match": first.headers["ETag"]}).status_code == 200

    def test_lines_endpoint_validates_and_is_not_cached(self, client, key):
        assert client.get("/api/ui/logs/lines").status_code == 400
        assert client.get("/api/ui/logs/lines?agent=nas1").status_code == 400
        self.post(client, key, {"log": "a", "lines": ["one", "two", "three"]})
        resp = client.get("/api/ui/logs/lines?agent=nas1&log=a&limit=2")
        assert resp.headers["Cache-Control"] == "no-store"
        body = resp.get_json()
        assert [line["text"] for line in body["lines"]] == ["two", "three"] and body["has_more_before"] is True
        newer = client.get(f"/api/ui/logs/lines?agent=nas1&log=a&after={body['lines'][0]['id']}").get_json()
        assert [line["text"] for line in newer["lines"]] == ["three"]
        older = client.get(f"/api/ui/logs/lines?agent=nas1&log=a&before={body['lines'][0]['id']}").get_json()
        assert [line["text"] for line in older["lines"]] == ["one"]
        assert client.get("/api/ui/logs/lines?agent=nas1&log=a&limit=abc").status_code == 200

    def test_search_term_is_matched_literally(self, client, key):
        self.post(client, key, {"log": "a", "lines": ["50% off", "5000 off"]})
        page = client.get("/api/ui/logs/lines?agent=nas1&log=a&q=50%25").get_json()
        assert [line["text"] for line in page["lines"]] == ["50% off"]

    def test_download_is_a_safe_plain_text_attachment(self, client, key):
        self.post(client, key, {"log": "a.b-c", "lines": ["one", "<b>two</b>"]})
        resp = client.get("/api/ui/logs/download?agent=nas1&log=a.b-c")
        assert resp.status_code == 200 and resp.mimetype == "text/plain"
        assert resp.get_data(as_text=True) == "one\n<b>two</b>\n"
        assert resp.headers["Content-Disposition"] == 'attachment; filename="nas1-a.b-c.log"'
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/api/ui/logs/download?agent=nas1").status_code == 400

    def test_download_filename_cannot_inject_headers(self, client):
        resp = client.get('/api/ui/logs/download?agent=a"b%0d%0aX:1&log=x')
        assert "\n" not in resp.headers["Content-Disposition"] and '"b' not in resp.headers["Content-Disposition"]

    def test_page_renders_with_nav_highlight(self, client):
        resp = client.get("/logs")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200 and "js/logs.js" in html
        assert 'href="/logs" aria-current="page"' in html or 'aria-current="page">Logs' in html
