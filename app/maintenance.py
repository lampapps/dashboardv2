"""Database maintenance for the admin tools: integrity, vacuum, restore, and deleting stored history."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db, settings

# Every table column that stores an agent's name; there are no foreign keys between them.
AGENT_NAME_COLUMNS = ("runs", "agent"), ("daily_runs", "agent"), ("job_schedules", "agent"), \
    ("agent_status_log", "agent"), ("agent_state", "name"), ("storage_samples", "source")


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


def job_counts(conn: sqlite3.Connection) -> list:
    """[{agent, job_name, runs}] for every job with stored runs."""
    return [dict(row) for row in conn.execute(
        "SELECT agent, job_name, COUNT(*) AS runs FROM runs GROUP BY agent, job_name ORDER BY agent, job_name")]


def list_runs(conn: sqlite3.Connection, agent: str, job_name: Optional[str] = None, limit: int = 500) -> list:
    """An agent's stored runs, newest first, optionally one job's; each a dict with id, job_name, target_label,
    status, started_at, runtime_seconds, bytes_processed and message (the closing event's text, as on the Job page)."""
    clause, params = "agent = ?", [agent]
    if job_name is not None:
        clause += " AND job_name = ?"
        params.append(job_name)
    return [dict(row) for row in conn.execute(
        "SELECT id, job_name, target_label, status, started_at, runtime_seconds, bytes_processed, "
        "COALESCE((SELECT message FROM events WHERE run_pk = runs.id AND event_type IN ('backup_complete', 'error') "
        "ORDER BY id DESC LIMIT 1), (SELECT message FROM events WHERE run_pk = runs.id ORDER BY id DESC LIMIT 1)) "
        f"AS message FROM runs WHERE {clause} ORDER BY started_at DESC, id DESC LIMIT ?", (*params, max(1, limit)))]


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


def delete_runs(conn: sqlite3.Connection, agent: str, job_name: Optional[str] = None) -> int:
    """Delete an agent's runs (and their events), optionally only one job's; returns the runs deleted.

    Per-day counts already rolled up are kept, so the activity history stays.
    """
    with db.transaction(conn):
        if job_name is None:
            deleted = conn.execute("DELETE FROM runs WHERE agent = ?", (agent,)).rowcount
        else:
            deleted = conn.execute("DELETE FROM runs WHERE agent = ? AND job_name = ?", (agent, job_name)).rowcount
            conn.execute("DELETE FROM job_schedules WHERE agent = ? AND job_name = ?", (agent, job_name))
        if deleted:
            db.bump_rev(conn)
    return deleted


def delete_agent_history(conn: sqlite3.Connection, agent: str) -> dict:
    """Delete everything stored under an agent's name; returns rows deleted per table."""
    deleted = {}
    with db.transaction(conn):
        for table, column in AGENT_NAME_COLUMNS:
            deleted[table] = conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (agent,)).rowcount
        db.bump_rev(conn)
    return deleted


def rename_agent(old: str, new: str) -> dict:
    """Rename an agent in agents.yaml and, when the database exists, in every table that stores its name.

    Returns the rows changed per table. A name that already has stored history is refused: its rows would merge
    with the old agent's. The registry is written last, inside the transaction, so a failure changes neither.
    """
    if not settings.DB_PATH.exists():
        config.rename_agent(old, new)
        return {}
    conn = db.connect()
    try:
        with db.transaction(conn):
            taken = [table for table, column in AGENT_NAME_COLUMNS
                     if conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (new,)).fetchone()]
            if taken:
                raise config.ConfigError(f"'{new}' already has stored history ({', '.join(taken)}); choose another name")
            changed = {table: conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                                           (new, old)).rowcount for table, column in AGENT_NAME_COLUMNS}
            config.rename_agent(old, new)
            db.bump_rev(conn)
    finally:
        conn.close()
    return changed


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
