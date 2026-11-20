"""Run history for one job: summary, a page of runs, the duration trend, and one run with its events."""

import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from . import activity, jobs
from .config import Config

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25
TREND_RUNS = 60
MAX_EVENTS = 500

_KNOWN_STATUSES = tuple(s for statuses in activity.BUCKET_STATUSES.values() for s in statuses)

def _status_filter(bucket: Optional[str]) -> tuple:
    """SQL fragment and params restricting runs to one display bucket."""
    if bucket in activity.BUCKET_STATUSES:
        statuses = activity.BUCKET_STATUSES[bucket]
        return f"status IN ({', '.join('?' * len(statuses))})", list(statuses)
    if bucket == "other":
        return f"status NOT IN ({', '.join('?' * len(_KNOWN_STATUSES))})", list(_KNOWN_STATUSES)
    return "", []


def list_runs(conn: sqlite3.Connection, agent_id: str, job_key: str, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0,
              status: Optional[str] = None, target: Optional[str] = None, since: Optional[float] = None) -> dict:
    """One page of the job's runs, newest first, plus the total matching the filters."""
    limit = max(1, min(MAX_PAGE_SIZE, limit))
    offset = max(0, offset)
    where, params = ["agent_id = ?", "job_key = ?"], [agent_id, job_key]
    fragment, status_params = _status_filter(status)
    if fragment:
        where.append(fragment)
        params += status_params
    if target:
        where.append("target_id = ?")
        params.append(target)
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) FROM run_view WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT id, target_id, target_label, backup_type, status, started_at, completed_at, runtime_seconds, "
        "files_count, bytes_processed, bytes_compressed, percent_complete, current_item, error_message, "
        # A finished run reports its closing message; a running one its latest.
        "COALESCE((SELECT message FROM events WHERE run_pk = run_view.id AND event_type IN ('backup_complete', 'error') "
        "ORDER BY id DESC LIMIT 1), (SELECT message FROM events WHERE run_pk = run_view.id ORDER BY id DESC LIMIT 1)) "
        f"AS message FROM run_view WHERE {clause} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "runs": [dict(r) for r in rows]}


def trend(conn: sqlite3.Connection, agent_id: str, job_key: str, limit: int = TREND_RUNS) -> list:
    """Duration and size of the most recent finished runs, oldest first."""
    rows = conn.execute(
        "SELECT started_at, status, runtime_seconds, bytes_processed, target_id, target_label FROM run_view "
        "WHERE agent_id = ? AND job_key = ? "
        "AND runtime_seconds IS NOT NULL ORDER BY started_at DESC LIMIT ?", (agent_id, job_key, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def job_summary(conn: sqlite3.Connection, cfg: Config, now: float, agent, job_key: str,
                hosts: Optional[dict] = None) -> Optional[dict]:
    """The job's rollup row plus its targets, averages and trend; None if it has no runs. `agent` is its AgentConfig."""
    rollup = next((r for r in jobs.job_rollups(conn, ZoneInfo(cfg.timezone), now, hosts)
                   if r["agent_id"] == agent.id and r["job_key"] == job_key), None)
    if rollup is None:
        return None
    success = activity.SUCCESSFUL_STATUSES
    stats = conn.execute(
        f"SELECT AVG(CASE WHEN status IN ({', '.join('?' * len(success))}) THEN runtime_seconds END) AS avg_runtime, "
        "SUM(bytes_processed) AS bytes FROM run_view WHERE agent_id = ? AND job_key = ?",
        (*success, agent.id, job_key)).fetchone()
    targets = [dict(r) for r in conn.execute(
        "SELECT target_id, MAX(target_label) AS target_label, COUNT(*) AS runs FROM run_view "
        "WHERE agent_id = ? AND job_key = ? GROUP BY target_id ORDER BY target_id", (agent.id, job_key))]
    schedules = [{"cron": r["cron"], "enabled": bool(r["enabled"])} for r in conn.execute(
        "SELECT s.cron, s.enabled FROM job_schedules s JOIN jobs j ON j.id = s.job_id "
        "WHERE j.agent_id = ? AND j.job_key = ? ORDER BY s.cron", (agent.id, job_key))]
    return {
        "agent_id": agent.id, "agent": agent.name, "host": rollup["host"], "job_key": job_key,
        "job_name": rollup["job_name"], "rollup": rollup, "schedules": schedules,
        "targets": targets,
        "avg_runtime_seconds": stats["avg_runtime"], "bytes_processed": stats["bytes"],
        "trend": trend(conn, agent.id, job_key),
        "timezone": cfg.timezone, "now": now,
    }


def run_detail(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
    run = conn.execute(
        "SELECT id, agent_id, run_id, job_run_id, job_key, job_name, target_id, target_label, backup_type, source, "
        "destination, status, started_at, completed_at, runtime_seconds, files_count, bytes_processed, "
        "bytes_compressed, percent_complete, bytes_per_second, eta_seconds, current_item, error_code, error_message, "
        "external_id FROM run_view WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    events = [dict(e) for e in conn.execute(
        "SELECT id, event_type, message, stage, error_code, timestamp FROM events WHERE run_pk = ? "
        "ORDER BY timestamp, id LIMIT ?", (run_id, MAX_EVENTS + 1))]
    return {"run": dict(run), "events": events[:MAX_EVENTS], "events_truncated": len(events) > MAX_EVENTS}
