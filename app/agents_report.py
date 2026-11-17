"""Builds the Agents pages' payloads: status, up/down history and read-only configuration per agent."""

import json
import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from . import activity, jobs, overview, storage
from .agents import host_source, resolve_hosts
from .config import AgentConfig, Config
from .status import effective_status, evaluate, gap_end, timeline, uptime

# Agents needing attention come first.
_ATTENTION = {"down": 0, "unknown": 1, "new": 2, "up": 3, "disabled": 4}


class _Context:
    """Lookups shared by every agent card, read once per request."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, now: float, hosts: dict):
        self.conn, self.cfg, self.now, self.hosts = conn, cfg, now, hosts
        self.gap = gap_end(conn)
        self.states = {row["name"]: row for row in conn.execute(
            "SELECT name, version, agent_type, hostname, last_heartbeat, status, status_since FROM agent_state")}
        self.jobs_by_agent: dict = {}
        for row in jobs.job_rollups(conn, ZoneInfo(cfg.timezone), now, hosts):
            self.jobs_by_agent.setdefault(row["agent"], []).append(row)
        self.schedules: dict = {}
        for row in conn.execute("SELECT agent, job_name, cron_schedule FROM job_schedules ORDER BY job_name"):
            self.schedules.setdefault(row["agent"], []).append(
                {"job_name": row["job_name"], "cron_schedule": row["cron_schedule"]})
        self.targets: dict = {}
        for row in conn.execute("SELECT agent, job_name, target, source, destination FROM job_targets "
                                "ORDER BY job_name, target"):
            self.targets.setdefault(row["agent"], {}).setdefault(row["job_name"], []).append(
                {"name": row["target"], "source": row["source"], "destination": row["destination"]})
        self.reported: dict = {}
        for row in conn.execute("SELECT agent, job_name, schedules FROM job_config"):
            self.reported.setdefault(row["agent"], {})[row["job_name"]] = json.loads(row["schedules"])

    def jobs_config(self, agent: str) -> list:
        """Every job the agent has told us about: what it copies and its schedules.

        `schedules` is the agent's own list with each one's enabled state, or None when the agent does not
        report it (an older one); `cron_schedule` is what its heartbeats say, which covers enabled ones only.
        """
        schedules = {s["job_name"]: s["cron_schedule"] for s in self.schedules.get(agent, [])}
        targets = self.targets.get(agent, {})
        reported = self.reported.get(agent, {})
        names = sorted(set(schedules) | set(targets) | set(reported), key=lambda name: (name.casefold(), name))
        return [{"job_name": name, "cron_schedule": schedules.get(name), "schedules": reported.get(name),
                 "targets": targets.get(name, [])} for name in names]


def _card(ctx: _Context, agent: AgentConfig, start: float) -> dict:
    state = ctx.states.get(agent.name)
    stored = state["status"] if state else None
    heartbeat = state["last_heartbeat"] if state else None
    pending = evaluate(agent, stored, heartbeat, ctx.now, ctx.gap)
    current = effective_status(agent, stored, heartbeat, ctx.now, ctx.gap)
    spans = timeline(ctx.conn, agent.name, start, ctx.now, pending)
    agent_jobs = ctx.jobs_by_agent.get(agent.name, [])
    latest = max(agent_jobs, key=lambda j: j["last_started_at"], default=None)
    return {
        "name": agent.name,
        "host": ctx.hosts.get(agent.name, agent.name),
        "host_source": host_source(state["hostname"] if state else None, agent),
        "status": current,
        "since": pending[1] if pending else (state["status_since"] if state and current == stored else None),
        "last_heartbeat": heartbeat,
        "version": state["version"] if state else None,
        "agent_type": state["agent_type"] if state else None,
        "config": {
            "enabled": agent.enabled, "host": agent.host, "grace_minutes": agent.grace_minutes, "notes": agent.notes,
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
        "schedules": ctx.schedules.get(agent.name, []),
        "jobs_config": ctx.jobs_config(agent.name),
    }


def build(conn: sqlite3.Connection, cfg: Config, configured: list, now: float, range_key: str) -> dict:
    key, span = storage.range_seconds(range_key)
    start = now - span
    ctx = _Context(conn, cfg, now, resolve_hosts(conn, configured))
    agents = [_card(ctx, agent, start) for agent in configured]
    agents.sort(key=lambda a: (_ATTENTION[a["status"]], a["name"]))
    return {"now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now, "agents": agents}


def _totals(conn: sqlite3.Connection, agent: str, since: float) -> dict:
    success = activity.SUCCESSFUL_STATUSES
    row = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(files_count) AS files, SUM(bytes_processed) AS bytes, "
        f"AVG(CASE WHEN status IN ({', '.join('?' * len(success))}) THEN runtime_seconds END) AS avg_runtime "
        "FROM runs WHERE agent = ? AND started_at >= ?", (*success, agent, since)).fetchone()
    return {"files": row["files"] or 0, "bytes": row["bytes"] or 0, "avg_runtime_seconds": row["avg_runtime"]}


def detail(conn: sqlite3.Connection, cfg: Config, agent: AgentConfig, now: float, range_key: str) -> dict:
    """One agent's card plus its activity chart, totals and job rows."""
    key, span = storage.range_seconds(range_key)
    days = overview.clamp_days(span // 86400)
    start = now - span
    ctx = _Context(conn, cfg, now, resolve_hosts(conn, [agent]))
    card = _card(ctx, agent, start)
    tz = ZoneInfo(cfg.timezone)
    chart = activity.activity(conn, tz, now, days, cfg.retention.runs_days, agent=agent.name)
    counts = {b: sum(values) for b, values in chart["series"].items()}
    running = conn.execute("SELECT COUNT(*) FROM runs WHERE agent = ? AND status = 'running'", (agent.name,)).fetchone()[0]
    # Files and sizes exist only for runs still retained, so they cover at most runs_days.
    raw_days = min(days, cfg.retention.runs_days)
    return {
        **card, "now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now,
        "activity": chart,
        "counts": {**counts, "runs_now": running, "total": sum(counts.values())},
        "totals": {**_totals(conn, agent.name, now - raw_days * 86400), "days": raw_days},
        "job_rows": ctx.jobs_by_agent.get(agent.name, []),
    }
