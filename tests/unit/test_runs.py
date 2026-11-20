import types

import pytest

from app import activity, agents_report, config, db, runs
from app.config import AgentConfig, Config
from app.web import api_ui, create_app

from ..seed import NOW, add_run, add_schedule

AID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"   # an agent id that passes the registry's format check
HOUR = 3600


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


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

    def test_runs_are_found_by_job_key_whatever_the_job_is_called(self, conn):
        add_run(conn, job="id-1", job_name="old name")
        conn.execute("UPDATE jobs SET name = 'new name'")
        assert runs.list_runs(conn, "a", "id-1")["total"] == 1
        assert runs.list_runs(conn, "a", "new name")["total"] == 0


class TestTrendAndSummary:
    def test_trend_is_oldest_first_and_skips_unfinished_runs(self, conn):
        add_run(conn, started=NOW - 300, runtime=30)
        add_run(conn, started=NOW - 100, runtime=10)
        add_run(conn, status="running", started=NOW - 50)
        points = runs.trend(conn, "a", "J")
        assert [p["runtime_seconds"] for p in points] == [30, 10]
        assert len(runs.trend(conn, "a", "J", limit=1)) == 1

    def test_trend_points_name_their_target(self, conn):
        add_run(conn, started=NOW - 300, runtime=30, target="X", label="Ex")
        add_run(conn, started=NOW - 100, runtime=10, target="Y")
        assert [(p["target_id"], p["target_label"]) for p in runs.trend(conn, "a", "J")] == [("X", "Ex"), ("Y", "Y")]
    def test_summary_has_targets_schedule_and_success_only_average(self, conn):
        add_run(conn, agent=AID, runtime=10, target="X", label="Ex")
        add_run(conn, agent=AID, runtime=30, target="X", label="Ex", status="purged")
        add_run(conn, agent=AID, status="failed", runtime=1000, target="Y")
        add_schedule(conn, AID, "J", "30 2 * * *")
        add_schedule(conn, AID, "J", "0 9 * * 1", enabled=False)
        summary = runs.job_summary(conn, Config(), NOW, AgentConfig(id=AID, name="nas", key_hash=config.hash_key("k")), "J")
        assert summary["avg_runtime_seconds"] == 20
        assert summary["bytes_processed"] == 3000
        assert summary["agent"] == "nas" and summary["job_name"] == "J"
        assert summary["schedules"] == [{"cron": "0 9 * * 1", "enabled": False}, {"cron": "30 2 * * *", "enabled": True}]
        assert [(t["target_id"], t["runs"]) for t in summary["targets"]] == [("X", 2), ("Y", 1)]
        assert summary["rollup"]["status_counts"] == {"success": 1, "purged": 1, "failed": 1}

    def test_unknown_job_has_no_summary(self, conn):
        agent = AgentConfig(id=AID, name="nas", key_hash=config.hash_key("k"))
        assert runs.job_summary(conn, Config(), NOW, agent, "missing") is None


class TestRunDetail:
    def test_events_are_in_order(self, conn):
        pk = add_run(conn, events=[("heartbeat", "one"), ("heartbeat", "two"), ("backup_complete", "three")])
        detail = runs.run_detail(conn, pk)
        assert [e["message"] for e in detail["events"]] == ["one", "two", "three"]
        assert detail["run"]["agent_id"] == "a" and detail["events_truncated"] is False

    def test_long_event_lists_are_truncated(self, conn, monkeypatch):
        monkeypatch.setattr(runs, "MAX_EVENTS", 2)
        pk = add_run(conn, events=[("heartbeat", str(i)) for i in range(5)])
        detail = runs.run_detail(conn, pk)
        assert len(detail["events"]) == 2 and detail["events_truncated"] is True

    def test_unknown_run(self, conn):
        assert runs.run_detail(conn, 999) is None


class TestAgentDetail:
    def agent(self):
        return AgentConfig(id=AID, name="a", key_hash=config.hash_key("a"))

    def test_counts_totals_and_job_rows_are_for_this_agent_only(self, conn):
        add_run(conn, agent=AID, bytes_=500, runtime=10)
        add_run(conn, agent=AID, status="failed", bytes_=100)
        add_run(conn, agent=AID, status="running", started=NOW - 5)
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
        add_run(conn, agent=AID, runtime=10)
        add_run(conn, agent=AID, runtime=30)
        add_run(conn, agent=AID, status="failed", runtime=900)
        detail = agents_report.detail(conn, Config(), self.agent(), NOW, "7d")
        assert detail["totals"]["avg_runtime_seconds"] == 20


class TestHttp:
    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        monkeypatch.setattr(api_ui, "time", types.SimpleNamespace(time=lambda: NOW))
        config.add_agent("nas1")
        self.aid = config.find_agent("nas1").id
        connection = db.connect()
        self.run_pk = add_run(connection, agent=self.aid, agent_name="nas1", job="job-1", job_name="Nightly",
                              events=[("backup_complete", "done")])
        add_run(connection, agent=self.aid, job="job-1", status="failed", target="U", started=NOW - 5 * HOUR,
                error="disk full")
        db.bump_rev(connection)
        connection.close()
        return create_app(config.load_config()).test_client()

    def q(self, **extra):
        params = {"agent": self.aid, "job": "job-1", **extra}
        return "&".join(f"{k}={v}" for k, v in params.items())

    def test_agent_detail(self, client):
        body = client.get(f"/api/ui/agents/{self.aid}?range=30d").get_json()
        assert body["name"] == "nas1" and body["id"] == self.aid and body["status"] == "new"
        assert {"activity", "counts", "totals", "job_rows", "spans", "config"} <= set(body)
        assert "key_hash" not in str(body)

    def test_agents_are_addressed_by_id_not_name(self, client):
        assert client.get("/api/ui/agents/nas1").status_code == 404
        assert client.get("/api/ui/agents/ghost").status_code == 404

    def test_job_requires_a_known_agent_and_a_job(self, client):
        assert client.get(f"/api/ui/job?agent={self.aid}").status_code == 400
        assert client.get("/api/ui/job?job=job-1").status_code == 400
        assert client.get("/api/ui/job?agent=nas1&job=job-1").status_code == 400
        assert client.get(f"/api/ui/job?agent={self.aid}&job=" + "x" * 300).status_code == 400

    def test_job_summary_and_unknown_job(self, client):
        body = client.get("/api/ui/job?" + self.q()).get_json()
        assert body["job_name"] == "Nightly" and body["agent"] == "nas1" and len(body["targets"]) == 2
        assert client.get("/api/ui/job?" + self.q(job="nope")).status_code == 404

    def test_runs_paging_and_filters(self, client):
        body = client.get("/api/ui/job/runs?" + self.q(limit=1)).get_json()
        assert (body["total"], len(body["runs"])) == (2, 1)
        failed = client.get("/api/ui/job/runs?" + self.q(status="failed")).get_json()
        assert [r["error_message"] for r in failed["runs"]] == ["disk full"]
        assert client.get("/api/ui/job/runs?" + self.q(target="U")).get_json()["total"] == 1
        assert client.get("/api/ui/job/runs?" + self.q(since="24h")).get_json()["total"] == 2
        assert client.get("/api/ui/job/runs?" + self.q(limit="abc")).get_json()["limit"] == runs.DEFAULT_PAGE_SIZE

    def test_runs_use_etags_that_depend_on_the_query(self, client):
        first = client.get("/api/ui/job/runs?" + self.q())
        assert client.get("/api/ui/job/runs?" + self.q(), headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        other = client.get("/api/ui/job/runs?" + self.q(offset=1))
        assert other.headers["ETag"] != first.headers["ETag"]

    def test_run_detail_names_the_agent_and_job(self, client):
        body = client.get(f"/api/ui/runs/{self.run_pk}").get_json()
        assert body["run"]["job_name"] == "Nightly" and body["run"]["agent"] == "nas1"
        assert body["events"][0]["message"] == "done"
        assert client.get("/api/ui/runs/99999").status_code == 404

    def test_pages_use_ids_and_job_keys(self, client):
        page = client.get(f"/agents/{self.aid}").get_data(as_text=True)
        assert f'data-agent="{self.aid}"' in page and "nas1" in page
        job = client.get(f"/agents/{self.aid}/jobs/job-1").get_data(as_text=True)
        assert 'id="run-dialog"' in job and "js/job.js" in job and 'data-job="job-1"' in job
        assert client.get("/agents/nas1").status_code == 404
        assert client.get(f"/agents/{'0' * 8}-0000-0000-0000-{'0' * 12}").status_code == 404
        assert client.get("/agents/-bad").status_code == 404


def test_every_status_bucket_has_a_filter():
    assert set(activity.BUCKET_STATUSES) == {"success", "purged", "failed", "stopped", "running"}
