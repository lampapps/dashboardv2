"""Per-(agent, job) rollups for the jobs table, ported from v1's /api/job_targets semantics."""

import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

# How far a job's last run may sit from a predicted cron time before the schedule is flagged as stale.
SCHEDULE_DRIFT_TOLERANCE_S = 300

# Precedence (highest first) for one representative status when a run covers several targets.
STATUS_PRIORITY = ["error", "failed", "stopped", "running", "skipped", "purged", "success", "completed"]


def representative_status(status_counts: dict) -> str:
    for candidate in STATUS_PRIORITY:
        if status_counts.get(candidate):
            return candidate
    return next(iter(status_counts), "unknown")


def _expressions(cron_schedule: Optional[str]) -> list:
    return [expr.strip() for expr in (cron_schedule or "").split(",") if expr.strip()]


def next_event(cron_schedule: Optional[str], last_started_at: Optional[float], tz: ZoneInfo,
               now: float) -> tuple:
    """Return (next_run_epoch or None, drifted) for a comma-separated cron schedule.

    drifted means the last run was not near any predicted occurrence, so the reported
    schedule is probably stale. Invalid expressions are skipped.
    """
    now_dt = datetime.fromtimestamp(now, tz)
    next_runs, valid = [], []
    for expr in _expressions(cron_schedule):
        try:
            next_runs.append(croniter(expr, now_dt).get_next(datetime))
            valid.append(expr)
        except (ValueError, KeyError):
            continue
    if not next_runs:
        return None, False

    drifted = False
    if last_started_at:
        last_dt = datetime.fromtimestamp(last_started_at, tz)
        drifted = True
        for expr in valid:
            previous = croniter(expr, last_dt).get_prev(datetime)
            if abs((last_dt - previous).total_seconds()) <= SCHEDULE_DRIFT_TOLERANCE_S:
                drifted = False
                break
    return min(next_runs).timestamp(), drifted


def job_rollups(conn: sqlite3.Connection, tz: ZoneInfo, now: float) -> list:
    """One row per (agent, job_name): latest run status and time, status counts, next scheduled run.

    Runs that share a job_run_id (several targets in one script invocation) count as one run
    starting at the earliest member, with the most noteworthy member status.
    """
    runs: dict = {}
    for row in conn.execute(
        "SELECT id, agent, job_name, job_run_id, status, started_at FROM runs ORDER BY started_at"
    ):
        job_key = (row["agent"], row["job_name"])
        run = runs.setdefault(
            (job_key, row["job_run_id"] or row["id"]),
            {"job_key": job_key, "started_at": row["started_at"], "counts": {}},
        )
        run["started_at"] = min(run["started_at"], row["started_at"])
        run["counts"][row["status"]] = run["counts"].get(row["status"], 0) + 1

    jobs: dict = {}
    for run in runs.values():
        entry = jobs.setdefault(run["job_key"], {"last_started_at": None, "last_status": None, "status_counts": {}})
        if entry["last_started_at"] is None or run["started_at"] > entry["last_started_at"]:
            entry["last_started_at"] = run["started_at"]
            entry["last_status"] = representative_status(run["counts"])
        for status, count in run["counts"].items():
            entry["status_counts"][status] = entry["status_counts"].get(status, 0) + count

    schedules = {
        (row["agent"], row["job_name"]): row["cron_schedule"]
        for row in conn.execute("SELECT agent, job_name, cron_schedule FROM job_schedules")
    }
    rows = []
    for (agent, job_name), entry in jobs.items():
        next_at, drifted = next_event(schedules.get((agent, job_name)), entry["last_started_at"], tz, now)
        rows.append({"agent": agent, "job_name": job_name, **entry,
                     "next_event_at": next_at, "schedule_drifted": drifted})
    rows.sort(key=lambda r: r["last_started_at"], reverse=True)
    return rows
