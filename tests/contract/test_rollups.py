"""Per-(agent, job) rollup the dashboard's jobs table is built from, plus the health endpoint."""

from datetime import timedelta

from tests.contract.conftest import new_run_id


def _run(reporter, job_id, target_id, outcome, job_run_id=None):
    """Report one finished run; outcome is a final status."""
    extra = {"job_id": job_id, "job_name": job_id, "target_id": target_id}
    if job_run_id:
        extra["job_run_id"] = job_run_id
    run_id = reporter.start(**extra)
    event_type = "error" if outcome == "failed" else "backup_complete"
    reporter.complete(run_id, event_type=event_type, status=outcome, **extra)
    return run_id


def _rollup(backend, agent, job_id):
    (row,) = [r for r in backend.rollups(agent) if r["job_id"] == job_id]
    return row


def test_counts_statuses_and_reports_the_latest_runs_status(backend, agent, reporter):
    for outcome in ("success", "failed", "success"):
        _run(reporter, "J", "T", outcome)

    row = _rollup(backend, agent, "J")
    assert row["status_counts"] == {"success": 2, "failed": 1}
    assert row["last_status"] == "success"

    _run(reporter, "J", "T", "failed")
    assert _rollup(backend, agent, "J")["last_status"] == "failed"


def test_each_job_id_is_its_own_row(backend, agent, reporter):
    _run(reporter, "A", "T", "success")
    _run(reporter, "B", "T", "failed")
    ids = sorted(r["job_id"] for r in backend.rollups(agent))
    assert ids == ["A", "B"]


def test_renaming_a_job_does_not_split_its_rollup(backend, agent, reporter):
    run_id = reporter.start(job_id="J", job_name="Old")
    reporter.complete(run_id, job_id="J", job_name="Old")
    run_id = reporter.start(job_id="J", job_name="New")
    reporter.complete(run_id, job_id="J", job_name="New")
    row = _rollup(backend, agent, "J")
    assert row["job_name"] == "New" and row["status_counts"] == {"success": 2}


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
    def _job_with_schedule(self, backend, agent, reporter, *schedules):
        reporter.jobs([{"job_id": "J", "job_name": "J", "targets": [{"target_id": "T"}],
                        "schedules": [{"cron": c, "enabled": e} for c, e in schedules]}])
        _run(reporter, "J", "T", "success")
        return _rollup(backend, agent, "J")

    def test_no_schedule_means_no_next_event(self, backend, agent, reporter):
        _run(reporter, "J", "T", "success")
        row = _rollup(backend, agent, "J")
        assert row["has_next_event"] is False
        assert row["schedule_drifted"] is False

    def test_every_minute_schedule_has_a_next_event_and_no_drift(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, ("* * * * *", True))
        assert row["has_next_event"] is True
        assert row["schedule_drifted"] is False

    def test_a_disabled_schedule_gives_no_next_event(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, ("* * * * *", False))
        assert row["has_next_event"] is False

    def test_last_run_far_from_any_scheduled_time_is_flagged_as_drift(
            self, backend, agent, reporter):
        far = backend.now() + timedelta(hours=12)
        row = self._job_with_schedule(backend, agent, reporter, (f"{far.minute} {far.hour} * * *", True))
        assert row["has_next_event"] is True
        assert row["schedule_drifted"] is True

    def test_invalid_schedule_yields_no_next_event(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, ("not a cron", True))
        assert row["has_next_event"] is False

    def test_invalid_entries_in_a_list_are_skipped(self, backend, agent, reporter):
        row = self._job_with_schedule(backend, agent, reporter, ("garbage", True), ("* * * * *", True))
        assert row["has_next_event"] is True


class TestHealthEndpoint:
    """Uptime Kuma and other monitors poll /api/heartbeat; it must keep working."""

    def test_reports_ok(self, backend):
        resp = backend.get("/api/heartbeat")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"]
        assert body["hostname"]
        assert isinstance(body["error_event_count"], int)

    def test_error_event_count_grows_with_error_events(self, backend, reporter):
        before = backend.get("/api/heartbeat").json()["error_event_count"]
        run_id = reporter.start()
        reporter.complete(run_id, event_type="error", status="failed")
        assert backend.get("/api/heartbeat").json()["error_event_count"] == before + 1
