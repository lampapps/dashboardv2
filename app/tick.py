"""Periodic tasks run by the systemd timer (`python -m app tick`)."""

import logging
import sqlite3
import time
from typing import Optional

from . import db
from .config import Config

logger = logging.getLogger(__name__)


def run_once(conn: sqlite3.Connection, cfg: Config, now: Optional[float] = None) -> None:
    """Run every due task once. Each task handles its own failures so one cannot block the rest."""
    now = time.time() if now is None else now
    with db.transaction(conn):
        db.set_meta(conn, "last_tick_ts", repr(now))
    logger.info("tick complete")
