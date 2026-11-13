import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import activity, config, db, jobs, overview, tick
from app.web import api_ui, create_app

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=IST).timestamp()


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 1, day, hour, minute, tzinfo=IST).timestamp()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def add_run(conn, agent="a", job="J", status="success", started=NOW - 60, completed=None,
            runtime=None, job_run_id=None, percent=None, current_item=None, eta=None, error=None):
    conn.execute(
        "INSERT INTO runs (agent, job_name, status, started_at, completed_at, runtime_seconds, "
        "job_run_id, percent_complete, current_item, eta_seconds, error_message, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent, job, status, started, completed, runtime, job_run_id, percent, current_item, eta, error, started),
    )


class TestBuckets:
    @pytest.mark.parametrize("status,expected", [
        ("success", "success"), ("completed", "success"), ("purged", "purged"),
        ("failed", "failed"), ("error", "failed"), ("stopped", "stopped"),
        ("running", "running"), ("skipped", "other"), ("weird", "other"),
    ])
    def test_bucket(self, status, expected):
        assert activity.bucket(status) == expected


class TestActivity:
    def test_days_are_grouped_in_the_configured_timezone(self, conn):
        add_run(conn, started=at(14, 23, 30))
        add_run(conn, started=at(15, 0, 30))
        result = activity.activity(conn, IST, NOW, days=2, runs_days=30)
        assert result["days"] == ["2026-01-14", "2026-01-15"]
        assert result["series"]["success"] == [1, 1]

    def test_every_bucket_has_a_value_for_every_day(self, conn):
        result = activity.activity(conn, IST, NOW, days=3, runs_days=30)
        assert set(result["series"]) == set(activity.BUCKETS)
        assert all(values == [0, 0, 0] for values in result["series"].values())

    def test_running_and_failed_runs_are_counted_live(self, conn):
        add_run(conn, status="running")
        add_run(conn, status="error")
        series = activity.activity(conn, IST, NOW, days=1, runs_days=30)["series"]
        assert (series["running"], series["failed"]) == ([1], [1])

    def test_can_be_limited_to_one_agent(self, conn):
        add_run(conn, agent="a")
        add_run(conn, agent="b")
        series = activity.activity(conn, IST, NOW, days=1, runs_days=30, agent="a")["series"]
        assert series["success"] == [1]

    def test_older_days_come_from_the_rollup_after_raw_runs_are_gone(self, conn):
        add_run(conn, started=at(10, 9))
        add_run(conn, started=at(10, 10), status="failed")
        activity.rollup_daily(conn, IST, NOW, runs_days=30)
        conn.execute("DELETE FROM runs")

        result = activity.activity(conn, IST, NOW, days=7, runs_days=3)
        index = result["days"].index("2026-01-10")
        assert result["series"]["success"][index] == 1
        assert result["series"]["failed"][index] == 1

    def test_rollup_never_overwrites_days_outside_the_live_window(self, conn):
        add_run(conn, started=at(10, 9))
        activity.rollup_daily(conn, IST, NOW, runs_days=30)
        conn.execute("DELETE FROM runs")
        activity.rollup_daily(conn, IST, NOW, runs_days=3)
        row = conn.execute("SELECT SUM(runs) FROM daily_runs WHERE day = '2026-01-10'").fetchone()
        assert row[0] == 1

    def test_rollup_is_idempotent_and_tracks_changes_inside_the_window(self, conn):
        add_run(conn, status="running")
        activity.rollup_daily(conn, IST, NOW, runs_days=30)
        conn.execute("UPDATE runs SET status = 'success'")
        activity.rollup_daily(conn, IST, NOW, runs_days=30)
        activity.rollup_daily(conn, IST, NOW, runs_days=30)
        rows = [tuple(r) for r in conn.execute("SELECT bucket, runs FROM daily_runs")]
        assert rows == [("success", 1)]


class TestJobRollupDetails:
    def test_recent_runs_are_oldest_first_and_capped(self, conn):
        for i in range(jobs.RECENT_RUNS + 5):
            add_run(conn, status="failed" if i == 0 else "success", started=NOW - 1000 + i)
        (row,) = jobs.job_rollups(conn, IST, NOW)
        assert len(row["recent"]) == jobs.RECENT_RUNS
        assert row["recent"][-1]["started_at"] == NOW - 1000 + jobs.RECENT_RUNS + 4
        assert all(r["status"] == "success" for r in row["recent"])

    def test_targets_of_one_invocation_are_one_run_with_summed_runtime(self, conn):
        add_run(conn, job_run_id="x", started=NOW - 100, completed=NOW - 90, runtime=10)
        add_run(conn, job_run_id="x", started=NOW - 90, completed=NOW - 70, runtime=20, status="failed",
                error="disk full")
        (row,) = jobs.job_rollups(conn, IST, NOW)
        assert len(row["recent"]) == 1
        assert row["last_status"] == "failed"
        assert row["last_runtime_seconds"] == 30
        assert row["last_error"] == "disk full"

    def test_running_job_reports_progress_and_no_runtime(self, conn):
        add_run(conn, status="running", percent=42, current_item="/a", eta=90)
        (row,) = jobs.job_rollups(conn, IST, NOW)
        assert row["last_runtime_seconds"] is None
        assert row["last_progress"] == {"percent_complete": 42, "eta_seconds": 90, "current_item": "/a"}

    def test_finished_job_has_no_progress_block(self, conn):
        add_run(conn, completed=NOW, runtime=5)
        (row,) = jobs.job_rollups(conn, IST, NOW)
        assert row["last_progress"] is None
        assert row["last_runtime_seconds"] == 5


class TestOverviewPayload:
    def cfg(self):
        return config.Config(timezone="Asia/Kolkata")

    def agents(self):
        return [config.AgentConfig(name="up1", key_hash="1" * 64, grace_minutes=60),
                config.AgentConfig(name="down1", key_hash="2" * 64, grace_minutes=60),
                config.AgentConfig(name="off", key_hash="3" * 64, enabled=False)]

    def test_stats_and_agents(self, conn):
        conn.execute("INSERT INTO agent_state (name, last_heartbeat, updated_at) VALUES ('up1', ?, ?)", (NOW - 60, NOW))
        conn.execute("INSERT INTO agent_state (name, last_heartbeat, updated_at) VALUES ('down1', ?, ?)", (NOW - 7200, NOW))
        add_run(conn, job="ok", completed=NOW - 100)
        add_run(conn, job="bad", status="failed", completed=NOW - 100)
        add_run(conn, job="go", status="running")
        add_run(conn, job="old", completed=NOW - 3 * 86400, started=NOW - 3 * 86400 - 5)

        data = overview.build(conn, self.cfg(), self.agents(), NOW, 7)
        stats = data["stats"]
        assert (stats["agents_total"], stats["agents_up"]) == (2, 1)
        assert (stats["running"], stats["success_24h"], stats["failed_24h"]) == (1, 1, 1)
        assert (stats["jobs_total"], stats["jobs_failing"]) == (4, 1)
        assert stats["success_rate_7d"] == round(2 / 3, 4)
        assert {a["name"]: a["status"] for a in data["agents"]} == {"up1": "up", "down1": "down", "off": "disabled"}

    def test_purged_runs_still_count_as_successes(self, conn):
        add_run(conn, job="a", completed=NOW - 100)
        add_run(conn, job="b", status="purged", completed=NOW - 100)
        add_run(conn, job="c", status="failed", completed=NOW - 100)
        stats = overview.build(conn, self.cfg(), [], NOW, 7)["stats"]
        assert (stats["success_24h"], stats["success_rate_7d"]) == (2, round(2 / 3, 4))
        assert overview.build(conn, self.cfg(), [], NOW, 7)["activity"]["series"]["purged"][-1] == 1

    def test_rate_is_none_without_finished_runs(self, conn):
        data = overview.build(conn, self.cfg(), [], NOW, 7)
        assert data["stats"]["success_rate_7d"] is None
        assert data["jobs"] == []

    @pytest.mark.parametrize("given,expected", [(None, 30), (0, 1), (-5, 1), (9999, 365), (14, 14)])
    def test_clamp_days(self, given, expected):
        assert overview.clamp_days(given) == expected


class TestTick:
    def test_tick_builds_the_daily_rollup(self, conn):
        add_run(conn, started=NOW - 10)
        tick.run_once(conn, config.Config(timezone="Asia/Kolkata"), NOW)
        assert conn.execute("SELECT COUNT(*) FROM daily_runs").fetchone()[0] == 1

    def test_a_failing_task_does_not_stop_the_others(self, conn, monkeypatch):
        import sqlite3

        ran = []

        def broken(*_a):
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(tick, "TASKS", [("broken", broken), ("fine", lambda *_a: ran.append(1))])
        tick.run_once(conn, config.Config(), NOW)
        assert ran == [1]


class TestOverviewHttp:
    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        monkeypatch.setattr(api_ui, "time", types.SimpleNamespace(time=lambda: 1_800_000_000.0))
        return create_app(config.load_config()).test_client()

    def test_payload_shape(self, client):
        body = client.get("/api/ui/overview").get_json()
        assert set(body) >= {"now", "timezone", "stats", "agents", "activity", "jobs", "tick_age_s"}
        assert len(body["activity"]["days"]) == 30

    def test_unchanged_data_answers_304_with_the_same_etag(self, client):
        first = client.get("/api/ui/overview")
        etag = first.headers["ETag"]
        second = client.get("/api/ui/overview", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag

    def test_a_write_changes_the_etag(self, client, home):
        etag = client.get("/api/ui/overview").headers["ETag"]
        key = config.add_agent("nas1")
        client.post("/api/monitoring/events", json={}, headers={"X-API-Key": key})
        resp = client.get("/api/ui/overview", headers={"If-None-Match": etag})
        assert resp.status_code == 200
        assert resp.headers["ETag"] != etag

    def test_range_is_part_of_the_etag_and_is_clamped(self, client):
        short = client.get("/api/ui/overview?days=7")
        assert len(short.get_json()["activity"]["days"]) == 7
        assert short.headers["ETag"] != client.get("/api/ui/overview?days=14").headers["ETag"]
        assert len(client.get("/api/ui/overview?days=100000").get_json()["activity"]["days"]) == 365


class TestPage:
    def test_static_files_are_revalidated_not_heuristically_cached(self, home):
        db.init_db()
        resp = create_app(config.load_config()).test_client().get("/static/js/ui.js")
        assert "no-cache" in resp.headers["Cache-Control"]
        assert resp.headers.get("ETag")

    def test_overview_page_renders_badges_escaped(self, home):
        db.init_db()
        settings_yaml = (
            'badges:\n  - label: \'NAS "one" <b>\'\n    image_url: http://kuma.lan/badge?a=1&b=2\n'
            "    link: https://kuma.lan/\n"
        )
        (home / "config" / "config.yaml").write_text(settings_yaml)
        html = create_app(config.load_config()).test_client().get("/").get_data(as_text=True)

        assert 'src="http://kuma.lan/badge?a=1&amp;b=2"' in html
        assert 'rel="noopener noreferrer"' in html
        assert "<b>" not in html
        assert "js/overview.js" in html and "vendor/chart.umd.min.js" in html

    def test_page_without_badges_has_no_badge_strip(self, home):
        db.init_db()
        html = create_app(config.load_config()).test_client().get("/").get_data(as_text=True)
        assert 'class="badges"' not in html
