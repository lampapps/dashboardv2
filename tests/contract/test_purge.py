"""POST /api/monitoring/target-purged contract."""

import pytest


def _finished_run(reporter, target_id, external_id):
    run_id = reporter.start(target_id=target_id)
    reporter.complete(run_id, target_id=target_id, external_id=external_id)
    return run_id


def test_purge_marks_only_matching_runs_and_logs_events(backend, reporter):
    old = _finished_run(reporter, "T", "snap-old")
    keep = _finished_run(reporter, "T", "snap-keep")

    resp = reporter.purge(target_id="T", external_ids=["snap-old"], message="forget --prune")

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "purged_jobs": 1}
    assert reporter.run(old)["status"] == "purged"
    assert reporter.run(keep)["status"] == "success"
    purged_event = backend.events(old)[-1]
    assert (purged_event["event_type"], purged_event["message"]) == ("purged", "forget --prune")
    assert backend.events(keep)[-1]["event_type"] == "backup_complete"


def test_purge_message_has_a_default(backend, reporter):
    run_id = _finished_run(reporter, "T", "snap-1")
    reporter.purge(target_id="T", external_ids=["snap-1"])
    assert backend.events(run_id)[-1]["message"]


def test_purge_ignores_other_targets_with_the_same_external_id(reporter):
    first = _finished_run(reporter, "T1", "shared")
    second = _finished_run(reporter, "T2", "shared")
    reporter.purge(target_id="T1", external_ids=["shared"])
    assert reporter.run(first)["status"] == "purged"
    assert reporter.run(second)["status"] == "success"


def test_purge_is_scoped_to_the_calling_agent(reporter, other_reporter):
    mine = _finished_run(reporter, "T", "snap-1")
    theirs = _finished_run(other_reporter, "T", "snap-1")

    resp = reporter.purge(target_id="T", external_ids=["snap-1"])

    assert resp.json()["purged_jobs"] == 1
    assert reporter.run(mine)["status"] == "purged"
    assert other_reporter.run(theirs)["status"] == "success"


def test_purge_with_no_match_is_a_successful_noop(reporter):
    resp = reporter.purge(target_id="T", external_ids=["nothing"])
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "purged_jobs": 0}


def test_purge_accepts_non_string_external_ids(reporter):
    run_id = _finished_run(reporter, "T", "123")
    resp = reporter.purge(target_id="T", external_ids=[123])
    assert resp.json()["purged_jobs"] == 1
    assert reporter.run(run_id)["status"] == "purged"


@pytest.mark.parametrize("body", [
    {"external_ids": ["x"]},
    {"target_id": "T"},
    {"target_id": "T", "external_ids": []},
    {"target_id": "  ", "external_ids": ["x"]},
])
def test_purge_requires_target_and_external_ids(reporter, body):
    resp = reporter.purge(**body)
    assert resp.status_code == 400
    assert "error" in resp.json()
