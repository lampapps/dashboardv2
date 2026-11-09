"""Heartbeat-only calls: no target_id means no job is created."""

import time


def test_bare_heartbeat_updates_agent_record(backend, agent, reporter):
    before = time.time()
    resp = reporter.event(version="1.4.0", agent_type="backup_agent")
    after = time.time()

    assert resp.status_code == 201
    assert resp.json() == {"success": True}
    info = backend.agent_info(agent)
    assert info["version"] == "1.4.0"
    assert info["agent_type"] == "backup_agent"
    assert before - 1 <= info["last_heartbeat"] <= after + 1
    assert backend.runs(agent) == []


def test_empty_object_is_a_heartbeat(backend, agent, reporter):
    resp = reporter.event()
    assert resp.status_code == 201
    assert resp.json() == {"success": True}
    assert backend.agent_info(agent)["last_heartbeat"] is not None


def test_scheduler_check_heartbeat_creates_no_run(backend, agent, reporter):
    resp = reporter.event(
        event_type="heartbeat", message="Scheduler check completed. 0 job(s) triggered.",
        stage="Scheduler check", version="1.0", agent_type="Local Sync",
    )
    assert resp.status_code == 201
    assert "event_id" not in resp.json()
    assert backend.runs(agent) == []


def test_version_and_type_persist_when_later_heartbeats_omit_them(backend, agent, reporter):
    reporter.event(version="2.0.0", agent_type="Snapshot")
    reporter.event(message="alive")
    info = backend.agent_info(agent)
    assert info["version"] == "2.0.0"
    assert info["agent_type"] == "Snapshot"


def test_cron_schedule_with_job_name_is_stored_and_replaced(backend, agent, reporter):
    assert reporter.event(job_name="J", cron_schedule="0 2 * * *").status_code == 201
    assert backend.schedules(agent) == {"J": "0 2 * * *"}

    reporter.event(job_name="J", cron_schedule="0 3 * * *, 0 15 * * *")
    assert backend.schedules(agent) == {"J": "0 3 * * *, 0 15 * * *"}


def test_cron_schedule_without_job_name_is_ignored(backend, agent, reporter):
    reporter.event(cron_schedule="0 2 * * *")
    assert backend.schedules(agent) == {}


def test_schedules_are_per_agent(backend, agent, other_agent, reporter, other_reporter):
    reporter.event(job_name="J", cron_schedule="0 1 * * *")
    other_reporter.event(job_name="J", cron_schedule="0 5 * * *")
    assert backend.schedules(agent) == {"J": "0 1 * * *"}
    assert backend.schedules(other_agent) == {"J": "0 5 * * *"}
