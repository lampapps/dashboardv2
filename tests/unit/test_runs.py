import types

import pytest

from app import activity, agents_report, config, db, runs
from app.config import AgentConfig, Config
from app.web import api_ui, create_app

NOW = 1_800_000_000.0
HOUR = 3600


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def add_run(conn, agent="a", job="J", status="success", started=NOW - HOUR, runtime=10.0, target="T",
            label="T label", bytes_=1000, files=5, error=None, percent=None, events=()):
    cursor = conn.execute(
        "INSERT INTO runs (agent, job_name, target_id, target_label, status, started_at, completed_at, "
        "runtime_seconds, files_count, bytes_processed, error_message, percent_complete, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, job, target, label, status, started, None if status == "running" else started + (runtime or 0),
         None if status == "running" else runtime, files, bytes_, error, percent, started))
    run_pk = cursor.lastrowid
    for i, (event_type, message) in enumerate(events):
        conn.execute("INSERT INTO events (run_pk, event_type, message, timestamp, created_at) VALUES (?, ?, ?, ?, ?)",
                     (run_pk, event_type, message, started + i, started + i))
    return run_pk


class TestListRuns:
    def test_pages_are_newest_first_and_report_the_total(self, conn):
        for i in range(7):
            add_run(conn, started=NOW - 1000 + i)
        first = runs.list_runs(conn, "a", "J", limit=3, offset=0)
        second = runs.list_runs(conn, "a", "J", limit=3, offset=3)
        assert first["total"] == 7
        started = [r["started_at"] for r in first["runs"] + second["runs"]]
        assert started == sorted(started, reverse=True)
        assert len(first["runs"]) == len(second["runs"]) == 3
        assert len(runs.list_runs(conn, "a", "J", limit=3, offset=6)["runs"]) == 1

    def test_limits_are_clamped(self, conn):
        add_run(conn)
        assert runs.list_runs(conn, "a", "J", limit=0)["limit"] == 1
        assert runs.list_runs(conn, "a", "J", limit=10_000)["limit"] == runs.MAX_PAGE_SIZE
        assert runs.list_runs(conn, "a", "J", offset=-5)["offset"] == 0

    def test_only_the_requested_agent_and_job_are_returned(self, conn):
        add_run(conn)
        add_run(conn, agent="b")
        add_run(conn, job="other")
        assert runs.list_runs(conn, "a", "J")["total"] == 1

    @pytest.mark.parametrize("bucket,expected", [
        ("success", {"success", "completed"}), ("failed", {"failed", "error"}),
        ("stopped", {"stopped"}), ("running", {"running"}), ("other", {"skipped"}), ("purged", {"purged"}),
    ])
    def test_status_filter_uses_display_buckets(self, conn, bucket, expected):
        for status in ("success", "completed", "purged", "failed", "error", "stopped", "running", "skipped"):
            add_run(conn, status=status)
        found = {r["status"] for r in runs.list_runs(conn, "a", "J", status=bucket)["runs"]}
        assert found == expected

    def test_target_and_age_filters(self, conn):
        add_run(conn, target="X", started=NOW - 10 * HOUR)
        add_run(conn, target="Y", started=NOW - HOUR)
        assert runs.list_runs(conn, "a", "J", target="X")["total"] == 1
        assert runs.list_runs(conn, "a", "J", since=NOW - 2 * HOUR)["total"] == 1

    def test_message_prefers_the_closing_event(self, conn):
        add_run(conn, events=[("heartbeat", "Starting"), ("backup_complete", "All done"), ("heartbeat", "Verify passed")])
        add_run(conn, status="running", started=NOW - 10, events=[("heartbeat", "Starting"), ("heartbeat", "Halfway")])
        messages = {r["status"]: r["message"] for r in runs.list_runs(conn, "a", "J")["runs"]}
        assert messages == {"success": "All done", "running": "Halfway"}

    def test_an_empty_job_name_is_a_valid_job(self, conn):
        add_run(conn, job="")
        assert runs.list_runs(conn, "a", "")["total"] == 1


class TestTrendAndSummary:
    def test_trend_is_oldest_first_and_skips_unfinished_runs(self, conn):
        add_run(conn, started=NOW - 300, runtime=30)
        add_run(conn, started=NOW - 100, runtime=10)
        add_run(conn, status="running", started=NOW - 50)
        points = runs.trend(conn, "a", "J")
        assert [p["runtime_seconds"] for p in points] == [30, 10]
        assert len(runs.trend(conn, "a", "J", limit=1)) == 1

    def test_summary_has_targets_schedule_and_success_only_average(self, conn):
        add_run(conn, runtime=10, target="X", label="Ex")
        add_run(conn, runtime=30, target="X", label="Ex", status="purged")
        add_run(conn, status="failed", runtime=1000, target="Y")
        conn.execute("INSERT INTO job_schedules VALUES ('a', 'J', '30 2 * * *', ?)", (NOW,))
        summary = runs.job_summary(conn, Config(), NOW, "a", "J")
        assert summary["avg_runtime_seconds"] == 20
        assert summary["bytes_processed"] == 3000
        assert summary["schedule"] == "30 2 * * *"
        assert [(t["target_id"], t["runs"]) for t in summary["targets"]] == [("X", 2), ("Y", 1)]
        assert summary["rollup"]["status_counts"] == {"success": 1, "purged": 1, "failed": 1}

    def test_unknown_job_has_no_summary(self, conn):
        assert runs.job_summary(conn, Config(), NOW, "a", "missing") is None


class TestRunDetail:
    def test_events_are_in_order(self, conn):
        pk = add_run(conn, events=[("heartbeat", "one"), ("heartbeat", "two"), ("backup_complete", "three")])
        detail = runs.run_detail(conn, pk)
        assert [e["message"] for e in detail["events"]] == ["one", "two", "three"]
        assert detail["run"]["agent"] == "a" and detail["events_truncated"] is False

    def test_long_event_lists_are_truncated(self, conn, monkeypatch):
        monkeypatch.setattr(runs, "MAX_EVENTS", 2)
        pk = add_run(conn, events=[("heartbeat", str(i)) for i in range(5)])
        detail = runs.run_detail(conn, pk)
        assert len(detail["events"]) == 2 and detail["events_truncated"] is True

    def test_unknown_run(self, conn):
        assert runs.run_detail(conn, 999) is None


class TestAgentDetail:
    def agent(self, name="a"):
        return AgentConfig(name=name, key_hash=config.hash_key(name))

    def test_counts_totals_and_job_rows_are_for_this_agent_only(self, conn):
        add_run(conn, bytes_=500, runtime=10)
        add_run(conn, status="failed", bytes_=100)
        add_run(conn, status="running", started=NOW - 5)
        add_run(conn, agent="other", job="J2")
        detail = agents_report.detail(conn, Config(), self.agent(), NOW, "30d")

        assert detail["counts"]["success"] == 1 and detail["counts"]["failed"] == 1
        assert detail["counts"]["runs_now"] == 1 and detail["counts"]["total"] == 3
        assert detail["totals"]["bytes"] == 1600  # 500 + 100 + 1000 for the running run's default size
        assert [r["job_name"] for r in detail["job_rows"]] == ["J"]
        assert len(detail["activity"]["days"]) == 30

    def test_file_and_size_totals_are_limited_to_kept_runs(self, conn):
        detail = agents_report.detail(conn, Config(), self.agent(), NOW, "1y")
        assert len(detail["activity"]["days"]) == 365
        assert detail["totals"]["days"] == Config().retention.runs_days

    def test_average_duration_counts_successful_runs_only(self, conn):
        add_run(conn, runtime=10)
        add_run(conn, runtime=30)
        add_run(conn, status="failed", runtime=900)
        detail = agents_report.detail(conn, Config(), self.agent(), NOW, "7d")
        assert detail["totals"]["avg_runtime_seconds"] == 20


class TestHttp:
    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        monkeypatch.setattr(api_ui, "time", types.SimpleNamespace(time=lambda: NOW))
        config.add_agent("nas1")
        connection = db.connect()
        self.run_pk = add_run(connection, agent="nas1", events=[("backup_complete", "done")])
        add_run(connection, agent="nas1", status="failed", target="U", started=NOW - 5 * HOUR, error="disk full")
        add_run(connection, agent="nas1", job="", started=NOW - 2 * HOUR)
        db.bump_rev(connection)
        connection.close()
        return create_app(config.load_config()).test_client()

    def test_agent_detail(self, client):
        body = client.get("/api/ui/agents/nas1?range=30d").get_json()
        assert body["name"] == "nas1" and body["status"] == "new"
        assert {"activity", "counts", "totals", "job_rows", "spans", "config"} <= set(body)
        assert "key_hash" not in str(body)

    def test_unknown_agent_is_404(self, client):
        assert client.get("/api/ui/agents/ghost").status_code == 404

    def test_job_requires_agent_and_name(self, client):
        assert client.get("/api/ui/job?agent=nas1").status_code == 400
        assert client.get("/api/ui/job?name=J").status_code == 400
        assert client.get("/api/ui/job?agent=nas1&name=" + "x" * 300).status_code == 400

    def test_job_summary_and_unknown_job(self, client):
        body = client.get("/api/ui/job?agent=nas1&name=J").get_json()
        assert body["rollup"]["job_name"] == "J" and len(body["targets"]) == 2
        assert client.get("/api/ui/job?agent=nas1&name=nope").status_code == 404

    def test_unnamed_job_is_reachable(self, client):
        assert client.get("/api/ui/job?agent=nas1&name=").status_code == 200
        assert client.get("/api/ui/job/runs?agent=nas1&name=").get_json()["total"] == 1

    def test_runs_paging_and_filters(self, client):
        body = client.get("/api/ui/job/runs?agent=nas1&name=J&limit=1").get_json()
        assert (body["total"], len(body["runs"])) == (2, 1)
        failed = client.get("/api/ui/job/runs?agent=nas1&name=J&status=failed").get_json()
        assert [r["error_message"] for r in failed["runs"]] == ["disk full"]
        by_target = client.get("/api/ui/job/runs?agent=nas1&name=J&target=U").get_json()
        assert by_target["total"] == 1
        recent = client.get("/api/ui/job/runs?agent=nas1&name=J&since=24h").get_json()
        assert recent["total"] == 2
        assert client.get("/api/ui/job/runs?agent=nas1&name=J&limit=abc").get_json()["limit"] == runs.DEFAULT_PAGE_SIZE

    def test_runs_use_etags_that_depend_on_the_query(self, client):
        first = client.get("/api/ui/job/runs?agent=nas1&name=J")
        assert client.get("/api/ui/job/runs?agent=nas1&name=J", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        other = client.get("/api/ui/job/runs?agent=nas1&name=J&offset=1")
        assert other.headers["ETag"] != first.headers["ETag"]

    def test_run_detail(self, client):
        body = client.get(f"/api/ui/runs/{self.run_pk}").get_json()
        assert body["run"]["job_name"] == "J" and body["events"][0]["message"] == "done"
        assert client.get("/api/ui/runs/99999").status_code == 404

    def test_pages(self, client):
        assert 'data-agent="nas1"' in client.get("/agents/nas1").get_data(as_text=True)
        job = client.get("/agents/nas1/job?name=J").get_data(as_text=True)
        assert 'id="run-dialog"' in job and "js/job.js" in job
        assert client.get("/agents/bad name/job").status_code == 404
        assert client.get("/agents/-bad").status_code == 404


def test_every_status_bucket_has_a_filter():
    assert set(activity.BUCKET_STATUSES) == {"success", "purged", "failed", "stopped", "running"}
