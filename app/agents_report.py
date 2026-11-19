"""Builds the Agents pages' payloads: status, up/down history and read-only configuration per agent."""

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
        self.states = {row["id"]: row for row in conn.execute(
            "SELECT id, version, agent_type, hostname, last_heartbeat, status, status_since FROM agents")}
        self.jobs_by_agent: dict = {}
        for row in jobs.job_rollups(conn, ZoneInfo(cfg.timezone), now, hosts):
            self.jobs_by_agent.setdefault(row["agent_id"], []).append(row)
        self._config: dict = {}
        by_pk: dict = {}
        for row in conn.execute("SELECT id, agent_id, job_key, name FROM jobs "
                                "WHERE active = 1 AND reported_at IS NOT NULL"):
            by_pk[row["id"]] = {"job_key": row["job_key"], "job_name": row["name"], "schedules": [], "targets": []}
            self._config.setdefault(row["agent_id"], []).append(by_pk[row["id"]])
        for row in conn.execute("SELECT job_id, cron, enabled FROM job_schedules ORDER BY cron"):
            if row["job_id"] in by_pk:
                by_pk[row["job_id"]]["schedules"].append({"cron": row["cron"], "enabled": bool(row["enabled"])})
        for row in conn.execute("SELECT job_id, target_key, label, source, destination FROM targets "
                                "WHERE active = 1 ORDER BY label, target_key"):
            if row["job_id"] in by_pk:
                by_pk[row["job_id"]]["targets"].append({
                    "target_id": row["target_key"], "label": row["label"], "source": row["source"],
                    "destination": row["destination"]})

    def jobs_config(self, agent_id: str) -> list:
        """The jobs the agent last reported (still configured there): schedules with their enabled state, targets."""
        return sorted(self._config.get(agent_id, []), key=lambda j: (j["job_name"].casefold(), j["job_key"]))


def _card(ctx: _Context, agent: AgentConfig, start: float) -> dict:
    state = ctx.states.get(agent.id)
    stored = state["status"] if state else None
    heartbeat = state["last_heartbeat"] if state else None
    pending = evaluate(agent, stored, heartbeat, ctx.now, ctx.gap)
    current = effective_status(agent, stored, heartbeat, ctx.now, ctx.gap)
    spans = timeline(ctx.conn, agent.id, start, ctx.now, pending)
    agent_jobs = ctx.jobs_by_agent.get(agent.id, [])
    latest = max(agent_jobs, key=lambda j: j["last_started_at"], default=None)
    return {
        "id": agent.id,
        "name": agent.name,
        "host": ctx.hosts.get(agent.id, agent.name),
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
            "job_key": latest["job_key"], "job_name": latest["job_name"], "status": latest["last_status"],
            "started_at": latest["last_started_at"]},
        "jobs_config": ctx.jobs_config(agent.id),
    }


def build(conn: sqlite3.Connection, cfg: Config, configured: list, now: float, range_key: str) -> dict:
    key, span = storage.range_seconds(range_key)
    start = now - span
    ctx = _Context(conn, cfg, now, resolve_hosts(conn, configured))
    agents = [_card(ctx, agent, start) for agent in configured]
    agents.sort(key=lambda a: (_ATTENTION[a["status"]], a["name"]))
    return {"now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now, "agents": agents}


def _totals(conn: sqlite3.Connection, agent_id: str, since: float) -> dict:
    success = activity.SUCCESSFUL_STATUSES
    row = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(files_count) AS files, SUM(bytes_processed) AS bytes, "
        f"AVG(CASE WHEN status IN ({', '.join('?' * len(success))}) THEN runtime_seconds END) AS avg_runtime "
        "FROM run_view WHERE agent_id = ? AND started_at >= ?", (*success, agent_id, since)).fetchone()
    return {"files": row["files"] or 0, "bytes": row["bytes"] or 0, "avg_runtime_seconds": row["avg_runtime"]}


def detail(conn: sqlite3.Connection, cfg: Config, agent: AgentConfig, now: float, range_key: str) -> dict:
    """One agent's card plus its activity chart, totals and job rows."""
    key, span = storage.range_seconds(range_key)
    days = overview.clamp_days(span // 86400)
    start = now - span
    ctx = _Context(conn, cfg, now, resolve_hosts(conn, [agent]))
    card = _card(ctx, agent, start)
    tz = ZoneInfo(cfg.timezone)
    chart = activity.activity(conn, tz, now, days, cfg.retention.runs_days, agent_id=agent.id)
    counts = {b: sum(values) for b, values in chart["series"].items()}
    running = conn.execute("SELECT COUNT(*) FROM run_view WHERE agent_id = ? AND status = 'running'",
                           (agent.id,)).fetchone()[0]
    # Files and sizes exist only for runs still retained, so they cover at most runs_days.
    raw_days = min(days, cfg.retention.runs_days)
    return {
        **card, "now": now, "timezone": cfg.timezone, "range": key, "start": start, "end": now,
        "activity": chart,
        "counts": {**counts, "runs_now": running, "total": sum(counts.values())},
        "totals": {**_totals(conn, agent.id, now - raw_days * 86400), "days": raw_days},
        "job_rows": ctx.jobs_by_agent.get(agent.id, []),
    }
