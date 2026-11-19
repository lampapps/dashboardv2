"""POST /api/v2/purge contract."""

import pytest


def _finished_run(reporter, target_id, external_id, job_id="J"):
    run_id = reporter.start(target_id=target_id, job_id=job_id)
    reporter.complete(run_id, target_id=target_id, external_id=external_id, job_id=job_id)
    return run_id


def test_purge_marks_only_matching_runs_and_logs_events(backend, reporter):
    old = _finished_run(reporter, "T", "snap-old")
    keep = _finished_run(reporter, "T", "snap-keep")

    resp = reporter.purge(job_id="J", target_id="T", external_ids=["snap-old"], message="forget --prune")

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "purged_runs": 1}
    assert reporter.run(old)["status"] == "purged"
    assert reporter.run(keep)["status"] == "success"
    purged_event = backend.events(old)[-1]
    assert (purged_event["event_type"], purged_event["message"]) == ("purged", "forget --prune")
    assert backend.events(keep)[-1]["event_type"] == "backup_complete"


def test_purge_message_has_a_default(backend, reporter):
    run_id = _finished_run(reporter, "T", "snap-1")
    reporter.purge(job_id="J", target_id="T", external_ids=["snap-1"])
    assert backend.events(run_id)[-1]["message"]


def test_purge_ignores_other_targets_with_the_same_external_id(reporter):
    first = _finished_run(reporter, "T1", "shared")
    second = _finished_run(reporter, "T2", "shared")
    reporter.purge(job_id="J", target_id="T1", external_ids=["shared"])
    assert reporter.run(first)["status"] == "purged"
    assert reporter.run(second)["status"] == "success"


def test_purge_ignores_other_jobs_with_the_same_target_and_external_id(reporter):
    first = _finished_run(reporter, "T", "shared", job_id="J1")
    second = _finished_run(reporter, "T", "shared", job_id="J2")
    reporter.purge(job_id="J1", target_id="T", external_ids=["shared"])
    assert reporter.run(first)["status"] == "purged"
    assert reporter.run(second)["status"] == "success"


def test_purge_is_scoped_to_the_calling_agent(reporter, other_reporter):
    mine = _finished_run(reporter, "T", "snap-1")
    theirs = _finished_run(other_reporter, "T", "snap-1")

    resp = reporter.purge(job_id="J", target_id="T", external_ids=["snap-1"])

    assert resp.json()["purged_runs"] == 1
    assert reporter.run(mine)["status"] == "purged"
    assert other_reporter.run(theirs)["status"] == "success"


def test_purge_with_no_match_is_a_successful_noop(reporter):
    resp = reporter.purge(job_id="J", target_id="T", external_ids=["nothing"])
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "purged_runs": 0}


def test_purge_accepts_non_string_external_ids(reporter):
    run_id = _finished_run(reporter, "T", "123")
    resp = reporter.purge(job_id="J", target_id="T", external_ids=[123])
    assert resp.json()["purged_runs"] == 1
    assert reporter.run(run_id)["status"] == "purged"


@pytest.mark.parametrize("body", [
    {"target_id": "T", "external_ids": ["x"]},
    {"job_id": "J", "external_ids": ["x"]},
    {"job_id": "J", "target_id": "T"},
    {"job_id": "J", "target_id": "T", "external_ids": []},
    {"job_id": "J", "target_id": "  ", "external_ids": ["x"]},
])
def test_purge_requires_job_target_and_external_ids(reporter, body):
    resp = reporter.purge(**body)
    assert resp.status_code == 400
    assert "error" in resp.json()
