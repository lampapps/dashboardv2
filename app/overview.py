"""Builds the Overview page's single JSON payload."""

import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from . import activity, jobs
from .agents import resolve_hosts
from .config import AgentConfig, Config
from .status import effective_status, gap_end

DAY_S = 86400
MAX_ACTIVITY_DAYS = 365
DEFAULT_ACTIVITY_DAYS = 30


def clamp_days(days: Optional[int]) -> int:
    if days is None:
        return DEFAULT_ACTIVITY_DAYS
    return max(1, min(MAX_ACTIVITY_DAYS, days))


def _finished_counts(conn: sqlite3.Connection, since: float) -> dict:
    """Finished runs since `since`, grouped into success/failed/stopped/other buckets."""
    counts = {b: 0 for b in activity.BUCKETS}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs WHERE completed_at >= ? GROUP BY status", (since,)
    ):
        counts[activity.bucket(row["status"])] += row["n"]
    return counts


def _agents(conn: sqlite3.Connection, configured: list, now: float, hosts: dict) -> list:
    states = {row["name"]: row for row in conn.execute(
        "SELECT name, status, last_heartbeat FROM agent_state")}
    gap = gap_end(conn)
    rows = []
    for a in sorted(configured, key=lambda a: a.name):
        state = states.get(a.name)
        heartbeat = state["last_heartbeat"] if state else None
        rows.append({"name": a.name, "host": hosts[a.name], "last_heartbeat": heartbeat,
                     "status": effective_status(a, state["status"] if state else None, heartbeat, now, gap)})
    return rows


def build(conn: sqlite3.Connection, cfg: Config, configured: list, now: float, days: int) -> dict:
    tz = ZoneInfo(cfg.timezone)
    hosts = resolve_hosts(conn, configured)
    rows = jobs.job_rollups(conn, tz, now, hosts)
    agents = _agents(conn, configured, now, hosts)

    week = _finished_counts(conn, now - 7 * DAY_S)
    week_ok = week["success"] + week["purged"]
    judged = week_ok + week["failed"]
    running = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]

    return {
        "now": now,
        "timezone": cfg.timezone,
        "stats": {
            "agents_total": sum(1 for a in agents if a["status"] != "disabled"),
            "agents_up": sum(1 for a in agents if a["status"] == "up"),
            "running": running,
            "success_7d": week_ok,
            "failed_7d": week["failed"],
            "jobs_total": len(rows),
            "jobs_failing": sum(1 for r in rows if activity.bucket(r["last_status"]) == "failed"),
            "success_rate_7d": round(week_ok / judged, 4) if judged else None,
        },
        "agents": agents,
        "activity": activity.activity(conn, tz, now, days, cfg.retention.runs_days),
        "jobs": rows,
    }
