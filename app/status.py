"""Agent up/down state and its history.

Transitions are logged in agent_status_log in two places: an agent's contact marks it up
(see ingest), and the periodic tick marks it down, disabled or unknown. Both go through
evaluate() so the Agents page can show a pending change before the next tick writes it.
"""

import sqlite3
from typing import Optional

from . import db, settings
from .config import AgentConfig, ConfigError, load_agents, Config

# The tick runs every TICK_INTERVAL_S; a longer silence means the dashboard itself was not running.
GAP_S = 2 * settings.TICK_INTERVAL_S + 60
LOGGED = ("up", "down", "disabled", "unknown")
UNKNOWN_REASON = "dashboard was not running"


def evaluate(agent: AgentConfig, status: Optional[str], last_heartbeat: Optional[float], now: float,
             gap_end: float = 0.0) -> Optional[tuple]:
    """Return (new_status, when) if the stored status should change, else None.

    new_status is one of up/down/disabled, or 'new' (nothing known yet, not logged). A down
    transition is dated to when the grace period ran out, not to when the tick noticed. After a
    dashboard outage ('unknown') the grace period restarts at gap_end, so an agent is not
    blamed for silence the dashboard could not have heard.
    """
    if not agent.enabled:
        return None if status == "disabled" else ("disabled", now)
    grace = agent.grace_minutes * 60
    if status == "disabled":  # just re-enabled
        if last_heartbeat is None:
            return ("new", now)
        return ("up", now) if now - last_heartbeat <= grace else ("down", now)
    if last_heartbeat is None:
        return None
    reference = max(last_heartbeat, gap_end) if status == "unknown" else last_heartbeat
    if now - reference > grace:
        return None if status == "down" else ("down", min(reference + grace, now))
    if status == "unknown" and last_heartbeat < gap_end:
        return None  # the agent has until its grace period ends to report in after the outage
    # Fresh contact but not logged as up: normally ingest logs it, this is the fallback.
    return None if status == "up" else ("up", last_heartbeat)


def effective_status(agent: AgentConfig, status: Optional[str], last_heartbeat: Optional[float],
                     now: float, gap_end: float = 0.0) -> str:
    """The status to show right now: the stored one with any pending change applied."""
    if not agent.enabled:
        return "disabled"
    pending = evaluate(agent, status, last_heartbeat, now, gap_end)
    current = pending[0] if pending else status
    return current if current in LOGGED else "new"


def _set_status(conn: sqlite3.Connection, agent_id: str, name: str, status: Optional[str], when: float,
                now: float, reason: str = "") -> None:
    conn.execute(
        "INSERT INTO agents (id, name, updated_at, status, status_since) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, status = excluded.status, "
        "status_since = excluded.status_since, updated_at = excluded.updated_at",
        (agent_id, name, now, status, when),
    )
    if status in LOGGED:
        conn.execute("INSERT INTO agent_status_log (agent_id, ts, status, reason) VALUES (?, ?, ?, ?)",
                     (agent_id, when, status, reason))


def record_contact(conn: sqlite3.Connection, agent_id: str, name: str, now: float) -> bool:
    """Log the agent as up if it was not; call inside the transaction that records the contact."""
    row = conn.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is not None and row["status"] == "up":
        return False
    _set_status(conn, agent_id, name, "up", now, now, "contact")
    return True


def update_statuses(conn: sqlite3.Connection, agents: list, now: float) -> int:
    """Apply every due status change; returns how many agents changed."""
    changed = 0
    names = {agent.id: agent.name for agent in agents}
    with db.transaction(conn):
        previous_alive = float(db.get_meta(conn, "alive_ts") or 0)
        latest_contact = conn.execute("SELECT MAX(last_heartbeat) FROM agents").fetchone()[0] or 0
        seen = max(previous_alive, latest_contact)
        if seen and now - seen > GAP_S:
            for row in conn.execute("SELECT id, name FROM agents WHERE status IN ('up', 'down')").fetchall():
                _set_status(conn, row["id"], names.get(row["id"], row["name"]), "unknown", seen, now, UNKNOWN_REASON)
                changed += 1
            db.set_meta(conn, "gap_end_ts", repr(now))
        gap_end = float(db.get_meta(conn, "gap_end_ts") or 0)
        states = {row["id"]: row for row in conn.execute("SELECT id, name, status, last_heartbeat FROM agents")}
        for agent in agents:
            state = states.get(agent.id)
            result = evaluate(agent, state["status"] if state else None,
                              state["last_heartbeat"] if state else None, now, gap_end)
            if result:
                status, when = result
                _set_status(conn, agent.id, agent.name, None if status == "new" else status, when, now, "tick")
                changed += 1
            elif state is not None and state["name"] != agent.name:
                conn.execute("UPDATE agents SET name = ? WHERE id = ?", (agent.name, agent.id))  # a rename in agents.yaml
                changed += 1
        db.set_meta(conn, "alive_ts", repr(now))
        if changed:
            db.bump_rev(conn)
    return changed


def tick_task(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    try:
        agents = load_agents()
    except ConfigError:
        return  # the web service already logs a broken agents.yaml
    update_statuses(conn, agents, now)


def purge_old(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM agent_status_log WHERE ts < ?", (now - cfg.retention.status_days * 86400,))


def gap_end(conn: sqlite3.Connection) -> float:
    return float(db.get_meta(conn, "gap_end_ts") or 0)


def timeline(conn: sqlite3.Connection, agent_id: str, start: float, now: float,
             pending: Optional[tuple] = None) -> list:
    """Status spans [[start, end, status], ...] for the window, oldest first.

    Time before the agent's first logged status is left out (nothing was known).
    """
    rows = [(r["ts"], r["status"]) for r in conn.execute(
        "SELECT ts, status FROM agent_status_log WHERE agent_id = ? AND ts <= ? ORDER BY ts, id", (agent_id, now))]
    if pending and pending[0] in LOGGED and (not rows or pending[1] >= rows[-1][0]):
        rows.append((pending[1], pending[0]))
    spans = []
    for i, (ts, status) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else now
        begin, finish = max(ts, start), min(end, now)
        if finish > begin:
            spans.append([begin, finish, status])
    return spans


def uptime(spans: list) -> Optional[float]:
    """Share of known up/down time that was up; 'unknown' and 'disabled' time does not count."""
    up = sum(e - s for s, e, st in spans if st == "up")
    down = sum(e - s for s, e, st in spans if st == "down")
    return up / (up + down) if up + down else None
