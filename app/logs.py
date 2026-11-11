"""Agent log tails: ingest, retention, and the queries behind the log viewer."""

import re
import sqlite3
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db
from .config import Config, clean_hostname

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
OTHER = "other"
LOG_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
MAX_LINES_PER_POST = 1000
MAX_LINES_AFTER_SPLIT = 5000
MAX_LINE_LENGTH = 2000
MAX_LOGS_PER_AGENT = 25
MAX_BATCH_ID_LENGTH = 100
BATCH_KEEP_S = 2 * 86400
DEFAULT_PAGE = 500
MAX_PAGE = 2000
# A cap is enforced only once this many lines over it have piled up, to avoid deleting on every post.
CAP_SLACK = 200

_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}
_LEVEL_IN_TEXT = re.compile(
    r"(?:^|[\s\-\[|:])(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)(?:$|[\s\-\]|:])")


class LogError(Exception):
    """A log post the agent should not retry as-is."""


class LogLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    level: Optional[str] = None
    ts: Optional[float] = None


class LogBatch(BaseModel):
    """Body of POST /api/v2/logs."""

    model_config = ConfigDict(extra="ignore")

    log: str = Field(pattern=LOG_NAME_PATTERN)
    batch_id: Optional[str] = Field(None, max_length=MAX_BATCH_ID_LENGTH)
    hostname: Optional[str] = None
    lines: list[Union[str, LogLine]] = Field(min_length=1, max_length=MAX_LINES_PER_POST)


    @field_validator("hostname", mode="before")
    @classmethod
    def _clean_hostname(cls, value: object) -> Optional[str]:
        return clean_hostname(value)


def normalize_level(level: Optional[str]) -> Optional[str]:
    if not level:
        return None
    upper = level.strip().upper()
    upper = _LEVEL_ALIASES.get(upper, upper)
    return upper if upper in LEVELS else None


def detect_level(text: str) -> Optional[str]:
    match = _LEVEL_IN_TEXT.search(text[:120])
    return normalize_level(match.group(1)) if match else None


def _rows(batch: LogBatch, agent: str, now: float) -> list:
    rows = []
    for item in batch.lines:
        line = item if isinstance(item, LogLine) else LogLine(message=item)
        ts = min(line.ts, now) if line.ts else now
        level = normalize_level(line.level)
        # Embedded newlines become separate lines; NUL bytes would upset downstream tools.
        for text in line.message.replace("\x00", "").splitlines() or [""]:
            text = text.rstrip("\r")[:MAX_LINE_LENGTH]
            rows.append((agent, batch.log, ts, level or detect_level(text), text))
    return rows[:MAX_LINES_AFTER_SPLIT]


def store_batch(conn: sqlite3.Connection, cfg: Config, agent: str, batch: LogBatch, now: float) -> dict:
    """Append a batch of lines; a repeated batch_id is acknowledged without storing it again."""
    cap = cfg.retention.log_lines_per_log
    with db.transaction(conn):
        if batch.batch_id:
            seen = conn.execute("SELECT 1 FROM log_batches WHERE agent = ? AND log = ? AND batch_id = ?",
                                (agent, batch.log, batch.batch_id)).fetchone()
            if seen:
                return {"success": True, "stored": 0, "duplicate": True}
        known = {r["log"] for r in conn.execute("SELECT DISTINCT log FROM log_lines WHERE agent = ?", (agent,))}
        if batch.log not in known and len(known) >= MAX_LOGS_PER_AGENT:
            raise LogError(f"too many logs for this agent (limit {MAX_LOGS_PER_AGENT})")
        rows = _rows(batch, agent, now)
        conn.executemany("INSERT INTO log_lines (agent, log, ts, level, text) VALUES (?, ?, ?, ?, ?)", rows)
        if batch.batch_id:
            conn.execute("INSERT INTO log_batches (agent, log, batch_id, ts) VALUES (?, ?, ?, ?)",
                         (agent, batch.log, batch.batch_id, now))
        # Drop the oldest lines once the stream is well past its cap.
        stream = "SELECT id FROM log_lines WHERE agent = ? AND log = ? ORDER BY id DESC LIMIT 1 OFFSET ?"
        if conn.execute(stream, (agent, batch.log, cap + CAP_SLACK)).fetchone():
            newest_to_drop = conn.execute(stream, (agent, batch.log, cap)).fetchone()["id"]
            conn.execute("DELETE FROM log_lines WHERE agent = ? AND log = ? AND id <= ?",
                         (agent, batch.log, newest_to_drop))
        db.bump_rev(conn, "log_rev")
    return {"success": True, "stored": len(rows)}


def purge_old(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM log_lines WHERE ts < ?", (now - cfg.retention.log_days * 86400,))
        conn.execute("DELETE FROM log_batches WHERE ts < ?", (now - BATCH_KEEP_S,))


# ---------------------------------------------------------------- viewer queries

def list_logs(conn: sqlite3.Connection) -> list:
    """Every stored log with its line count, time span and per-level counts, most recent first."""
    logs: dict = {}
    for row in conn.execute(
        "SELECT agent, log, COALESCE(level, 'other') AS lv, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM log_lines GROUP BY agent, log, lv"
    ):
        entry = logs.setdefault((row["agent"], row["log"]), {
            "agent": row["agent"], "log": row["log"], "lines": 0, "first_ts": row["first_ts"],
            "last_ts": row["last_ts"], "counts": {}})
        entry["lines"] += row["n"]
        entry["counts"][row["lv"]] = row["n"]
        entry["first_ts"] = min(entry["first_ts"], row["first_ts"])
        entry["last_ts"] = max(entry["last_ts"], row["last_ts"])
    return sorted(logs.values(), key=lambda e: e["last_ts"], reverse=True)


def _filters(agent: str, log: str, levels: Optional[list], query: Optional[str]) -> tuple:
    where, params = ["agent = ?", "log = ?"], [agent, log]
    wanted = [lv for lv in (levels or []) if lv in LEVELS or lv == OTHER]
    if wanted:
        named = [lv for lv in wanted if lv != OTHER]
        parts = []
        if named:
            parts.append(f"level IN ({', '.join('?' * len(named))})")
            params += named
        if OTHER in wanted:
            parts.append("level IS NULL")
        where.append("(" + " OR ".join(parts) + ")")
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    return " AND ".join(where), params


def read_lines(conn: sqlite3.Connection, agent: str, log: str, limit: int = DEFAULT_PAGE,
               after: Optional[int] = None, before: Optional[int] = None,
               levels: Optional[list] = None, query: Optional[str] = None) -> dict:
    """Lines oldest-first. No cursor: the newest `limit`. `after`: newer than that id. `before`: the page before it."""
    limit = max(1, min(MAX_PAGE, limit))
    clause, params = _filters(agent, log, levels, query)
    if after is not None:
        rows = conn.execute(
            f"SELECT id, ts, level, text FROM log_lines WHERE {clause} AND id > ? ORDER BY id LIMIT ?",
            (*params, after, limit)).fetchall()
        lines = [dict(r) for r in rows]
    else:
        bound, bound_params = ("AND id < ?", [before]) if before is not None else ("", [])
        rows = conn.execute(
            f"SELECT id, ts, level, text FROM log_lines WHERE {clause} {bound} ORDER BY id DESC LIMIT ?",
            (*params, *bound_params, limit)).fetchall()
        lines = [dict(r) for r in reversed(rows)]
    oldest_shown = lines[0]["id"] if lines and after is None else None
    has_more = False
    if oldest_shown is not None:
        has_more = conn.execute(f"SELECT 1 FROM log_lines WHERE {clause} AND id < ? LIMIT 1",
                                (*params, oldest_shown)).fetchone() is not None
    return {"lines": lines, "has_more_before": has_more}


def export_text(conn: sqlite3.Connection, agent: str, log: str) -> str:
    return "".join(f"{r['text']}\n" for r in conn.execute(
        "SELECT text FROM log_lines WHERE agent = ? AND log = ? ORDER BY id", (agent, log)))
