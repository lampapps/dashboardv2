"""POST /api/v2/heartbeat and POST /api/v2/jobs: what an agent says about itself and its jobs."""

import time


def test_bare_heartbeat_updates_agent_record(backend, agent, reporter):
    before = time.time()
    resp = reporter.heartbeat(version="1.4.0", agent_type="backup_agent")
    after = time.time()

    assert resp.status_code == 201
    assert resp.json() == {"success": True}
    info = backend.agent_info(agent)
    assert info["version"] == "1.4.0"
    assert info["agent_type"] == "backup_agent"
    assert before - 1 <= info["last_heartbeat"] <= after + 1
    assert backend.runs(agent) == []


def test_empty_object_is_a_heartbeat(backend, agent, reporter):
    resp = reporter.heartbeat()
    assert resp.status_code == 201
    assert resp.json() == {"success": True}
    assert backend.agent_info(agent)["last_heartbeat"] is not None


def test_version_and_type_persist_when_later_heartbeats_omit_them(backend, agent, reporter):
    reporter.heartbeat(version="2.0.0", agent_type="Snapshot")
    reporter.heartbeat()
    info = backend.agent_info(agent)
    assert info["version"] == "2.0.0"
    assert info["agent_type"] == "Snapshot"


def test_reported_hostname_is_accepted_even_where_it_is_ignored(backend, agent, reporter):
    assert reporter.heartbeat(version="1.0", hostname="P3Tiny").status_code == 201
    assert reporter.heartbeat(hostname="bad host with spaces").status_code == 201
    assert backend.agent_info(agent)["version"] == "1.0"


def test_a_renamed_agent_is_the_same_agent(backend, agent, reporter):
    reporter.heartbeat(version="1.0")
    renamed = backend.rename(agent, agent.name + "-renamed")
    assert reporter.heartbeat(version="1.1").status_code == 201
    assert backend.agent_info(renamed)["version"] == "1.1"


def _job(job_id, name, schedules=(), targets=()):
    return {"job_id": job_id, "job_name": name, "schedules": [{"cron": c, "enabled": e} for c, e in schedules],
            "targets": [{"target_id": t, "target_label": t, "source": "/s", "destination": "/d"} for t in targets]}


def test_a_jobs_report_stores_jobs_targets_and_schedules(backend, agent, reporter):
    resp = reporter.jobs([_job("j1", "Nightly", [("0 2 * * *", True), ("0 9 * * 1", False)], ["T"])])
    assert resp.status_code == 201
    assert resp.json() == {"success": True, "stored": 1}
    assert backend.jobs(agent) == {"j1": {"name": "Nightly", "active": True, "targets": {"T": "T"}}}
    assert backend.schedules(agent) == {"j1": [("0 2 * * *", True), ("0 9 * * 1", False)]}


def test_each_report_replaces_the_last_and_keeps_dropped_jobs_inactive(backend, agent, reporter):
    reporter.jobs([_job("j1", "A", targets=["T"]), _job("j2", "B", targets=["T"])])
    reporter.jobs([_job("j2", "B", targets=["T"])])
    assert sorted(backend.jobs(agent)) == ["j2"]
    assert sorted(backend.jobs(agent, active_only=False)) == ["j1", "j2"]


def test_a_job_keeps_its_row_when_its_name_changes(backend, agent, reporter):
    reporter.jobs([_job("j1", "Old name", targets=["T"])])
    reporter.jobs([_job("j1", "New name", targets=["T"])])
    assert backend.jobs(agent, active_only=False) == {"j1": {"name": "New name", "active": True, "targets": {"T": "T"}}}


def test_schedules_are_per_agent(backend, agent, other_agent, reporter, other_reporter):
    reporter.jobs([_job("J", "J", [("0 1 * * *", True)])])
    other_reporter.jobs([_job("J", "J", [("0 5 * * *", True)])])
    assert backend.schedules(agent) == {"J": [("0 1 * * *", True)]}
    assert backend.schedules(other_agent) == {"J": [("0 5 * * *", True)]}


def test_invalid_job_reports_are_rejected(reporter):
    assert reporter.jobs([{"job_name": "no id"}]).status_code == 400
    assert reporter.jobs([_job("j", "J", [("", True)])]).status_code == 400
