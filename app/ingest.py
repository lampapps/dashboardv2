"""Agent ingest: validate the /api/v2 payloads and apply them to the database.

An agent is identified by its registry id, a job by the `job_id` the agent sends and a target by its `target_id`
within that job. Names and labels are display text: when one changes, the row is renamed and history stays.
"""

import sqlite3
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db, status as agent_status
from .config import clean_hostname

Number = Optional[Union[int, float]]

# Agent-supplied text is untrusted: long values are truncated, never rejected.
_STRING_LIMITS = {
    "version": 100, "agent_type": 100, "event_type": 50, "status": 50,
    "message": 2000, "stage": 200, "error_message": 2000,
    "run_id": 200, "job_run_id": 200, "job_id": 200, "target_id": 200, "target_label": 500,
    "job_name": 200, "backup_type": 50, "external_id": 500,
    "source": 1000, "destination": 1000, "current_item": 1000, "cron": 100,
}
_KEYS = ("job_id", "target_id")   # required on an event: an empty one is a validation error, not a blank

DEFAULT_PURGE_MESSAGE = "Job target artifact(s) purged by agent"

MAX_JOBS = 200
MAX_TARGETS_PER_JOB = 200
MAX_SCHEDULES_PER_JOB = 20


def _clean(value: Any, limit: int) -> Any:
    return value.strip()[:limit] if isinstance(value, str) else value


class JobTarget(BaseModel):
    """One source and destination a job copies: a sync pair, or a snapshot job's single target."""

    model_config = ConfigDict(extra="ignore")

    target_id: str = Field(min_length=1)
    target_label: str = ""
    source: str = ""
    destination: str = ""

    @field_validator("target_id", "target_label", "source", "destination", mode="before")
    @classmethod
    def _clean_text(cls, value: Any, info) -> Any:
        cleaned = _clean(value, _STRING_LIMITS[info.field_name])
        return "" if cleaned is None and info.field_name != "target_id" else cleaned


class JobSchedule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cron: str = Field(min_length=1)
    enabled: bool = False

    @field_validator("cron", mode="before")
    @classmethod
    def _clean_cron(cls, value: Any) -> Any:
        return _clean(value, _STRING_LIMITS["cron"])


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(min_length=1)
    job_name: str = ""
    targets: list[JobTarget] = Field(default_factory=list, max_length=MAX_TARGETS_PER_JOB)
    schedules: list[JobSchedule] = Field(default_factory=list, max_length=MAX_SCHEDULES_PER_JOB)

    @field_validator("job_id", "job_name", mode="before")
    @classmethod
    def _clean_text(cls, value: Any, info) -> Any:
        cleaned = _clean(value, _STRING_LIMITS[info.field_name])
        return "" if cleaned is None and info.field_name == "job_name" else cleaned


class _Reporter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hostname: Optional[str] = None

    @field_validator("hostname", mode="before")
    @classmethod
    def _clean_hostname(cls, value: Any) -> Optional[str]:
        return clean_hostname(value)


class JobsPayload(_Reporter):
    """Body of POST /api/v2/jobs: every job the agent has configured, replacing what it reported before."""

    jobs: list[JobConfig] = Field(max_length=MAX_JOBS)


class HeartbeatPayload(_Reporter):
    """Body of POST /api/v2/heartbeat: the agent is alive."""

    version: Optional[str] = None
    agent_type: Optional[str] = None

    @field_validator("version", "agent_type", mode="before")
    @classmethod
    def _clean_text(cls, value: Any, info) -> Optional[str]:
        return (_clean(value, _STRING_LIMITS[info.field_name]) or None) if isinstance(value, str) else None


class EventPayload(_Reporter):
    """Body of POST /api/v2/events: one step of a run. Unknown fields are ignored; nulls mean 'absent'."""

    version: Optional[str] = None
    agent_type: Optional[str] = None
    event_type: Optional[str] = None
    message: Optional[str] = None
    stage: Optional[str] = None
    timestamp: Number = None
    run_id: Optional[str] = None
    job_run_id: Optional[str] = None
    job_id: str = Field(min_length=1)
    job_name: Optional[str] = None
    target_id: str = Field(min_length=1)
    target_label: Optional[str] = None
    backup_type: Optional[str] = None
    source: Optional[str] = None
    destination: Optional[str] = None
    status: Optional[str] = None
    duration_seconds: Number = None
    files_backed_up: Number = None
    bytes_backed_up: Number = None
    bytes_compressed: Number = None
    percent_complete: Number = None
    bytes_per_second: Number = None
    eta_seconds: Number = None
    current_item: Optional[str] = None
    error_code: Number = None
    error_message: Optional[str] = None
    external_id: Optional[str] = None

    @field_validator(*_KEYS, mode="before")
    @classmethod
    def _clean_key(cls, value: Any, info) -> Any:
        return _clean(value, _STRING_LIMITS[info.field_name])

    @field_validator(*(k for k in _STRING_LIMITS if k not in _KEYS and k != "cron"), mode="after")
    @classmethod
    def _clean_text(cls, value: Optional[str], info) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value[:_STRING_LIMITS[info.field_name]] or None


class PurgePayload(BaseModel):
    """Body of POST /api/v2/purge: artifacts the agent deleted, so their runs show as purged."""

    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    external_ids: Optional[list[Any]] = None
    message: Optional[str] = None
    timestamp: Number = None

    @field_validator("job_id", "target_id", mode="before")
    @classmethod
    def _clean_key(cls, value: Any, info) -> Any:
        return _clean(value, _STRING_LIMITS[info.field_name])

    @field_validator("message", mode="after")
    @classmethod
    def _clean_text(cls, value: Optional[str]) -> Optional[str]:
        return (value.strip()[:2000] or None) if value is not None else None

    def clean_external_ids(self) -> list:
        return [text for text in (str(x).strip()[:500] for x in (self.external_ids or [])) if text]


_PROGRESS_COLUMNS = {
    "percent_complete": "percent_complete",
    "bytes_per_second": "bytes_per_second",
    "eta_seconds": "eta_seconds",
    "current_item": "current_item",
    "files_backed_up": "files_count",
    "bytes_backed_up": "bytes_processed",
}


def touch_agent(conn: sqlite3.Connection, agent, now: float, hostname: Optional[str] = None,
                version: Optional[str] = None, agent_type: Optional[str] = None) -> bool:
    """Record that an agent (an AgentConfig) was heard from; True if that changed its logged status or host name."""
    previous = conn.execute("SELECT hostname FROM agents WHERE id = ?", (agent.id,)).fetchone()
    conn.execute(
        "INSERT INTO agents (id, name, version, agent_type, hostname, last_heartbeat, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name = excluded.name, last_heartbeat = excluded.last_heartbeat, updated_at = excluded.updated_at, "
        "version = COALESCE(excluded.version, version), "
        "agent_type = COALESCE(excluded.agent_type, agent_type), "
        "hostname = COALESCE(excluded.hostname, hostname)",
        (agent.id, agent.name, version, agent_type, hostname, now, now),
    )
    host_changed = hostname is not None and (previous is None or previous["hostname"] != hostname)
    return agent_status.record_contact(conn, agent.id, agent.name, now) or host_changed


def _job(conn: sqlite3.Connection, agent_id: str, job_key: str, name: Optional[str], now: float,
         reported: bool = False) -> int:
    """The row of an agent's job, created when first seen and renamed when the agent's label changes."""
    row = conn.execute("SELECT id, name FROM jobs WHERE agent_id = ? AND job_key = ?", (agent_id, job_key)).fetchone()
    if row is None:
        return conn.execute(
            "INSERT INTO jobs (agent_id, job_key, name, active, reported_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (agent_id, job_key, name or job_key, now if reported else None, now)).lastrowid
    if name and name != row["name"]:
        conn.execute("UPDATE jobs SET name = ?, updated_at = ? WHERE id = ?", (name, now, row["id"]))
    if reported:
        conn.execute("UPDATE jobs SET active = 1, reported_at = ? WHERE id = ?", (now, row["id"]))
    return row["id"]


def _target(conn: sqlite3.Connection, job_pk: int, key: str, label: Optional[str], now: float,
            source: Optional[str] = None, destination: Optional[str] = None, reported: bool = False) -> int:
    row = conn.execute("SELECT id, label, source, destination FROM targets WHERE job_id = ? AND target_key = ?",
                       (job_pk, key)).fetchone()
    if row is None:
        return conn.execute(
            "INSERT INTO targets (job_id, target_key, label, source, destination, active, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)", (job_pk, key, label or key, source or "", destination or "", now)).lastrowid
    conn.execute(
        "UPDATE targets SET label = ?, source = ?, destination = ?, active = CASE WHEN ? THEN 1 ELSE active END, "
        "updated_at = ? WHERE id = ?",
        (label or row["label"], row["source"] if source is None else source,
         row["destination"] if destination is None else destination, 1 if reported else 0, now, row["id"]))
    return row["id"]


def _find_or_create_run(conn: sqlite3.Connection, target_pk: int, p: EventPayload, now: float) -> int:
    row = None
    if p.run_id:
        row = conn.execute(
            "SELECT id, backup_type FROM runs WHERE target_pk = ? AND run_id = ?", (target_pk, p.run_id)
        ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO runs (target_pk, run_id, job_run_id, backup_type, source, destination, status, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (target_pk, p.run_id, p.job_run_id, p.backup_type or "", p.source or "", p.destination or "", now, now),
        )
        return cursor.lastrowid
    # A run is only ever upgraded to 'full'; finalize events carry the original type.
    if p.backup_type == "full" and row["backup_type"] != "full":
        conn.execute("UPDATE runs SET backup_type = 'full', updated_at = ? WHERE id = ?", (now, row["id"]))
    return row["id"]


def _finalize(conn: sqlite3.Connection, run_pk: int, p: EventPayload, now: float) -> None:
    status = p.status or ("success" if p.event_type == "backup_complete" else "failed")
    # Success always reaches 100%; failures keep the last known percent.
    percent = 100 if status == "success" else None
    conn.execute(
        "UPDATE runs SET status = ?, completed_at = ?, runtime_seconds = ?, files_count = ?, "
        "bytes_processed = ?, bytes_compressed = ?, error_code = ?, error_message = ?, "
        "external_id = COALESCE(?, external_id), percent_complete = COALESCE(?, percent_complete), "
        "bytes_per_second = NULL, eta_seconds = NULL, current_item = NULL, updated_at = ? "
        "WHERE id = ?",
        (status, now, p.duration_seconds, p.files_backed_up, p.bytes_backed_up, p.bytes_compressed,
         p.error_code, p.error_message, p.external_id, percent, now, run_pk),
    )


def _apply_progress(conn: sqlite3.Connection, run_pk: int, p: EventPayload, now: float) -> None:
    sets, params = ["updated_at = ?", "progress_updated_at = ?"], [now, now]
    for field, column in _PROGRESS_COLUMNS.items():
        value = getattr(p, field)
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    if len(sets) > 2:
        conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", (*params, run_pk))


def record_heartbeat(conn: sqlite3.Connection, agent, p: HeartbeatPayload, now: float) -> dict:
    """The agent is alive; no run is touched."""
    with db.transaction(conn):
        touch_agent(conn, agent, now, p.hostname, p.version, p.agent_type)
        db.bump_rev(conn)
    return {"success": True}


def record_event(conn: sqlite3.Connection, agent, p: EventPayload, now: float) -> dict:
    """Apply one agent event and return the JSON body for the response."""
    with db.transaction(conn):
        touch_agent(conn, agent, now, p.hostname, p.version, p.agent_type)
        job_pk = _job(conn, agent.id, p.job_id, p.job_name, now)
        target_pk = _target(conn, job_pk, p.target_id, p.target_label, now)
        run_pk = _find_or_create_run(conn, target_pk, p, now)
        event_id = conn.execute(
            "INSERT INTO events (run_pk, event_type, message, stage, error_code, timestamp, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_pk, p.event_type or "", p.message or "", p.stage, p.error_code,
             p.timestamp or now, now),
        ).lastrowid

        # Only a completion call carries duration_seconds, so a later verify-step event
        # (error or heartbeat without it) never overwrites the finished run's outcome.
        if p.event_type in ("backup_complete", "error") and p.duration_seconds is not None:
            _finalize(conn, run_pk, p, now)
        else:
            _apply_progress(conn, run_pk, p, now)
        db.bump_rev(conn)
    return {"success": True, "event_id": event_id, "run_pk": run_pk}


def record_jobs(conn: sqlite3.Connection, agent, p: JobsPayload, now: float) -> int:
    """Replace what the agent reported about its jobs; returns the targets stored.

    A job or target the agent no longer lists is marked inactive, not deleted: its history stays.
    """
    stored = 0
    with db.transaction(conn):
        touch_agent(conn, agent, now, p.hostname)
        keys = []
        for job in p.jobs:
            job_pk = _job(conn, agent.id, job.job_id, job.job_name, now, reported=True)
            keys.append(job.job_id)
            conn.execute("DELETE FROM job_schedules WHERE job_id = ?", (job_pk,))
            conn.executemany("INSERT OR REPLACE INTO job_schedules (job_id, cron, enabled) VALUES (?, ?, ?)",
                             [(job_pk, s.cron, int(s.enabled)) for s in job.schedules])
            target_keys = []
            for target in job.targets:
                _target(conn, job_pk, target.target_id, target.target_label, now, target.source, target.destination,
                        reported=True)
                target_keys.append(target.target_id)
                stored += 1
            marks = ", ".join("?" * len(target_keys))
            conn.execute(f"UPDATE targets SET active = 0 WHERE job_id = ? AND target_key NOT IN ({marks})",
                         (job_pk, *target_keys))
        marks = ", ".join("?" * len(keys))
        conn.execute(f"UPDATE jobs SET active = 0 WHERE agent_id = ? AND job_key NOT IN ({marks})", (agent.id, *keys))
        db.bump_rev(conn)
    return stored


def record_purge(conn: sqlite3.Connection, agent, p: PurgePayload, now: float) -> dict:
    """Mark the agent's runs whose external_id was deleted locally as purged."""
    external_ids = p.clean_external_ids()
    with db.transaction(conn):
        marks = ", ".join("?" * len(external_ids))
        run_pks = [
            row["id"] for row in conn.execute(
                "SELECT id FROM run_view WHERE agent_id = ? AND job_key = ? AND target_id = ? "
                f"AND external_id IN ({marks})", (agent.id, p.job_id, p.target_id, *external_ids),
            )
        ]
        for run_pk in run_pks:
            conn.execute("UPDATE runs SET status = 'purged', updated_at = ? WHERE id = ?", (now, run_pk))
            conn.execute(
                "INSERT INTO events (run_pk, event_type, message, timestamp, created_at) "
                "VALUES (?, 'purged', ?, ?, ?)",
                (run_pk, p.message or DEFAULT_PURGE_MESSAGE, p.timestamp or now, now),
            )
        if run_pks:
            db.bump_rev(conn)
    return {"success": True, "purged_runs": len(run_pks)}
