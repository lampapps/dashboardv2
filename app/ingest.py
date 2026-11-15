"""Agent event ingest: validate payloads and apply them to the database.

Behavior matches the v1 dashboard's /api/monitoring endpoints so existing agents work unchanged
(see docs/AGENTS_API_GUIDE.md); the contract tests in tests/contract enforce this.
"""

import sqlite3
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, field_validator

from . import db, status as agent_status
from .config import clean_hostname

Number = Optional[Union[int, float]]

# Agent-supplied text is untrusted: long values are truncated, never rejected.
_STRING_LIMITS = {
    "version": 100, "agent_type": 100, "event_type": 50, "status": 50,
    "message": 2000, "stage": 200, "error_message": 2000,
    "run_id": 200, "job_run_id": 200, "target_id": 500, "target_label": 500,
    "job_name": 200, "backup_type": 50, "external_id": 500,
    "source": 1000, "destination": 1000, "current_item": 1000, "cron_schedule": 500,
}

DEFAULT_PURGE_MESSAGE = "Job target artifact(s) purged by agent"


class EventPayload(BaseModel):
    """Body of POST /api/monitoring/events. Unknown fields are ignored; nulls mean 'absent'."""

    model_config = ConfigDict(extra="ignore")

    version: Optional[str] = None
    agent_type: Optional[str] = None
    event_type: Optional[str] = None
    message: Optional[str] = None
    stage: Optional[str] = None
    timestamp: Number = None
    run_id: Optional[str] = None
    job_run_id: Optional[str] = None
    target_id: Optional[str] = None
    target_label: Optional[str] = None
    job_name: Optional[str] = None
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
    cron_schedule: Optional[str] = None
    hostname: Optional[str] = None

    @field_validator("hostname", mode="before")
    @classmethod
    def _clean_hostname(cls, value: Any) -> Optional[str]:
        return clean_hostname(value)

    @field_validator(*_STRING_LIMITS, mode="after")
    @classmethod
    def _clean_text(cls, value: Optional[str], info) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value[:_STRING_LIMITS[info.field_name]] or None


class PurgePayload(BaseModel):
    """Body of POST /api/monitoring/target-purged."""

    model_config = ConfigDict(extra="ignore")

    target_id: Optional[str] = None
    external_ids: Optional[list[Any]] = None
    message: Optional[str] = None
    timestamp: Number = None

    @field_validator("target_id", "message", mode="after")
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


def _touch_agent(conn: sqlite3.Connection, agent: str, payload: EventPayload, now: float) -> bool:
    previous = conn.execute("SELECT hostname FROM agent_state WHERE name = ?", (agent,)).fetchone()
    conn.execute(
        "INSERT INTO agent_state (name, version, agent_type, hostname, last_heartbeat, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "last_heartbeat = excluded.last_heartbeat, updated_at = excluded.updated_at, "
        "version = COALESCE(excluded.version, version), "
        "agent_type = COALESCE(excluded.agent_type, agent_type), "
        "hostname = COALESCE(excluded.hostname, hostname)",
        (agent, payload.version, payload.agent_type, payload.hostname, now, now),
    )
    host_changed = payload.hostname is not None and (previous is None or previous["hostname"] != payload.hostname)
    return agent_status.record_contact(conn, agent, now) or host_changed


def touch_agent(conn: sqlite3.Connection, agent: str, now: float, hostname: Optional[str] = None) -> bool:
    """Record that an agent was heard from; True if that changed its logged status or its host name."""
    return _touch_agent(conn, agent, EventPayload(hostname=hostname), now)


def _find_or_create_run(conn: sqlite3.Connection, agent: str, p: EventPayload, now: float) -> int:
    row = None
    if p.run_id:
        row = conn.execute(
            "SELECT id, backup_type FROM runs WHERE agent = ? AND run_id = ?", (agent, p.run_id)
        ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO runs (agent, run_id, job_run_id, job_name, target_id, target_label, "
            "backup_type, source, destination, status, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (agent, p.run_id, p.job_run_id, p.job_name or "", p.target_id, p.target_label or "",
             p.backup_type or "", p.source or "", p.destination or "", now, now),
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


def record_event(conn: sqlite3.Connection, agent: str, p: EventPayload, now: float) -> dict:
    """Apply one agent event and return the JSON body for the response."""
    with db.transaction(conn):
        _touch_agent(conn, agent, p, now)
        if p.cron_schedule and p.job_name:
            conn.execute(
                "INSERT INTO job_schedules (agent, job_name, cron_schedule, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(agent, job_name) DO UPDATE SET "
                "cron_schedule = excluded.cron_schedule, updated_at = excluded.updated_at",
                (agent, p.job_name, p.cron_schedule, now),
            )
        if not p.target_id:  # a bare heartbeat creates no run
            db.bump_rev(conn)
            return {"success": True}

        run_pk = _find_or_create_run(conn, agent, p, now)
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
    return {"success": True, "event_id": event_id, "backup_job_id": run_pk}


def record_purge(conn: sqlite3.Connection, agent: str, p: PurgePayload, now: float) -> dict:
    """Mark the agent's runs whose external_id was deleted locally as purged."""
    external_ids = p.clean_external_ids()
    with db.transaction(conn):
        marks = ", ".join("?" * len(external_ids))
        run_pks = [
            row["id"] for row in conn.execute(
                f"SELECT id FROM runs WHERE agent = ? AND target_id = ? AND external_id IN ({marks})",
                (agent, p.target_id, *external_ids),
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
    return {"success": True, "purged_jobs": len(run_pks)}
