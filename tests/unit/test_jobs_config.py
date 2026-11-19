import pytest

from app import agents_report, config, db, ingest
from app.config import AgentConfig, Config
from app.web import create_app

from ..seed import aid

NOW = 1_800_000_000.0


@pytest.fixture
def client(home):
    db.init_db()
    return create_app(config.load_config()).test_client()


@pytest.fixture
def key(home):
    return config.add_agent("snap")


def post(client, key, body, **kw):
    return client.post("/api/v2/jobs", json=body, headers={"X-API-Key": key}, **kw)


def stored(active_only=True):
    """(agent name, job key, job name, target key, label, source, destination) of the stored targets."""
    conn = db.connect()
    try:
        where = " WHERE j.active = 1 AND t.active = 1" if active_only else ""
        return [tuple(r) for r in conn.execute(
            "SELECT a.name, j.job_key, j.name, t.target_key, t.label, t.source, t.destination FROM targets t "
            "JOIN jobs j ON j.id = t.job_id JOIN agents a ON a.id = j.agent_id" + where +
            " ORDER BY a.name, j.job_key, t.target_key")]
    finally:
        conn.close()


def one_job(job_id="id-a", name="jim-home", source="/home/jim", destination="s3:bucket/jim", **extra):
    return {"job_id": job_id, "job_name": name,
            "targets": [{"target_id": job_id, "target_label": name, "source": source, "destination": destination}],
            **extra}


class TestSubmit:
    def test_an_agent_can_report_its_jobs(self, client, key):
        resp = post(client, key, {"hostname": "P3Tiny", "jobs": [one_job("id-a", "a"), one_job("id-b", "b", "/b", "s3:b")]})
        assert (resp.status_code, resp.get_json()) == (201, {"success": True, "stored": 2})
        assert stored() == [("snap", "id-a", "a", "id-a", "a", "/home/jim", "s3:bucket/jim"),
                            ("snap", "id-b", "b", "id-b", "b", "/b", "s3:b")]

    def test_each_report_replaces_the_last_and_dropped_jobs_go_inactive(self, client, key):
        post(client, key, {"jobs": [one_job("id-a", "a"), one_job("id-b", "b")]})
        post(client, key, {"jobs": [one_job("id-b", "b", "/new", "s3:new")]})
        assert stored() == [("snap", "id-b", "b", "id-b", "b", "/new", "s3:new")]
        post(client, key, {"jobs": []})
        assert stored() == []
        assert len(stored(active_only=False)) == 2   # the rows (and so their history) are kept

    def test_a_job_that_comes_back_is_active_again(self, client, key):
        post(client, key, {"jobs": [one_job()]})
        post(client, key, {"jobs": []})
        post(client, key, {"jobs": [one_job()]})
        assert len(stored()) == 1

    def test_renaming_a_job_keeps_its_row_and_history(self, client, key):
        post(client, key, {"jobs": [one_job("id-a", "old name")]})
        conn = db.connect()
        before = conn.execute("SELECT id FROM jobs").fetchone()[0]
        conn.close()
        post(client, key, {"jobs": [one_job("id-a", "new name")]})
        conn = db.connect()
        rows = [tuple(r) for r in conn.execute("SELECT id, job_key, name FROM jobs")]
        conn.close()
        assert rows == [(before, "id-a", "new name")]

    def test_other_agents_keep_their_rows(self, client, key):
        other = config.add_agent("other")
        post(client, key, {"jobs": [one_job("id-a", "a")]})
        post(client, other, {"jobs": [one_job("id-z", "z")]})
        post(client, key, {"jobs": []})
        assert stored() == [("other", "id-z", "z", "id-z", "z", "/home/jim", "s3:bucket/jim")]

    def test_two_agents_may_use_the_same_job_id(self, client, key):
        other = config.add_agent("other")
        post(client, key, {"jobs": [one_job("same", "a")]})
        post(client, other, {"jobs": [one_job("same", "b")]})
        assert [(r[0], r[2]) for r in stored()] == [("other", "b"), ("snap", "a")]

    def test_a_sync_job_has_one_target_per_pair_keyed_by_pair_name(self, client, key):
        post(client, key, {"jobs": [{"job_id": "id-u", "job_name": "UNAS", "targets": [
            {"target_id": "backups", "source": "/mnt/backups", "destination": "nas2:jof"},
            {"target_id": "kpf", "source": "nas2:kpf", "destination": "/mnt/kpf"}]}]})
        assert [(r[2], r[3], r[4]) for r in stored()] == [("UNAS", "backups", "backups"), ("UNAS", "kpf", "kpf")]

    def test_a_dropped_pair_goes_inactive_but_its_runs_stay(self, client, key):
        post(client, key, {"jobs": [{"job_id": "j", "job_name": "J", "targets": [
            {"target_id": "p1"}, {"target_id": "p2"}]}]})
        post(client, key, {"jobs": [{"job_id": "j", "job_name": "J", "targets": [{"target_id": "p1"}]}]})
        assert [r[3] for r in stored()] == ["p1"]
        assert [r[3] for r in stored(active_only=False)] == ["p1", "p2"]

    def test_reporting_counts_as_hearing_from_the_agent_and_records_the_host(self, client, key):
        post(client, key, {"hostname": "P3Tiny", "jobs": []})
        conn = db.connect()
        row = conn.execute("SELECT hostname, last_heartbeat FROM agents WHERE name = 'snap'").fetchone()
        conn.close()
        assert row["hostname"] == "P3Tiny" and row["last_heartbeat"] > 0

    def test_agent_text_is_trimmed_and_capped(self, client, key):
        post(client, key, {"jobs": [{"job_id": " j ", "job_name": "  j  ", "targets": [
            {"target_id": "t" * 500, "source": "s" * 5000, "destination": None}]}]})
        ((_, job_key, job, target, _label, source, destination),) = stored()
        assert (job_key, job, len(target), len(source), destination) == ("j", "j", 200, 1000, "")

    @pytest.mark.parametrize("body", [
        {}, {"jobs": "x"}, {"jobs": [{"job_name": "no id", "targets": []}]}, {"jobs": [{"job_id": "   "}]},
        {"jobs": [{"job_id": "j", "targets": [{"source": "/s"}]}]},
        {"jobs": [one_job()] * (ingest.MAX_JOBS + 1)},
        {"jobs": [{"job_id": "j", "targets": [{"target_id": "t"}] * (ingest.MAX_TARGETS_PER_JOB + 1)}]},
    ])
    def test_invalid_reports_are_rejected_and_change_nothing(self, client, key, body):
        post(client, key, {"jobs": [one_job("id-keep", "keep")]})
        assert post(client, key, body).status_code == 400
        assert [r[2] for r in stored()] == ["keep"]

    def test_requires_a_valid_key(self, client, key):
        assert client.post("/api/v2/jobs", json={"jobs": []}).status_code == 401
        assert post(client, "wrong", {"jobs": []}).status_code == 403

    def test_a_disabled_agent_is_refused(self, client, key):
        config.set_agent_enabled("snap", False)
        assert post(client, key, {"jobs": [one_job()]}).status_code == 403
        assert stored() == []

    def test_the_old_agent_endpoints_are_gone(self, client, key):
        for path in ("/api/monitoring/events", "/api/monitoring/target-purged"):
            assert client.post(path, json={}, headers={"X-API-Key": key}).status_code == 404


class TestSchedules:
    def schedules(self):
        conn = db.connect()
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT j.job_key, s.cron, s.enabled FROM job_schedules s JOIN jobs j ON j.id = s.job_id "
                "ORDER BY j.job_key, s.cron")]
        finally:
            conn.close()

    def test_schedules_are_stored_with_their_enabled_state(self, client, key):
        post(client, key, {"jobs": [
            {"job_id": "a", "schedules": [{"cron": "0 2 * * *", "enabled": True}, {"cron": " 0 9 * * 1 "}]},
            {"job_id": "b", "schedules": []}, {"job_id": "c"}]})
        assert self.schedules() == [("a", "0 2 * * *", 1), ("a", "0 9 * * 1", 0)]

    def test_each_report_replaces_the_last(self, client, key):
        post(client, key, {"jobs": [{"job_id": "a", "schedules": [{"cron": "* * * * *", "enabled": True}]}]})
        post(client, key, {"jobs": [{"job_id": "a", "schedules": [{"cron": "0 1 * * *", "enabled": False}]}]})
        assert self.schedules() == [("a", "0 1 * * *", 0)]

    @pytest.mark.parametrize("schedules", [
        "daily", [{}], [{"cron": ""}], [{"cron": "* * * * *", "enabled": "maybe"}],
        [{"cron": "* * * * *"}] * (ingest.MAX_SCHEDULES_PER_JOB + 1),
    ])
    def test_invalid_schedules_are_rejected(self, client, key, schedules):
        assert post(client, key, {"jobs": [{"job_id": "a", "schedules": schedules}]}).status_code == 400


class TestAgentCard:
    AGENT = AgentConfig(id=aid("snap"), name="snap", key_hash="0" * 64)

    def card(self, conn):
        return agents_report.build(conn, Config(), [self.AGENT], NOW, "7d")["agents"][0]

    @pytest.fixture
    def conn(self, home):
        db.init_db()
        connection = db.connect()
        yield connection
        connection.close()

    def report(self, conn, jobs):
        agent = self.AGENT
        ingest.record_jobs(conn, agent, ingest.JobsPayload.model_validate({"jobs": jobs}), NOW)

    def test_jobs_merge_the_schedule_with_what_they_copy(self, conn):
        self.report(conn, [one_job("id-a", "jim-home", schedules=[{"cron": "0 0 * * *", "enabled": True}])])
        assert self.card(conn)["jobs_config"] == [{
            "job_key": "id-a", "job_name": "jim-home", "schedules": [{"cron": "0 0 * * *", "enabled": True}],
            "targets": [{"target_id": "id-a", "label": "jim-home", "source": "/home/jim", "destination": "s3:bucket/jim"}]}]

    def test_jobs_sort_by_name_ignoring_case_and_targets_by_label(self, conn):
        self.report(conn, [
            {"job_id": "1", "job_name": "b-job"},
            {"job_id": "2", "job_name": "A-job", "targets": [
                {"target_id": "z", "target_label": "z"}, {"target_id": "a", "target_label": "a"}]}])
        jobs = self.card(conn)["jobs_config"]
        assert [j["job_name"] for j in jobs] == ["A-job", "b-job"]
        assert [t["label"] for t in jobs[0]["targets"]] == ["a", "z"]

    def test_disabled_schedules_are_shown_as_disabled(self, conn):
        self.report(conn, [{"job_id": "1", "job_name": "off", "schedules": [{"cron": "0 2 * * *", "enabled": False}]}])
        assert self.card(conn)["jobs_config"][0]["schedules"] == [{"cron": "0 2 * * *", "enabled": False}]

    def test_jobs_dropped_from_the_report_leave_the_card(self, conn):
        self.report(conn, [one_job("1", "gone")])
        self.report(conn, [])
        assert self.card(conn)["jobs_config"] == []

    def test_an_agent_with_nothing_reported_has_no_jobs(self, conn):
        assert self.card(conn)["jobs_config"] == []
