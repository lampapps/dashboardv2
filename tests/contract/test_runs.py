"""Run lifecycle: create, progress, finalize, and the quirks agents rely on."""

import time

import pytest

from tests.contract.conftest import new_run_id


def test_start_creates_a_running_run(backend, reporter):
    run_id = new_run_id()
    resp = reporter.event(
        event_type="heartbeat", message="Starting sync", stage="Starting sync",
        run_id=run_id, job_run_id="jr-1", job_name="J", backup_type="sync",
        target_id="T", target_label="T label", source="/src", destination="/dst",
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert isinstance(body["event_id"], int)
    assert isinstance(body["backup_job_id"], int)

    run = reporter.run(run_id)
    assert run["status"] == "running"
    assert (run["job_name"], run["backup_type"]) == ("J", "sync")
    assert (run["target_id"], run["target_label"]) == ("T", "T label")
    assert (run["source"], run["destination"]) == ("/src", "/dst")
    assert run["job_run_id"] == "jr-1"
    assert run["completed_at"] is None

    (event,) = backend.events(run_id)
    assert (event["event_type"], event["message"], event["stage"]) == (
        "heartbeat", "Starting sync", "Starting sync")


def test_same_run_id_reuses_the_run_and_keeps_creation_fields(backend, reporter):
    run_id = reporter.start(source="/first")
    resp = reporter.event(
        event_type="heartbeat", message="Next stage", stage="Stage 2", run_id=run_id,
        job_name="J", target_id="T", source="/second",
    )
    assert resp.status_code == 201

    run = reporter.run(run_id)
    assert resp.json()["backup_job_id"] == run["id"]
    assert run["source"] == "/first"
    assert [e["message"] for e in backend.events(run_id)] == ["Starting", "Next stage"]


def test_progress_fields_are_stored_without_finalizing(reporter):
    run_id = reporter.start()
    reporter.event(
        event_type="heartbeat", message="Backup in progress", run_id=run_id, job_name="J",
        target_id="T", percent_complete=42, bytes_per_second=1250.5, eta_seconds=210,
        current_item="/home/x", files_backed_up=10, bytes_backed_up=2048,
    )

    run = reporter.run(run_id)
    assert run["status"] == "running"
    assert run["completed_at"] is None
    assert run["percent_complete"] == 42
    assert run["bytes_per_second"] == 1250.5
    assert run["eta_seconds"] == 210
    assert run["current_item"] == "/home/x"
    assert run["files_count"] == 10
    assert run["bytes_processed"] == 2048


def test_partial_progress_leaves_other_fields_unchanged(reporter):
    run_id = reporter.start()
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat",
                   percent_complete=40, bytes_per_second=100, eta_seconds=60, current_item="a")
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat", percent_complete=50)

    run = reporter.run(run_id)
    assert run["percent_complete"] == 50
    assert run["bytes_per_second"] == 100
    assert run["eta_seconds"] == 60
    assert run["current_item"] == "a"


def test_success_completion_finalizes_and_clears_live_progress(backend, reporter):
    run_id = reporter.start()
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat",
                   percent_complete=80, bytes_per_second=5, eta_seconds=9, current_item="f")
    reporter.complete(
        run_id, message="Backup Complete", stage="Completed", status="success",
        duration_seconds=842.3, files_backed_up=128933, bytes_backed_up=55834574848,
        bytes_compressed=21474836480, external_id="snap-1",
    )

    run = reporter.run(run_id)
    assert run["status"] == "success"
    assert run["completed_at"] is not None
    assert run["runtime_seconds"] == 842.3
    assert run["files_count"] == 128933
    assert run["bytes_processed"] == 55834574848
    assert run["bytes_compressed"] == 21474836480
    assert run["external_id"] == "snap-1"
    assert run["percent_complete"] == 100
    assert run["bytes_per_second"] is None
    assert run["eta_seconds"] is None
    assert run["current_item"] is None
    assert backend.events(run_id)[-1]["event_type"] == "backup_complete"


def test_failure_keeps_last_known_percent_and_records_error(backend, reporter):
    run_id = reporter.start()
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat", percent_complete=63)
    reporter.complete(
        run_id, event_type="error", message="Backup failed: disk full", stage="Error",
        status="failed", error_code=7, error_message="disk full",
    )

    run = reporter.run(run_id)
    assert run["status"] == "failed"
    assert run["percent_complete"] == 63
    assert run["error_code"] == 7
    assert run["error_message"] == "disk full"
    final = backend.events(run_id)[-1]
    assert (final["event_type"], final["error_code"]) == ("error", 7)


def test_stopped_job_is_finalized_without_forcing_100_percent(reporter):
    run_id = reporter.start()
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat", percent_complete=30)
    reporter.complete(run_id, status="stopped", stage="Stopped")

    run = reporter.run(run_id)
    assert run["status"] == "stopped"
    assert run["completed_at"] is not None
    assert run["percent_complete"] == 30


@pytest.mark.parametrize("event_type,status,expected", [
    ("backup_complete", None, "success"),
    ("error", None, "failed"),
    ("backup_complete", "stopped", "stopped"),
    ("error", "failed", "failed"),
])
def test_completion_status_defaults(reporter, event_type, status, expected):
    run_id = reporter.start()
    extra = {} if status is None else {"status": status}
    reporter.complete(run_id, event_type=event_type, **extra)
    assert reporter.run(run_id)["status"] == expected


def test_completion_event_alone_creates_a_finished_run(reporter):
    run_id = new_run_id()
    reporter.complete(run_id, status="success", files_backed_up=3)

    run = reporter.run(run_id)
    assert run["status"] == "success"
    assert run["files_count"] == 3


def test_error_event_without_duration_does_not_finalize(backend, reporter):
    run_id = reporter.start()
    resp = reporter.event(
        event_type="error", message="Verify (quick) found 2 mismatched item(s)",
        stage="Verify (quick)", status="failed", error_message="2 mismatches",
        run_id=run_id, job_name="J", target_id="T",
    )

    assert resp.status_code == 201
    run = reporter.run(run_id)
    assert run["status"] == "running"
    assert run["completed_at"] is None
    assert backend.events(run_id)[-1]["event_type"] == "error"


def test_late_events_do_not_overwrite_a_finalized_outcome(reporter):
    run_id = reporter.start()
    reporter.complete(run_id, status="success", files_backed_up=5, duration_seconds=2.0)
    reporter.event(
        event_type="error", message="Verify failed", stage="Verify (quick)", status="failed",
        run_id=run_id, job_name="J", target_id="T",
    )
    reporter.event(
        event_type="heartbeat", message="Verify passed", stage="Verify (quick)", status="success",
        run_id=run_id, job_name="J", target_id="T",
    )

    run = reporter.run(run_id)
    assert run["status"] == "success"
    assert run["runtime_seconds"] == 2.0
    assert run["files_count"] == 5


def test_backup_type_upgrades_to_full_and_never_downgrades(reporter):
    run_id = reporter.start(backup_type="incremental")
    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat", backup_type="full")
    assert reporter.run(run_id)["backup_type"] == "full"

    reporter.event(run_id=run_id, target_id="T", event_type="heartbeat", backup_type="incremental")
    assert reporter.run(run_id)["backup_type"] == "full"


def test_events_without_run_id_each_create_a_run(backend, agent, reporter):
    # Documents current v1 behavior; agents always send run_id on job events.
    for message in ("one", "two"):
        resp = reporter.event(event_type="heartbeat", message=message, job_name="J", target_id="T")
        assert resp.status_code == 201
    assert len(backend.runs(agent)) == 2


def test_payload_timestamp_is_preserved_on_the_event(backend, reporter):
    run_id = reporter.start(timestamp=1_700_000_000)
    assert backend.events(run_id)[0]["timestamp"] == 1_700_000_000


def test_missing_timestamp_defaults_to_server_time(backend, reporter):
    before = time.time()
    run_id = reporter.start()
    after = time.time()
    assert before - 1 <= backend.events(run_id)[0]["timestamp"] <= after + 1


def test_unknown_fields_are_ignored(reporter):
    run_id = reporter.start(encrypt=True, sync=False, hostname="spoofed", ip_address="1.2.3.4")
    assert reporter.run(run_id)["status"] == "running"
