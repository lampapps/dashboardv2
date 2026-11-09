from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import config, db
from app.agents import AgentRegistry
from app.jobs import next_event, representative_status
from app.web import create_app

IST = ZoneInfo("Asia/Kolkata")
EVENTS = "/api/monitoring/events"


@pytest.fixture
def client(home):
    db.init_db()
    return create_app(config.load_config()).test_client()


@pytest.fixture
def key(home):
    return config.add_agent("nas1")


def post(client, key, path=EVENTS, **fields):
    return client.post(path, json=fields, headers={"X-API-Key": key})


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
        assert post(client, key).status_code == 201
        first = int(meta("rev"))
        post(client, key, event_type="heartbeat", target_id="T", run_id="r1")
        assert int(meta("rev")) == first + 1

    def test_purge_with_no_match_leaves_the_revision_alone(self, client, key):
        post(client, key)
        before = meta("rev")
        post(client, key, path="/api/monitoring/target-purged", target_id="T", external_ids=["x"])
        assert meta("rev") == before

    def test_wrong_value_type_is_a_400_naming_the_field(self, client, key):
        resp = post(client, key, percent_complete="abc")
        assert resp.status_code == 400
        assert "percent_complete" in resp.get_json()["error"]

    def test_numeric_strings_are_accepted(self, client, key):
        assert post(client, key, target_id="T", run_id="r", percent_complete="42").status_code == 201

    def test_long_text_is_truncated_not_rejected(self, client, key):
        assert post(client, key, target_id="T", run_id="r", message="x" * 10_000).status_code == 201
        conn = db.connect()
        (message,) = [r[0] for r in conn.execute("SELECT message FROM events")]
        conn.close()
        assert len(message) == 2000

    def test_oversized_body_is_rejected(self, client, key):
        resp = client.post(EVENTS, data="x" * (1024 * 1024 + 1), headers={
            "X-API-Key": key, "Content-Type": "application/json"})
        assert resp.status_code == 413

    def test_blank_strings_count_as_absent(self, client, key):
        resp = post(client, key, target_id="   ", job_name="J", cron_schedule="* * * * *")
        assert resp.get_json() == {"success": True}

    def test_agent_cannot_use_another_agents_run_id(self, client, key, home):
        other = config.add_agent("nas2")
        post(client, key, target_id="T", run_id="shared", event_type="heartbeat")
        post(client, other, target_id="T", run_id="shared", event_type="heartbeat")
        conn = db.connect()
        agents = sorted(r[0] for r in conn.execute("SELECT agent FROM runs"))
        conn.close()
        assert agents == ["nas1", "nas2"]

    def test_allowed_ip_restricts_where_a_key_works(self, home):
        db.init_db()
        key = config.add_agent("locked", allowed_ip="10.0.0.5")
        app = create_app(config.load_config()).test_client()

        denied = app.post(EVENTS, json={}, headers={"X-API-Key": key},
                          environ_base={"REMOTE_ADDR": "10.0.0.9"})
        allowed = app.post(EVENTS, json={}, headers={"X-API-Key": key},
                           environ_base={"REMOTE_ADDR": "10.0.0.5"})
        assert (denied.status_code, allowed.status_code) == (403, 201)

    def test_errors_never_echo_internal_details(self, client, key, monkeypatch):
        import sqlite3

        from app import ingest

        def boom(*_a, **_k):
            raise sqlite3.OperationalError("secret path /x/y")

        monkeypatch.setattr(ingest, "record_event", boom)
        resp = post(client, key)
        assert resp.status_code == 500
        assert "secret" not in resp.get_data(as_text=True)


class TestNextEvent:
    NOW = datetime(2026, 1, 15, 5, 30, tzinfo=IST).timestamp()

    def test_next_run_is_evaluated_in_the_configured_timezone(self):
        next_at, _ = next_event("30 2 * * *", None, IST, self.NOW)
        assert next_at == datetime(2026, 1, 16, 2, 30, tzinfo=IST).timestamp()

    def test_earliest_of_several_expressions_wins(self):
        next_at, _ = next_event("0 23 * * *, 0 6 * * *", None, IST, self.NOW)
        assert next_at == datetime(2026, 1, 15, 6, 0, tzinfo=IST).timestamp()

    def test_run_close_to_a_scheduled_time_is_not_drift(self):
        last = datetime(2026, 1, 15, 2, 31, tzinfo=IST).timestamp()
        assert next_event("30 2 * * *", last, IST, self.NOW)[1] is False

    def test_run_far_from_any_scheduled_time_is_drift(self):
        last = datetime(2026, 1, 15, 3, 30, tzinfo=IST).timestamp()
        assert next_event("30 2 * * *", last, IST, self.NOW)[1] is True

    @pytest.mark.parametrize("schedule", [None, "", "  ", "not a cron", "1 2 3"])
    def test_no_usable_schedule(self, schedule):
        assert next_event(schedule, 1.0, IST, self.NOW) == (None, False)


def test_representative_status_prefers_the_most_noteworthy():
    assert representative_status({"success": 3, "failed": 1}) == "failed"
    assert representative_status({"success": 2, "running": 1}) == "running"
    assert representative_status({"weird": 1}) == "weird"
    assert representative_status({}) == "unknown"
