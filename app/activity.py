"""Run outcomes grouped into display buckets, and the per-day activity counts behind the chart."""

import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import db

BUCKETS = ("success", "purged", "failed", "stopped", "running", "other")

_BUCKET_OF = {
    "success": "success",
    "completed": "success",
    "purged": "purged",
    "failed": "failed",
    "error": "failed",
    "stopped": "stopped",
    "running": "running",
}

# Statuses belonging to each display bucket ('other' is everything not listed here).
BUCKET_STATUSES = {
    b: tuple(s for s, member in _BUCKET_OF.items() if member == b)
    for b in ("success", "purged", "failed", "stopped", "running")
}

# A purged run succeeded before its artifact was rotated away, so metrics still count it as a success.
SUCCESSFUL_STATUSES = BUCKET_STATUSES["success"] + BUCKET_STATUSES["purged"]


def bucket(status: str) -> str:
    return _BUCKET_OF.get(status, "other")


def _day_start(day: date, tz: ZoneInfo) -> float:
    return datetime.combine(day, time.min, tzinfo=tz).timestamp()


def _today(now: float, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(now, tz).date()


def _count_runs(conn: sqlite3.Connection, tz: ZoneInfo, since: float) -> dict:
    """Return {(day, agent_id, bucket): count} for runs started at or after `since`."""
    counts: dict = {}
    for row in conn.execute("SELECT agent_id, started_at, status FROM run_view WHERE started_at >= ?", (since,)):
        key = (datetime.fromtimestamp(row["started_at"], tz).date().isoformat(), row["agent_id"], bucket(row["status"]))
        counts[key] = counts.get(key, 0) + 1
    return counts


def rollup_daily(conn: sqlite3.Connection, tz: ZoneInfo, now: float, runs_days: int) -> None:
    """Recompute daily counts for every day whose raw runs are still fully retained.

    Older days are left alone: their runs may already be partly purged, so the stored counts
    are the only complete record. Run this before the retention purge.
    """
    first_live = _today(now, tz) - timedelta(days=runs_days - 1)
    counts = _count_runs(conn, tz, _day_start(first_live, tz))
    with db.transaction(conn):
        conn.execute("DELETE FROM daily_runs WHERE day >= ?", (first_live.isoformat(),))
        conn.executemany(
            "INSERT INTO daily_runs (day, agent_id, bucket, runs) VALUES (?, ?, ?, ?)",
            [(day, agent_id, b, n) for (day, agent_id, b), n in counts.items()],
        )


def purge_runs(conn: sqlite3.Connection, tz: ZoneInfo, now: float, runs_days: int, rollup_days: int) -> int:
    """Delete runs (and their events) older than the raw window and rollups older than rollup_days.

    The day leaving the raw window is rolled up one last time first, so its counts are final.
    Returns the number of runs deleted.
    """
    rollup_daily(conn, tz, now, runs_days + 1)
    today = _today(now, tz)
    cutoff = _day_start(today - timedelta(days=runs_days - 1), tz)
    with db.transaction(conn):
        deleted = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM daily_runs WHERE day < ?", ((today - timedelta(days=rollup_days)).isoformat(),))
    return deleted


def activity(conn: sqlite3.Connection, tz: ZoneInfo, now: float, days: int, runs_days: int,
             agent_id: Optional[str] = None) -> dict:
    """Daily run counts per bucket for the last `days` days, oldest first.

    Days inside the raw-retention window are counted live (so running jobs show immediately);
    older days come from the stored rollup.
    """
    today = _today(now, tz)
    first = today - timedelta(days=days - 1)
    first_live = today - timedelta(days=runs_days - 1)
    labels = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    series = {b: {label: 0 for label in labels} for b in BUCKETS}

    if first < first_live:
        query = "SELECT day, bucket, SUM(runs) AS n FROM daily_runs WHERE day >= ? AND day < ?"
        params: list = [first.isoformat(), first_live.isoformat()]
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        for row in conn.execute(query + " GROUP BY day, bucket", params):
            series[row["bucket"]][row["day"]] += row["n"]

    for (day, run_agent, b), n in _count_runs(conn, tz, _day_start(max(first, first_live), tz)).items():
        if day in series[b] and (agent_id is None or run_agent == agent_id):
            series[b][day] += n

    return {"days": labels, "series": {b: [series[b][label] for label in labels] for b in BUCKETS}}
