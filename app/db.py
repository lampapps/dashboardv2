"""SQLite access: connection settings, numbered migrations, and small helpers."""

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator, Optional, Union

from . import settings

PathLike = Union[str, Path]


class DbError(Exception):
    """Raised for schema or backup problems the caller can report to the user."""


# Each migration is a list of statements applied in one transaction. Until the first
# production install, migration 1 may still be edited; after that, only append.
MIGRATIONS: list[list[str]] = [
    [
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE agent_state (
            name TEXT PRIMARY KEY,
            version TEXT,
            agent_type TEXT,
            last_heartbeat REAL,
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            run_id TEXT,
            job_run_id TEXT,
            job_name TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            target_label TEXT NOT NULL DEFAULT '',
            backup_type TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            destination TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            started_at REAL NOT NULL,
            completed_at REAL,
            runtime_seconds REAL,
            files_count INTEGER,
            bytes_processed INTEGER,
            bytes_compressed INTEGER,
            percent_complete INTEGER,
            bytes_per_second REAL,
            eta_seconds REAL,
            current_item TEXT,
            error_code INTEGER,
            error_message TEXT,
            external_id TEXT,
            progress_updated_at REAL,
            updated_at REAL NOT NULL
        )""",
        "CREATE UNIQUE INDEX idx_runs_agent_run_id ON runs(agent, run_id) WHERE run_id IS NOT NULL",
        "CREATE INDEX idx_runs_agent_job ON runs(agent, job_name, started_at DESC)",
        "CREATE INDEX idx_runs_target ON runs(agent, target_id, started_at DESC)",
        "CREATE INDEX idx_runs_started ON runs(started_at)",
        "CREATE INDEX idx_runs_external ON runs(agent, target_id, external_id)",
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_pk INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            stage TEXT,
            error_code INTEGER,
            timestamp REAL NOT NULL,
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX idx_events_run ON events(run_pk, id)",
        """CREATE TABLE job_schedules (
            agent TEXT NOT NULL,
            job_name TEXT NOT NULL,
            cron_schedule TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (agent, job_name)
        )""",
        """CREATE TABLE agent_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            ts REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('up', 'down', 'disabled', 'unknown')),
            reason TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX idx_status_log_agent_ts ON agent_status_log(agent, ts)",
    ],
    [
        # Per-day run counts by outcome bucket; outlives the raw runs the counts came from.
        """CREATE TABLE daily_runs (
            day TEXT NOT NULL,
            agent TEXT NOT NULL,
            bucket TEXT NOT NULL,
            runs INTEGER NOT NULL,
            PRIMARY KEY (day, agent, bucket)
        )""",
    ],
    [
        # Storage usage over time. source is 'dashboard' or an agent name; series splits a group
        # (S3 storage class or prefix) and is '' for plain drives. total_bytes is NULL when unbounded.
        """CREATE TABLE storage_samples (
            source TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('local', 'cloud', 'repo')),
            name TEXT NOT NULL,
            series TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL,
            used_bytes INTEGER NOT NULL,
            total_bytes INTEGER,
            PRIMARY KEY (source, kind, name, series, ts)
        )""",
        "CREATE INDEX idx_storage_samples_ts ON storage_samples(ts)",
    ],
    [
        # Last logged up/down status per agent (NULL = never known), so ingest can spot transitions cheaply.
        "ALTER TABLE agent_state ADD COLUMN status TEXT",
        "ALTER TABLE agent_state ADD COLUMN status_since REAL",
    ],
    [
        # Agent log tails. id is the cursor the viewer follows; batches make agent retries idempotent.
        """CREATE TABLE log_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            log TEXT NOT NULL,
            ts REAL NOT NULL,
            level TEXT,
            text TEXT NOT NULL
        )""",
        "CREATE INDEX idx_log_lines_stream ON log_lines(agent, log, id)",
        "CREATE INDEX idx_log_lines_ts ON log_lines(ts)",
        """CREATE TABLE log_batches (
            agent TEXT NOT NULL,
            log TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            ts REAL NOT NULL,
            PRIMARY KEY (agent, log, batch_id)
        )""",
    ],
]


def connect(path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use transaction() for writes."""
    conn = sqlite3.connect(str(path or settings.DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one write transaction, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply(conn: sqlite3.Connection, number: int) -> None:
    try:
        with transaction(conn):
            # Another process may have applied it while we waited for the write lock.
            if schema_version(conn) >= number:
                return
            for statement in MIGRATIONS[number - 1]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(number)}")
    except sqlite3.Error as exc:
        raise DbError(f"migration {number} failed: {exc}") from exc


def init_db(path: Optional[PathLike] = None) -> int:
    """Create or upgrade the schema and return its version. Safe to run repeatedly."""
    path = Path(path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            raise DbError(f"cannot open database {path}: {exc}") from exc
        current = schema_version(conn)
        if current > len(MIGRATIONS):
            raise DbError(
                f"{path} has schema version {current}, newer than this code supports "
                f"({len(MIGRATIONS)}); upgrade the application or restore a backup"
            )
        for number in range(current + 1, len(MIGRATIONS) + 1):
            _apply(conn, number)
        return len(MIGRATIONS)
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bump_rev(conn: sqlite3.Connection, key: str = "rev") -> None:
    """Advance a data revision the UI uses for ETags; call inside the writing transaction."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (key,),
    )


def backup(dest: PathLike, src: Optional[PathLike] = None) -> Path:
    """Write a consistent copy of the database (safe while the app is running)."""
    dest = Path(dest)
    if dest.exists():
        raise DbError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = connect(src)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    except sqlite3.Error as exc:
        dest.unlink(missing_ok=True)
        raise DbError(f"backup failed: {exc}") from exc
    finally:
        target.close()
        source.close()
    return dest
