"""Per-(agent, job) rollup the dashboard's jobs table is built from, plus legacy health endpoint."""

from datetime import datetime, timedelta

import pytest

from tests.contract.conftest import new_run_id


def _run(reporter, job_name, target_id, outcome, job_run_id=None):
    """Report one finished run; outcome is a final status."""
    extra = {"job_name": job_name, "target_id": target_id}
    if job_run_id:
        extra["job_run_id"] = job_run_id
    run_id = reporter.start(**extra)
    event_type = "error" if outcome == "failed" else "backup_complete"
    reporter.complete(run_id, event_type=event_type, status=outcome, **extra)
    return run_id


def _rollup(backend, agent, job_name):
    (row,) = [r for r in backend.rollups(agent) if r["job_name"] == job_name]
    return row


def test_counts_statuses_and_reports_the_latest_runs_status(backend, agent, reporter):
    for outcome in ("success", "failed", "success"):
        _run(reporter, "J", "T", outcome)

    row = _rollup(backend, agent, "J")
    assert row["status_counts"] == {"success": 2, "failed": 1}
    assert row["last_status"] == "success"

    _run(reporter, "J", "T", "failed")
    assert _rollup(backend, agent, "J")["last_status"] == "failed"


def test_each_job_name_is_its_own_row(backend, agent, reporter):
    _run(reporter, "A", "T", "success")
    _run(reporter, "B", "T", "failed")
    names = sorted(r["job_name"] for r in backend.rollups(agent))
    assert names == ["A", "B"]


def test_agents_do_not_share_rollups(backend, agent, other_agent, reporter, other_reporter):
    _run(reporter, "J", "T", "success")
    _run(other_reporter, "J", "T", "failed")
    assert _rollup(backend, agent, "J")["status_counts"] == {"success": 1}
    assert _rollup(backend, other_agent, "J")["status_counts"] == {"failed": 1}


def test_targets_sharing_a_job_run_id_count_as_one_run_with_the_worst_status(
        backend, agent, reporter):
    shared = new_run_id()
    _run(reporter, "J", "T1", "success", job_run_id=shared)
    _run(reporter, "J", "T2", "failed", job_run_id=shared)

    row = _rollup(backend, agent, "J")
    assert row["last_status"] == "failed"
    assert row["status_counts"] == {"success": 1, "failed": 1}

    _run(reporter, "J", "T1", "success", job_run_id=new_run_id())
    assert _rollup(backend, agent, "J")["last_status"] == "success"


def test_targets_without_a_job_run_id_are_independent_runs(backend, agent, reporter):
    _run(reporter, "J", "T1", "failed")
    _run(reporter, "J", "T2", "success")
    assert _rollup(backend, agent, "J")["last_status"] == "success"


class TestNextEvent:
    def _job_with_schedule(self, backend, agent, reporter, cron):
        reporter.event(job_name="J", cron_schedule=cron)
        _run(reporter, "J", "T", "success")
        return _rollup(backend, agent, "J")

    def test_no_schedule_means_no_next_event(self, backend, agent, reporter):
        _run(reporter, "J", "T", "success")
        row = _rollup(backend, agent, "J")
        assert row["has_next_event"] is False
        assert row["schedule_drifted"] is False

    def test_every_minute_schedule_has_a_next_event_and_no_drift(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, "* * * * *")
        assert row["has_next_event"] is True
        assert row["schedule_drifted"] is False

    def test_last_run_far_from_any_scheduled_time_is_flagged_as_drift(
            self, backend, agent, reporter):
        far = datetime.now() + timedelta(hours=12)
        row = self._job_with_schedule(backend, agent, reporter, f"{far.minute} {far.hour} * * *")
        assert row["has_next_event"] is True
        assert row["schedule_drifted"] is True

    def test_invalid_schedule_yields_no_next_event(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, "not a cron")
        assert row["has_next_event"] is False

    def test_invalid_entries_in_a_list_are_skipped(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, "garbage, * * * * *")
        assert row["has_next_event"] is True


class TestLegacyHealthEndpoint:
    """Uptime Kuma and other monitors poll /api/heartbeat; it must keep working."""

    def test_reports_ok(self, backend):
        resp = backend.get("/api/heartbeat")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"]
        assert body["hostname"]
        assert isinstance(body["error_event_count"], int)

    @pytest.mark.v1_bug("v1 calls .cursor() on an unentered context manager, so the count is always 0")
    def test_error_event_count_grows_with_error_events(self, backend, reporter):
        before = backend.get("/api/heartbeat").json()["error_event_count"]
        run_id = reporter.start()
        reporter.complete(run_id, event_type="error", status="failed")
        assert backend.get("/api/heartbeat").json()["error_event_count"] == before + 1
