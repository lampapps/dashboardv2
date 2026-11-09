"""Periodic tasks run by the systemd timer (`python -m app tick`)."""

import logging
import sqlite3
import time
from typing import Optional
from zoneinfo import ZoneInfo

from . import activity, db
from .config import Config

logger = logging.getLogger(__name__)


def _rollup_daily(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    activity.rollup_daily(conn, ZoneInfo(cfg.timezone), now, cfg.retention.runs_days)


# Order matters: rollups must run before any task that purges raw runs.
TASKS = [("daily rollup", _rollup_daily)]


def run_once(conn: sqlite3.Connection, cfg: Config, now: Optional[float] = None) -> None:
    """Run every due task once. A failing task is logged and does not stop the others."""
    now = time.time() if now is None else now
    with db.transaction(conn):
        db.set_meta(conn, "last_tick_ts", repr(now))
    for name, task in TASKS:
        try:
            task(conn, cfg, now)
        except sqlite3.Error:
            logger.exception("tick task '%s' failed", name)
    logger.info("tick complete")
