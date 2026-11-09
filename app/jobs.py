"""Per-(agent, job) rollups for the jobs table, ported from v1's /api/job_targets semantics."""

import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

# How far a job's last run may sit from a predicted cron time before the schedule is flagged as stale.
SCHEDULE_DRIFT_TOLERANCE_S = 300

# How many past runs each job row carries for the history dots.
RECENT_RUNS = 20

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


def _collect_runs(conn: sqlite3.Connection) -> dict:
    """Group run rows into runs: rows sharing a job_run_id are the targets of one invocation."""
    runs: dict = {}
    for row in conn.execute(
        "SELECT id, agent, job_name, job_run_id, status, started_at, completed_at, runtime_seconds, "
        "percent_complete, current_item, eta_seconds, error_message FROM runs ORDER BY started_at"
    ):
        job_key = (row["agent"], row["job_name"])
        run = runs.setdefault(
            (job_key, row["job_run_id"] or row["id"]),
            {"job_key": job_key, "started_at": row["started_at"], "counts": {}, "members": []},
        )
        run["started_at"] = min(run["started_at"], row["started_at"])
        run["counts"][row["status"]] = run["counts"].get(row["status"], 0) + 1
        run["members"].append(row)
    return runs


def _duration(run: dict) -> Optional[float]:
    """Seconds the run took, or None while any member is still running."""
    members = run["members"]
    if any(m["completed_at"] is None for m in members):
        return None
    if all(m["runtime_seconds"] is not None for m in members):
        return sum(m["runtime_seconds"] for m in members)  # targets of one invocation run in turn
    return max(m["completed_at"] for m in members) - run["started_at"]


def _progress(run: dict) -> Optional[dict]:
    running = [m for m in run["members"] if m["status"] == "running"]
    if not running:
        return None
    percents = [m["percent_complete"] for m in running if m["percent_complete"] is not None]
    etas = [m["eta_seconds"] for m in running if m["eta_seconds"] is not None]
    items = [m["current_item"] for m in running if m["current_item"]]
    return {
        "percent_complete": max(percents) if percents else None,
        "eta_seconds": max(etas) if etas else None,
        "current_item": items[-1] if items else None,
    }


def _error_message(run: dict) -> Optional[str]:
    for member in run["members"]:
        if member["status"] in ("failed", "error") and member["error_message"]:
            return member["error_message"][:300]
    return None


def job_rollups(conn: sqlite3.Connection, tz: ZoneInfo, now: float) -> list:
    """One row per (agent, job_name): latest run, recent history, status counts, next scheduled run.

    Runs that share a job_run_id (several targets in one script invocation) count as one run
    starting at the earliest member, with the most noteworthy member status.
    """
    jobs: dict = {}
    for run in sorted(_collect_runs(conn).values(), key=lambda r: r["started_at"]):
        status = representative_status(run["counts"])
        entry = jobs.setdefault(run["job_key"], {"status_counts": {}, "recent": []})
        entry["recent"].append({"status": status, "started_at": run["started_at"]})
        entry.update(
            last_started_at=run["started_at"], last_status=status,
            last_runtime_seconds=_duration(run), last_progress=_progress(run),
            last_error=_error_message(run),
        )
        for member_status, count in run["counts"].items():
            entry["status_counts"][member_status] = entry["status_counts"].get(member_status, 0) + count
    for entry in jobs.values():
        entry["recent"] = entry["recent"][-RECENT_RUNS:]

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
