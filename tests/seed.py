"""Helpers that write agents, jobs, targets and runs straight into a test database.

`agent` is both the id and the name of the agent row unless `name` is given; `job` is the job key and, unless
`job_name` is given, its name; `target` is the target key and `label` its label (default: the key).
"""

import sqlite3
import uuid
from typing import Optional

NOW = 1_800_000_000.0


def ensure_agent(conn: sqlite3.Connection, agent: str, name: Optional[str] = None, **columns) -> str:
    conn.execute("INSERT OR IGNORE INTO agents (id, name, updated_at) VALUES (?, ?, ?)", (agent, name or agent, NOW))
    if columns:
        sets = ", ".join(f"{key} = ?" for key in columns)
        conn.execute(f"UPDATE agents SET {sets} WHERE id = ?", (*columns.values(), agent))
    return agent


def ensure_job(conn: sqlite3.Connection, agent: str, job: str, job_name: Optional[str] = None, **columns) -> int:
    ensure_agent(conn, agent)
    conn.execute("INSERT OR IGNORE INTO jobs (agent_id, job_key, name, updated_at) VALUES (?, ?, ?, ?)",
                 (agent, job, job if job_name is None else job_name, NOW))
    row = conn.execute("SELECT id FROM jobs WHERE agent_id = ? AND job_key = ?", (agent, job)).fetchone()
    if columns:
        sets = ", ".join(f"{key} = ?" for key in columns)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*columns.values(), row["id"]))
    return row["id"]


def ensure_target(conn: sqlite3.Connection, job_pk: int, target: str, label: Optional[str] = None, **columns) -> int:
    conn.execute("INSERT OR IGNORE INTO targets (job_id, target_key, label, updated_at) VALUES (?, ?, ?, ?)",
                 (job_pk, target, target if label is None else label, NOW))
    row = conn.execute("SELECT id FROM targets WHERE job_id = ? AND target_key = ?", (job_pk, target)).fetchone()
    if columns:
        sets = ", ".join(f"{key} = ?" for key in columns)
        conn.execute(f"UPDATE targets SET {sets} WHERE id = ?", (*columns.values(), row["id"]))
    return row["id"]


def add_schedule(conn: sqlite3.Connection, agent: str, job: str, cron: str, enabled: bool = True) -> None:
    conn.execute("INSERT OR REPLACE INTO job_schedules (job_id, cron, enabled) VALUES (?, ?, ?)",
                 (ensure_job(conn, agent, job), cron, int(enabled)))


def add_run(conn: sqlite3.Connection, agent: str = "a", job: str = "J", status: str = "success",
            started: float = NOW - 3600, runtime: Optional[float] = 10.0, target: str = "T",
            label: Optional[str] = None, bytes_: Optional[int] = 1000, files: Optional[int] = 5,
            error: Optional[str] = None, percent: Optional[int] = None, events=(), job_name: Optional[str] = None,
            run_id: Optional[str] = None, job_run_id: Optional[str] = None, external_id: Optional[str] = None,
            backup_type: str = "", source: str = "", destination: str = "", agent_name: Optional[str] = None) -> int:
    """One run (and its events) for an agent's job target; the agent, job and target rows are created as needed."""
    ensure_agent(conn, agent, agent_name)
    target_pk = ensure_target(conn, ensure_job(conn, agent, job, job_name), target, label)
    finished = status != "running"
    cursor = conn.execute(
        "INSERT INTO runs (target_pk, run_id, job_run_id, backup_type, source, destination, status, started_at, "
        "completed_at, runtime_seconds, files_count, bytes_processed, error_message, percent_complete, external_id, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target_pk, run_id, job_run_id, backup_type, source, destination, status, started,
         started + (runtime or 0) if finished else None, runtime if finished else None, files, bytes_, error, percent,
         external_id, started))
    run_pk = cursor.lastrowid
    for i, (event_type, message) in enumerate(events):
        conn.execute("INSERT INTO events (run_pk, event_type, message, timestamp, created_at) VALUES (?, ?, ?, ?, ?)",
                     (run_pk, event_type, message, started + i, started + i))
    return run_pk


def aid(name: str) -> str:
    """A stable, well-formed agent id for a test agent called `name`."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
