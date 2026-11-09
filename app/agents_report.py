"""Builds the Agents page payload: status, up/down history and read-only configuration per agent."""

import sqlite3
from zoneinfo import ZoneInfo

from . import activity, jobs, storage
from .config import Config
from .status import effective_status, evaluate, gap_end, timeline, uptime

# Agents needing attention come first.
_ATTENTION = {"down": 0, "unknown": 1, "new": 2, "up": 3, "disabled": 4}


def build(conn: sqlite3.Connection, cfg: Config, configured: list, now: float, range_key: str) -> dict:
    key, span = storage.range_seconds(range_key)
    start = now - span
    gap = gap_end(conn)
    states = {row["name"]: row for row in conn.execute(
        "SELECT name, version, agent_type, last_heartbeat, status, status_since FROM agent_state")}

    jobs_by_agent: dict = {}
    for row in jobs.job_rollups(conn, ZoneInfo(cfg.timezone), now):
        jobs_by_agent.setdefault(row["agent"], []).append(row)
    schedules: dict = {}
    for row in conn.execute("SELECT agent, job_name, cron_schedule FROM job_schedules ORDER BY job_name"):
        schedules.setdefault(row["agent"], []).append(
            {"job_name": row["job_name"], "cron_schedule": row["cron_schedule"]})

    agents = []
    for agent in configured:
        state = states.get(agent.name)
        stored = state["status"] if state else None
        heartbeat = state["last_heartbeat"] if state else None
        pending = evaluate(agent, stored, heartbeat, now, gap)
        current = effective_status(agent, stored, heartbeat, now, gap)
        spans = timeline(conn, agent.name, start, now, pending)
        agent_jobs = jobs_by_agent.get(agent.name, [])
        latest = max(agent_jobs, key=lambda j: j["last_started_at"], default=None)
        agents.append({
            "name": agent.name,
            "status": current,
            "since": pending[1] if pending else (state["status_since"] if state and current == stored else None),
            "last_heartbeat": heartbeat,
            "version": state["version"] if state else None,
            "agent_type": state["agent_type"] if state else None,
            "config": {
                "enabled": agent.enabled, "grace_minutes": agent.grace_minutes, "notes": agent.notes,
                "allowed_ip": agent.allowed_ip, "offline_alerts": agent.offline_alerts,
            },
            "uptime": uptime(spans),
            "spans": spans,
            "jobs": {
                "total": len(agent_jobs),
                "failing": sum(1 for j in agent_jobs if activity.bucket(j["last_status"]) == "failed"),
            },
            "last_run": None if latest is None else {
                "job_name": latest["job_name"], "status": latest["last_status"], "started_at": latest["last_started_at"]},
            "schedules": schedules.get(agent.name, []),
        })
    agents.sort(key=lambda a: (_ATTENTION[a["status"]], a["name"]))
    return {"now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now, "agents": agents}
