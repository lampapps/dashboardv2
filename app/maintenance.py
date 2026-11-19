"""Database maintenance for the admin tools: integrity, vacuum, restore, and deleting stored history."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db, settings


def integrity_problems(conn: sqlite3.Connection) -> list:
    """Messages from SQLite's integrity and foreign key checks; empty when the database is sound."""
    problems = [row[0] for row in conn.execute("PRAGMA integrity_check") if row[0] != "ok"]
    problems += [f"foreign key violation in {row[0]} (row {row[1]})" for row in conn.execute("PRAGMA foreign_key_check")]
    return problems


def format_bytes(size: int) -> str:
    """'1.5 MiB' style text."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def file_sizes(path: Optional[Path] = None) -> dict:
    """Bytes used by the database file and its WAL (0 when absent)."""
    path = Path(path or settings.DB_PATH)

    def size(candidate: Path) -> int:
        return candidate.stat().st_size if candidate.exists() else 0

    return {"db": size(path), "wal": size(Path(f"{path}-wal"))}


def vacuum(conn: sqlite3.Connection) -> dict:
    """Rebuild the file to reclaim free pages and fold the WAL in; returns sizes before and after."""
    before = file_sizes()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        raise db.DbError(f"vacuum failed: {exc}") from exc
    return {"before": before, "after": file_sizes()}


def table_counts(conn: sqlite3.Connection) -> list:
    """[(table, rows)] for every application table, largest first."""
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = [(name, conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names]
    return sorted(counts, key=lambda item: (-item[1], item[0]))


def meta_items(conn: sqlite3.Connection) -> list:
    return [(row["key"], row["value"]) for row in conn.execute("SELECT key, value FROM meta ORDER BY key")]


def reset_database() -> int:
    """Delete the database (and its WAL files) and recreate it empty; returns the new schema version."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.DB_PATH}{suffix}").unlink(missing_ok=True)
    return db.init_db()


def list_backups() -> list:
    """Backup files in the backup directory, newest first, as (path, bytes, mtime)."""
    if not settings.BACKUP_DIR.is_dir():
        return []
    files = [p for p in settings.BACKUP_DIR.iterdir() if p.is_file() and p.suffix == ".sqlite"]
    return [(p, p.stat().st_size, p.stat().st_mtime) for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


def check_backup_file(source: Path) -> int:
    """Validate that `source` is an intact database this version can use; returns its schema version."""
    if not source.is_file():
        raise db.DbError(f"{source} not found")
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise db.DbError(f"cannot open {source}: {exc}") from exc
    try:
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        except sqlite3.Error as exc:
            raise db.DbError(f"{source} is not a usable database: {exc}") from exc
    finally:
        conn.close()
    if result != ["ok"]:
        raise db.DbError(f"{source} failed the integrity check: {result[0]}")
    if version < 1:
        raise db.DbError(f"{source} is not a JABS database (no schema version)")
    if version > len(db.MIGRATIONS):
        raise db.DbError(
            f"{source} has schema version {version}, newer than this code supports ({len(db.MIGRATIONS)})")
    return version


def restore(source: Path) -> dict:
    """Replace the live database with a backup, after saving the current one as pre-restore-<time>.sqlite."""
    source = Path(source)
    check_backup_file(source)
    safety = None
    if settings.DB_PATH.exists():
        safety = db.backup(settings.BACKUP_DIR / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}.sqlite")
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target = db.connect()
    try:
        reader.backup(target)
    except sqlite3.Error as exc:
        raise db.DbError(f"restore failed: {exc}") from exc
    finally:
        target.close()
        reader.close()
    return {"safety_copy": safety, "schema_version": db.init_db()}


def resolve_job(conn: sqlite3.Connection, agent_id: str, ref: str) -> str:
    """The job_key of an agent's job given as its key or its name; raises DbError when unknown or ambiguous."""
    rows = conn.execute("SELECT job_key, name FROM jobs WHERE agent_id = ? AND (job_key = ? OR name = ?)",
                        (agent_id, ref, ref)).fetchall()
    keys = sorted({row["job_key"] for row in rows})
    if not keys:
        raise db.DbError(f"no job '{ref}' for that agent")
    exact = [key for key in keys if key == ref]
    if exact:
        return exact[0]
    if len(keys) > 1:
        raise db.DbError(f"several jobs are named '{ref}'; use one of their keys: {', '.join(keys)}")
    return keys[0]


def job_counts(conn: sqlite3.Connection) -> list:
    """[{agent_id, agent, job_key, job_name, runs}] for every job with stored runs."""
    return [dict(row) for row in conn.execute(
        "SELECT a.id AS agent_id, a.name AS agent, j.job_key, j.name AS job_name, COUNT(r.id) AS runs "
        "FROM jobs j JOIN agents a ON a.id = j.agent_id JOIN targets t ON t.job_id = j.id "
        "JOIN runs r ON r.target_pk = t.id GROUP BY j.id ORDER BY a.name, j.name")]


def list_runs(conn: sqlite3.Connection, agent_id: str, job_key: Optional[str] = None, limit: int = 500) -> list:
    """An agent's stored runs, newest first, optionally one job's; each a dict with id, job_name, target_label,
    status, started_at, runtime_seconds, bytes_processed and message (the closing event's text, as on the Job page)."""
    clause, params = "agent_id = ?", [agent_id]
    if job_key is not None:
        clause += " AND job_key = ?"
        params.append(job_key)
    return [dict(row) for row in conn.execute(
        "SELECT id, job_name, target_label, status, started_at, runtime_seconds, bytes_processed, "
        "COALESCE((SELECT message FROM events WHERE run_pk = run_view.id AND event_type IN ('backup_complete', 'error') "
        "ORDER BY id DESC LIMIT 1), (SELECT message FROM events WHERE run_pk = run_view.id ORDER BY id DESC LIMIT 1)) "
        f"AS message FROM run_view WHERE {clause} ORDER BY started_at DESC, id DESC LIMIT ?", (*params, max(1, limit)))]


def delete_run_ids(conn: sqlite3.Connection, ids: list) -> int:
    """Delete specific runs (and their events) by id; returns how many existed.

    Inside the retention window the day's activity count follows at the next tick; older rolled-up counts stay.
    """
    wanted = sorted({int(i) for i in ids})
    deleted = 0
    with db.transaction(conn):
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            deleted += conn.execute(
                f"DELETE FROM runs WHERE id IN ({', '.join('?' * len(chunk))})", chunk).rowcount
        if deleted:
            db.bump_rev(conn)
    return deleted


def delete_runs(conn: sqlite3.Connection, agent_id: str, job_key: Optional[str] = None) -> int:
    """Delete an agent's runs (and their events), optionally only one job's; returns the runs deleted.

    A job the agent no longer reports goes with its runs (its targets and schedules cascade); one it still
    reports stays, since it is configured there. Per-day counts already rolled up are kept.
    """
    where, params = "j.agent_id = ?", [agent_id]
    if job_key is not None:
        where += " AND j.job_key = ?"
        params.append(job_key)
    with db.transaction(conn):
        deleted = conn.execute(
            "DELETE FROM runs WHERE target_pk IN (SELECT t.id FROM targets t JOIN jobs j ON j.id = t.job_id "
            f"WHERE {where})", params).rowcount
        conn.execute(f"DELETE FROM jobs WHERE id IN (SELECT j.id FROM jobs j WHERE {where} AND j.active = 0)", params)
        if deleted:
            db.bump_rev(conn)
    return deleted


def delete_agent_history(conn: sqlite3.Connection, agent_id: str) -> dict:
    """Delete everything stored for an agent id (its row cascades); returns rows deleted per kind."""
    with db.transaction(conn):
        deleted = {
            "runs": conn.execute("SELECT COUNT(*) FROM run_view WHERE agent_id = ?", (agent_id,)).fetchone()[0],
            "jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE agent_id = ?", (agent_id,)).fetchone()[0],
            "agent_status_log": conn.execute("SELECT COUNT(*) FROM agent_status_log WHERE agent_id = ?",
                                            (agent_id,)).fetchone()[0],
            "daily_runs": conn.execute("SELECT COUNT(*) FROM daily_runs WHERE agent_id = ?", (agent_id,)).fetchone()[0],
        }
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        deleted["storage_samples"] = conn.execute("DELETE FROM storage_samples WHERE source = ?", (agent_id,)).rowcount
        db.bump_rev(conn)
    return deleted


def orphans(conn: sqlite3.Connection, registered_ids: list) -> dict:
    """What the database holds that nothing configures any more.

    agents: stored agents that are not in agents.yaml (removed without purging their history).
    jobs: jobs an agent no longer lists in its jobs report, with how many runs they still have.
    """
    marks = ", ".join("?" * len(registered_ids))
    agents = [dict(row) for row in conn.execute(
        "SELECT a.id, a.name, a.last_heartbeat, "
        "(SELECT COUNT(*) FROM run_view r WHERE r.agent_id = a.id) AS runs "
        f"FROM agents a WHERE a.id NOT IN ({marks}) ORDER BY a.name", registered_ids)]
    jobs = [dict(row) for row in conn.execute(
        "SELECT a.id AS agent_id, a.name AS agent, j.job_key, j.name AS job_name, COUNT(r.id) AS runs "
        "FROM jobs j JOIN agents a ON a.id = j.agent_id LEFT JOIN targets t ON t.job_id = j.id "
        "LEFT JOIN runs r ON r.target_pk = t.id "
        f"WHERE j.active = 0 AND a.id IN ({marks}) GROUP BY j.id ORDER BY a.name, j.name", registered_ids)]
    return {"agents": agents, "jobs": jobs}


def storage_series(conn: sqlite3.Connection) -> list:
    """[{source, kind, name, samples}] for every stored storage series."""
    return [dict(row) for row in conn.execute(
        "SELECT source, kind, name, COUNT(*) AS samples FROM storage_samples "
        "GROUP BY source, kind, name ORDER BY source, kind, name")]


def purge_storage(conn: sqlite3.Connection, source: Optional[str] = None, name: Optional[str] = None) -> int:
    """Delete stored storage samples, optionally only one source and/or name; returns the rows deleted."""
    clauses, params = [], []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if name is not None:
        clauses.append("name = ?")
        params.append(name)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.transaction(conn):
        deleted = conn.execute(f"DELETE FROM storage_samples{where}", params).rowcount
        if deleted:
            db.bump_rev(conn)
    return deleted
