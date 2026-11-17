import json

import pytest

from app import agents_report, config, db, ingest
from app.config import AgentConfig, Config
from app.web import create_app

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


def stored():
    conn = db.connect()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT agent, job_name, target, source, destination FROM job_targets ORDER BY job_name, target")]
    finally:
        conn.close()


def one_job(name="jim-home", source="/home/jim", destination="s3:bucket/jim"):
    return {"job_name": name, "targets": [{"name": name, "source": source, "destination": destination}]}


class TestSubmit:
    def test_an_agent_can_report_its_jobs(self, client, key):
        resp = post(client, key, {"hostname": "P3Tiny", "jobs": [one_job("a"), one_job("b", "/b", "s3:b")]})
        assert (resp.status_code, resp.get_json()) == (201, {"success": True, "stored": 2})
        assert stored() == [("snap", "a", "a", "/home/jim", "s3:bucket/jim"), ("snap", "b", "b", "/b", "s3:b")]

    def test_each_report_replaces_the_last_so_removed_jobs_disappear(self, client, key):
        post(client, key, {"jobs": [one_job("a"), one_job("b")]})
        post(client, key, {"jobs": [one_job("b", "/new", "s3:new")]})
        assert stored() == [("snap", "b", "b", "/new", "s3:new")]
        post(client, key, {"jobs": []})
        assert stored() == []

    def test_other_agents_keep_their_rows(self, client, key):
        other = config.add_agent("other")
        post(client, key, {"jobs": [one_job("a")]})
        post(client, other, {"jobs": [one_job("z")]})
        post(client, key, {"jobs": []})
        assert stored() == [("other", "z", "z", "/home/jim", "s3:bucket/jim")]

    def test_a_sync_job_has_one_target_per_pair(self, client, key):
        post(client, key, {"jobs": [{"job_name": "UNAS", "targets": [
            {"name": "backups", "source": "/mnt/backups", "destination": "nas2:jof"},
            {"name": "kpf", "source": "nas2:kpf", "destination": "/mnt/kpf"}]}]})
        assert [(r[1], r[2]) for r in stored()] == [("UNAS", "backups"), ("UNAS", "kpf")]

    def test_a_target_without_a_name_takes_the_job_name(self, client, key):
        post(client, key, {"jobs": [{"job_name": "j", "targets": [{"source": "/s", "destination": "/d"}]}]})
        assert stored() == [("snap", "j", "j", "/s", "/d")]

    def test_reporting_counts_as_hearing_from_the_agent_and_records_the_host(self, client, key):
        post(client, key, {"hostname": "P3Tiny", "jobs": []})
        conn = db.connect()
        row = conn.execute("SELECT hostname, last_heartbeat FROM agent_state WHERE name = 'snap'").fetchone()
        conn.close()
        assert row["hostname"] == "P3Tiny" and row["last_heartbeat"] > 0

    def test_agent_text_is_trimmed_and_capped(self, client, key):
        post(client, key, {"jobs": [{"job_name": "  j  ", "targets": [
            {"name": "n" * 500, "source": "s" * 5000, "destination": 7}]}]})
        ((_, job, target, source, destination),) = stored()
        assert (job, len(target), len(source), destination) == ("j", 200, 1000, "")

    @pytest.mark.parametrize("body", [
        {}, {"jobs": "x"}, {"jobs": [{"targets": []}]}, {"jobs": [{"job_name": "   "}]},
        {"jobs": [one_job()] * (ingest.MAX_JOBS + 1)},
        {"jobs": [{"job_name": "j", "targets": [{}] * (ingest.MAX_TARGETS_PER_JOB + 1)}]},
    ])
    def test_invalid_reports_are_rejected_and_change_nothing(self, client, key, body):
        post(client, key, {"jobs": [one_job("keep")]})
        assert post(client, key, body).status_code == 400
        assert [r[1] for r in stored()] == ["keep"]

    def test_requires_a_valid_key(self, client, key):
        assert client.post("/api/v2/jobs", json={"jobs": []}).status_code == 401
        assert post(client, "wrong", {"jobs": []}).status_code == 403

    def test_a_disabled_agent_is_refused(self, client, key):
        config.set_agent_enabled("snap", False)
        assert post(client, key, {"jobs": [one_job()]}).status_code == 403
        assert stored() == []


class TestSchedules:
    def reported(self):
        conn = db.connect()
        try:
            return {r[0]: json.loads(r[1]) for r in conn.execute("SELECT job_name, schedules FROM job_config")}
        finally:
            conn.close()

    def test_schedules_are_stored_with_their_enabled_state(self, client, key):
        post(client, key, {"jobs": [
            {"job_name": "a", "schedules": [{"cron": "0 2 * * *", "enabled": True}, {"cron": " 0 9 * * 1 "}]},
            {"job_name": "b", "schedules": []}, {"job_name": "c"}]})
        assert self.reported() == {"a": [{"cron": "0 2 * * *", "enabled": True}, {"cron": "0 9 * * 1", "enabled": False}],
                                   "b": []}   # "c" did not say, so nothing is claimed about it

    def test_each_report_replaces_the_last(self, client, key):
        post(client, key, {"jobs": [{"job_name": "a", "schedules": [{"cron": "* * * * *", "enabled": True}]}]})
        post(client, key, {"jobs": [{"job_name": "b", "schedules": []}]})
        assert self.reported() == {"b": []}

    @pytest.mark.parametrize("schedules", [
        "daily", [{}], [{"cron": ""}], [{"cron": "* * * * *", "enabled": "maybe"}],
        [{"cron": "* * * * *"}] * (ingest.MAX_SCHEDULES_PER_JOB + 1),
    ])
    def test_invalid_schedules_are_rejected(self, client, key, schedules):
        assert post(client, key, {"jobs": [{"job_name": "a", "schedules": schedules}]}).status_code == 400


class TestAgentCard:
    def card(self, conn):
        agent = AgentConfig(name="snap", key_hash="0" * 64)
        return agents_report.build(conn, Config(), [agent], NOW, "7d")["agents"][0]

    @pytest.fixture
    def conn(self, home):
        db.init_db()
        connection = db.connect()
        yield connection
        connection.close()

    def schedule(self, conn, job, cron):
        conn.execute("INSERT INTO job_schedules VALUES ('snap', ?, ?, 1)", (job, cron))

    def target(self, conn, job, name, source, destination):
        conn.execute("INSERT INTO job_targets VALUES ('snap', ?, ?, ?, ?, 1)", (job, name, source, destination))

    def test_jobs_merge_the_schedule_with_what_they_copy(self, conn):
        self.schedule(conn, "jim-home", "0 0 * * *")
        self.target(conn, "jim-home", "jim-home", "/home/jim", "s3:bucket/jim")
        assert self.card(conn)["jobs_config"] == [{
            "job_name": "jim-home", "cron_schedule": "0 0 * * *", "schedules": None,
            "targets": [{"name": "jim-home", "source": "/home/jim", "destination": "s3:bucket/jim"}]}]

    def test_a_job_known_by_only_one_source_still_appears(self, conn):
        self.schedule(conn, "old-agent-job", "0 3 * * *")
        self.target(conn, "no-schedule", "p", "/s", "/d")
        jobs = {j["job_name"]: j for j in self.card(conn)["jobs_config"]}
        assert jobs["old-agent-job"]["targets"] == [] and jobs["old-agent-job"]["cron_schedule"] == "0 3 * * *"
        assert jobs["no-schedule"]["cron_schedule"] is None and len(jobs["no-schedule"]["targets"]) == 1

    def test_jobs_sort_by_name_ignoring_case_and_targets_by_name(self, conn):
        for job in ("b-job", "A-job"):
            self.schedule(conn, job, "* * * * *")
        self.target(conn, "A-job", "z", "/z", "/zz")
        self.target(conn, "A-job", "a", "/a", "/aa")
        jobs = self.card(conn)["jobs_config"]
        assert [j["job_name"] for j in jobs] == ["A-job", "b-job"]
        assert [t["name"] for t in jobs[0]["targets"]] == ["a", "z"]

    def test_reported_schedules_carry_their_enabled_state(self, conn):
        self.schedule(conn, "stale", "0 1 * * *")
        conn.execute("INSERT INTO job_config VALUES ('snap', 'stale', '[]', 1)")
        conn.execute("INSERT INTO job_config VALUES ('snap', 'off', '[{\"cron\": \"0 2 * * *\", \"enabled\": false}]', 1)")
        jobs = {j["job_name"]: j for j in self.card(conn)["jobs_config"]}
        assert jobs["off"]["schedules"] == [{"cron": "0 2 * * *", "enabled": False}]
        assert jobs["stale"]["schedules"] == [] and jobs["stale"]["cron_schedule"] == "0 1 * * *"

    def test_an_agent_with_nothing_reported_has_no_jobs(self, conn):
        card = self.card(conn)
        assert card["jobs_config"] == [] and card["schedules"] == []
