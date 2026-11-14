"""Small helpers shared by the admin console (no Textual imports, so they are easy to test)."""

import sqlite3
import time
from typing import Callable, Optional

from .. import db, maintenance


def run_db(func: Callable, *args):
    """Call func(conn, *args) on a fresh connection that is always closed (safe to run in a thread)."""
    from .. import settings  # late: tests move the paths

    if not settings.DB_PATH.exists():
        raise db.DbError(f"{settings.DB_PATH} does not exist yet; create it from the Database tab")
    conn = db.connect()
    try:
        return func(conn, *args)
    finally:
        conn.close()


def db_state() -> dict:
    """{'exists', 'version', 'supported', 'error'} without creating or migrating anything."""
    from .. import settings  # late: tests move the paths

    info = {"exists": settings.DB_PATH.exists(), "version": None, "supported": len(db.MIGRATIONS), "error": None}
    if not info["exists"]:
        return info
    try:
        info["version"] = run_db(db.schema_version)
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    return info


def ago(ts: Optional[float], now: Optional[float] = None) -> str:
    """'5 min ago' style text for an epoch time, or '-' when unknown."""
    if not ts:
        return "-"
    seconds = max(0, int((now if now is not None else time.time()) - ts))
    for limit, unit, size in ((90, "s", 1), (5400, "min", 60), (172800, "h", 3600)):
        if seconds < limit:
            return f"{seconds // size} {unit} ago"
    return f"{seconds // 86400} d ago"


format_bytes = maintenance.format_bytes


def one_line(text: Optional[str], limit: int = 60) -> str:
    """Text collapsed to one line and cut to `limit` characters with an ellipsis; '-' when empty."""
    flat = " ".join((text or "").split())
    if not flat:
        return "-"
    return flat if len(flat) <= limit else flat[:limit - 1] + "\u2026"
