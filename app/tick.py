"""Periodic tasks run by the systemd timer (`python -m app tick`)."""

import logging
import sqlite3
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import activity, db, notify, status, storage
from .config import Config

logger = logging.getLogger(__name__)


def _rollup_daily(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    activity.rollup_daily(conn, ZoneInfo(cfg.timezone), now, cfg.retention.runs_days)


def _purge_runs(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    retention = cfg.retention
    deleted = activity.purge_runs(conn, ZoneInfo(cfg.timezone), now, retention.runs_days, retention.rollup_days)
    if deleted:
        logger.info("runs purge: deleted %d run(s) older than %d days", deleted, retention.runs_days)


# Order matters: rollups run before anything that purges raw runs; slow S3 sampling goes last.
TASKS = [
    ("daily rollup", _rollup_daily),
    ("agent status", status.tick_task),
    ("offline alerts", notify.offline_alerts),
    ("digest", notify.digest_task),
    ("drive usage", storage.collect_drives),
    ("runs purge", _purge_runs),
    ("storage purge", storage.purge_old),
    ("status purge", status.purge_old),
    ("s3 usage", storage.collect_s3),
]


def run_once(conn: sqlite3.Connection, cfg: Config, now: Optional[float] = None) -> list:
    """Run every due task once and return [{name, seconds, error}]. A failing task does not stop the others."""
    now = time.time() if now is None else now
    with db.transaction(conn):
        db.set_meta(conn, "last_tick_ts", repr(now))
    results = []
    for name, task in TASKS:
        started = time.monotonic()
        error = None
        try:
            task(conn, cfg, now)
        except Exception as exc:  # pylint: disable=broad-except  # tasks are independent; one bug must not stop the rest
            logger.exception("tick task '%s' failed", name)
            error = f"{type(exc).__name__}: {exc}"
        results.append({"name": name, "seconds": time.monotonic() - started, "error": error})
    logger.info("tick complete")
    return results
