from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import config, db
from app.agents import AgentRegistry
from app.jobs import next_event, representative_status
from app.web import create_app

IST = ZoneInfo("Asia/Kolkata")
EVENTS = "/api/v2/events"
HEARTBEAT = "/api/v2/heartbeat"
PURGE = "/api/v2/purge"


@pytest.fixture
def client(home):
    db.init_db()
    return create_app(config.load_config()).test_client()


@pytest.fixture
def key(home):
    return config.add_agent("nas1")


def post(client, key, path=EVENTS, **fields):
    return client.post(path, json=fields, headers={"X-API-Key": key})


def ids(**extra):
    return {"job_id": "J1", "target_id": "T", **extra}


def meta(name):
    conn = db.connect()
    try:
        return db.get_meta(conn, name)
    finally:
        conn.close()


class TestRegistry:
    def test_new_agents_are_picked_up_without_a_restart(self, tmp_path):
        path = tmp_path / "agents.yaml"
        registry = AgentRegistry(path)
        assert registry.find_by_key("anything") is None

        key = config.add_agent("a", path=path)
        assert registry.find_by_key(key).name == "a"

    def test_disabling_takes_effect_on_the_next_lookup(self, tmp_path):
        path = tmp_path / "agents.yaml"
        key = config.add_agent("a", path=path)
        registry = AgentRegistry(path)
        assert registry.find_by_key(key).enabled is True

        config.set_agent_enabled("a", False, path=path)
        assert registry.find_by_key(key).enabled is False

    def test_a_broken_edit_keeps_the_last_good_registry(self, tmp_path):
        path = tmp_path / "agents.yaml"
        key = config.add_agent("a", path=path)
        registry = AgentRegistry(path)
        assert registry.find_by_key(key)

        path.write_text("agents: [not valid")
        assert registry.find_by_key(key).name == "a"

    def test_unknown_agent_cannot_be_enabled(self, tmp_path):
        with pytest.raises(config.ConfigError, match="not found"):
            config.set_agent_enabled("ghost", True, path=tmp_path / "agents.yaml")


class TestIngestHttp:
    def test_events_bump_the_data_revision_only_when_something_changed(self, client, key):
        assert post(client, key, **ids()).status_code == 201
        first = int(meta("rev"))
        post(client, key, event_type="heartbeat", run_id="r1", **ids())
        assert int(meta("rev")) == first + 1

    def test_the_response_names_the_stored_event_and_run(self, client, key):
        body = post(client, key, run_id="r1", **ids()).get_json()
        assert body["success"] is True and body["event_id"] >= 1 and body["run_pk"] >= 1

    def test_purge_with_no_match_leaves_the_revision_alone(self, client, key):
        post(client, key, **ids())
        before = meta("rev")
        post(client, key, path=PURGE, job_id="J1", target_id="T", external_ids=["x"])
        assert meta("rev") == before

    @pytest.mark.parametrize("fields", [{}, {"job_id": "J1"}, {"target_id": "T"}, {"job_id": " ", "target_id": "T"}])
    def test_events_need_a_job_id_and_a_target_id(self, client, key, fields):
        resp = post(client, key, **fields)
        assert resp.status_code == 400
        assert "job_id" in resp.get_json()["error"] or "target_id" in resp.get_json()["error"]

    def test_wrong_value_type_is_a_400_naming_the_field(self, client, key):
        resp = post(client, key, percent_complete="abc", **ids())
        assert resp.status_code == 400
        assert "percent_complete" in resp.get_json()["error"]

    def test_numeric_strings_are_accepted(self, client, key):
        assert post(client, key, run_id="r", percent_complete="42", **ids()).status_code == 201

    def test_long_text_is_truncated_not_rejected(self, client, key):
        assert post(client, key, run_id="r", message="x" * 10_000, **ids()).status_code == 201
        conn = db.connect()
        (message,) = [r[0] for r in conn.execute("SELECT message FROM events")]
        conn.close()
        assert len(message) == 2000

    def test_oversized_body_is_rejected(self, client, key):
        resp = client.post(EVENTS, data="x" * (1024 * 1024 + 1), headers={
            "X-API-Key": key, "Content-Type": "application/json"})
        assert resp.status_code == 413

    def test_agent_cannot_use_another_agents_run_id_or_job_id(self, client, key, home):
        other = config.add_agent("nas2")
        post(client, key, run_id="shared", event_type="heartbeat", **ids())
        post(client, other, run_id="shared", event_type="heartbeat", **ids())
        conn = db.connect()
        agents = sorted(r[0] for r in conn.execute("SELECT agent_id FROM run_view"))
        conn.close()
        assert agents == sorted([config.find_agent("nas1").id, config.find_agent("nas2").id])

    def test_a_job_and_its_runs_follow_the_job_id_when_the_name_changes(self, client, key):
        post(client, key, run_id="r1", job_name="Old", **ids())
        post(client, key, run_id="r2", job_name="New", **ids())
        conn = db.connect()
        jobs = [tuple(r) for r in conn.execute("SELECT job_key, name FROM jobs")]
        runs = conn.execute("SELECT COUNT(*) FROM run_view WHERE job_key = 'J1'").fetchone()[0]
        conn.close()
        assert jobs == [("J1", "New")] and runs == 2

    def test_a_target_label_change_keeps_the_target(self, client, key):
        post(client, key, run_id="r1", target_label="one", **ids())
        post(client, key, run_id="r2", target_label="two", **ids())
        conn = db.connect()
        targets = [tuple(r) for r in conn.execute("SELECT target_key, label FROM targets")]
        conn.close()
        assert targets == [("T", "two")]

    def test_the_same_run_id_on_two_targets_is_two_runs(self, client, key):
        post(client, key, run_id="r", job_id="J1", target_id="A")
        post(client, key, run_id="r", job_id="J1", target_id="B")
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        conn.close()

    def test_heartbeat_records_the_version_type_and_host_without_a_run(self, client, key):
        resp = client.post(HEARTBEAT, json={"version": "1.2.3", "agent_type": "Sync", "hostname": "P3Tiny"},
                           headers={"X-API-Key": key})
        assert resp.status_code == 201
        conn = db.connect()
        row = conn.execute("SELECT version, agent_type, hostname FROM agents").fetchone()
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert tuple(row) == ("1.2.3", "Sync", "P3Tiny") and runs == 0

    def test_a_renamed_agent_keeps_its_row_and_history(self, client, key, home):
        post(client, key, run_id="r1", **ids())
        config.rename_agent("nas1", "renamed")
        post(client, key, run_id="r2", **ids())
        conn = db.connect()
        agents = [tuple(r) for r in conn.execute("SELECT name FROM agents")]
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert agents == [("renamed",)] and runs == 2

    def test_allowed_ip_restricts_where_a_key_works(self, home):
        db.init_db()
        key = config.add_agent("locked", allowed_ip="10.0.0.5")
        app = create_app(config.load_config()).test_client()

        denied = app.post(HEARTBEAT, json={}, headers={"X-API-Key": key},
                          environ_base={"REMOTE_ADDR": "10.0.0.9"})
        allowed = app.post(HEARTBEAT, json={}, headers={"X-API-Key": key},
                           environ_base={"REMOTE_ADDR": "10.0.0.5"})
        assert (denied.status_code, allowed.status_code) == (403, 201)

    def test_errors_never_echo_internal_details(self, client, key, monkeypatch):
        import sqlite3

        from app import ingest

        def boom(*_a, **_k):
            raise sqlite3.OperationalError("secret path /x/y")

        monkeypatch.setattr(ingest, "record_event", boom)
        resp = post(client, key, **ids())
        assert resp.status_code == 500
        assert "secret" not in resp.get_data(as_text=True)


class TestPurge:
    def finished(self, client, key, run_id, external_id, **extra):
        post(client, key, run_id=run_id, event_type="backup_complete", duration_seconds=1, external_id=external_id,
             **{**ids(), **extra})

    def test_marks_only_matching_runs_of_that_job_target(self, client, key):
        self.finished(client, key, "r1", "snap-1")
        self.finished(client, key, "r2", "snap-2")
        self.finished(client, key, "r3", "snap-1", target_id="OTHER")
        resp = post(client, key, path=PURGE, job_id="J1", target_id="T", external_ids=["snap-1"], message="pruned")
        assert resp.get_json() == {"success": True, "purged_runs": 1}
        conn = db.connect()
        states = {r["run_id"]: r["status"] for r in conn.execute("SELECT run_id, status FROM runs")}
        messages = [r[0] for r in conn.execute("SELECT message FROM events WHERE event_type = 'purged'")]
        conn.close()
        assert states == {"r1": "purged", "r2": "success", "r3": "success"} and messages == ["pruned"]

    def test_is_scoped_to_the_calling_agent(self, client, key):
        other = config.add_agent("nas2")
        self.finished(client, key, "r1", "same")
        post(client, other, run_id="r1", event_type="backup_complete", duration_seconds=1, external_id="same", **ids())
        post(client, other, path=PURGE, job_id="J1", target_id="T", external_ids=["same"])
        conn = db.connect()
        states = sorted(r[0] for r in conn.execute("SELECT status FROM runs"))
        conn.close()
        assert states == ["purged", "success"]

    @pytest.mark.parametrize("fields", [
        {"target_id": "T", "external_ids": ["x"]}, {"job_id": "J1", "external_ids": ["x"]},
        {"job_id": "J1", "target_id": "T"}, {"job_id": "J1", "target_id": "T", "external_ids": []}])
    def test_needs_job_target_and_ids(self, client, key, fields):
        assert post(client, key, path=PURGE, **fields).status_code == 400


class TestJobsAreRenameSafe:
    def test_hand_renaming_an_agent_or_job_never_splits_history(self, client, key):
        post(client, key, run_id="r1", job_name="Nightly", **ids())
        config.rename_agent("nas1", "backup-box")
        post(client, key, run_id="r2", job_name="Nightly (renamed)", **ids())
        conn = db.connect()
        rows = [tuple(r) for r in conn.execute("SELECT COUNT(DISTINCT agent_id), COUNT(DISTINCT job_pk) FROM run_view")]
        conn.close()
        assert rows == [(1, 1)]


class TestNextEvent:
    NOW = datetime(2026, 1, 15, 5, 30, tzinfo=IST).timestamp()

    def test_next_run_is_evaluated_in_the_configured_timezone(self):
        next_at, _ = next_event(["30 2 * * *"], None, IST, self.NOW)
        assert next_at == datetime(2026, 1, 16, 2, 30, tzinfo=IST).timestamp()

    def test_earliest_of_several_expressions_wins(self):
        next_at, _ = next_event(["0 23 * * *", "0 6 * * *"], None, IST, self.NOW)
        assert next_at == datetime(2026, 1, 15, 6, 0, tzinfo=IST).timestamp()

    def test_run_close_to_a_scheduled_time_is_not_drift(self):
        last = datetime(2026, 1, 15, 2, 31, tzinfo=IST).timestamp()
        assert next_event(["30 2 * * *"], last, IST, self.NOW)[1] is False

    def test_run_far_from_any_scheduled_time_is_drift(self):
        last = datetime(2026, 1, 15, 3, 30, tzinfo=IST).timestamp()
        assert next_event(["30 2 * * *"], last, IST, self.NOW)[1] is True

    @pytest.mark.parametrize("schedule", [None, [], [""], ["not a cron"], ["1 2 3"]])
    def test_no_usable_schedule(self, schedule):
        assert next_event(schedule, 1.0, IST, self.NOW) == (None, False)


def test_representative_status_prefers_the_most_noteworthy():
    assert representative_status({"success": 3, "failed": 1}) == "failed"
    assert representative_status({"success": 2, "running": 1}) == "running"
    assert representative_status({"weird": 1}) == "weird"
    assert representative_status({}) == "unknown"
