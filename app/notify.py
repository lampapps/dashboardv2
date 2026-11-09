"""Email notifications driven by the tick: offline-agent alerts and the periodic digest."""

import logging
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from . import activity, db, mailer, status
from .config import Config, ConfigError, load_agents

logger = logging.getLogger(__name__)

DIGEST_LAST_SENT = "digest_last_sent"
MAX_DIGEST_PROBLEMS = 50
MAX_ERROR_CHARS = 200


def _fmt(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M %Z")


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} days {hours % 24} h"


# ---------------------------------------------------------------- offline alerts

def _send_offline_alert(conn: sqlite3.Connection, cfg: Config, agent, state: sqlite3.Row, now: float) -> None:
    alert = cfg.email.offline_alert
    count = state["alert_count"]
    if alert.max_alerts and count >= alert.max_alerts:
        return
    if count and now - (state["last_alert_at"] or 0) < alert.repeat_interval_minutes * 60:
        return
    tz = ZoneInfo(cfg.timezone)
    last_contact = state["last_heartbeat"]
    text, html = mailer.render(
        "offline", agent=agent.name, notes=agent.notes, grace_minutes=agent.grace_minutes,
        last_contact=_fmt(last_contact, tz) if last_contact else "never",
        silent_for=_duration(now - last_contact) if last_contact else "an unknown time",
        down_since=_fmt(state["status_since"], tz), alert_number=count + 1, max_alerts=alert.max_alerts,
        recovery=alert.send_recovery, generated=_fmt(now, tz))
    if mailer.send(cfg.email, f"JABS alert: agent {agent.name} is offline", text, html):
        with db.transaction(conn):
            conn.execute("UPDATE agent_state SET alert_count = alert_count + 1, last_alert_at = ? WHERE name = ?",
                         (now, agent.name))


def _send_recovery(conn: sqlite3.Connection, cfg: Config, name: str, state: sqlite3.Row, now: float) -> None:
    tz = ZoneInfo(cfg.timezone)
    went_down = conn.execute("SELECT ts FROM agent_status_log WHERE agent = ? AND status = 'down' "
                             "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    back_at = state["status_since"] or now
    down_ts = went_down["ts"] if went_down else None
    text, html = mailer.render(
        "recovered", agent=name, generated=_fmt(now, tz), back_at=_fmt(back_at, tz),
        down_since=_fmt(down_ts, tz) if down_ts else "unknown",
        down_for=_duration(back_at - down_ts) if down_ts else "unknown")
    mailer.send(cfg.email, f"JABS: agent {name} is back", text, html)


def offline_alerts(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    """Email once an agent is logged down (repeating per config), and again when it recovers."""
    alert = cfg.email.offline_alert
    if not alert.enabled or not mailer.is_configured(cfg.email):
        return
    try:
        agents = load_agents()
    except ConfigError:
        return  # the web service already logs a broken agents.yaml
    states = {row["name"]: row for row in conn.execute(
        "SELECT name, status, status_since, last_heartbeat, alert_count, last_alert_at FROM agent_state")}
    for agent in agents:
        state = states.get(agent.name)
        if state is None:
            continue
        if state["status"] == "down" and agent.offline_alerts:
            _send_offline_alert(conn, cfg, agent, state, now)
        elif state["status"] in ("up", "disabled") and state["alert_count"]:
            if state["status"] == "up" and alert.send_recovery:
                _send_recovery(conn, cfg, agent.name, state, now)
            with db.transaction(conn):
                conn.execute("UPDATE agent_state SET alert_count = 0, last_alert_at = NULL WHERE name = ?",
                             (agent.name,))


# ---------------------------------------------------------------- digest

def digest_data(conn: sqlite3.Connection, cfg: Config, agent_names: list, since: float, now: float) -> dict:
    """What happened between `since` and `now`: finished runs, problem runs and agent outages."""
    tz = ZoneInfo(cfg.timezone)
    totals = {b: 0 for b in ("success", "failed", "stopped", "other")}
    per_agent: dict = {}
    problems = []
    rows = conn.execute(
        "SELECT agent, job_name, target_label, status, completed_at, error_message FROM runs "
        "WHERE completed_at >= ? AND completed_at <= ? ORDER BY completed_at DESC", (since, now)).fetchall()
    for row in rows:
        bucket = activity.bucket(row["status"])
        if bucket == "running":
            continue
        totals[bucket] += 1
        per_agent.setdefault(row["agent"], {"agent": row["agent"], **{b: 0 for b in totals}})[bucket] += 1
        if bucket in ("failed", "stopped"):
            problems.append({
                "when": _fmt(row["completed_at"], tz), "agent": row["agent"], "job": row["job_name"],
                "target": row["target_label"], "status": row["status"],
                "error": " ".join((row["error_message"] or "").split())[:MAX_ERROR_CHARS]})

    outages = []
    for name in agent_names:
        state = conn.execute("SELECT status FROM agent_state WHERE name = ?", (name,)).fetchone()
        for start, end, kind in status.timeline(conn, name, since, now):
            if kind != "down":
                continue
            ongoing = end >= now and state is not None and state["status"] == "down"
            outages.append({"agent": name, "start": _fmt(start, tz), "began_before": start <= since,
                            "end": _fmt(end, tz), "ongoing": ongoing, "duration": _duration(end - start),
                            "sort": start})
    outages.sort(key=lambda o: o["sort"])
    return {
        "period_start": _fmt(since, tz), "period_end": _fmt(now, tz), "generated": _fmt(now, tz),
        "totals": totals, "per_agent": sorted(per_agent.values(), key=lambda a: a["agent"]),
        "problems": problems[:MAX_DIGEST_PROBLEMS], "problems_more": max(0, len(problems) - MAX_DIGEST_PROBLEMS),
        "outages": outages,
    }


def _digest_subject(data: dict, now: float, tz: ZoneInfo) -> str:
    totals = data["totals"]
    subject = (f"JABS digest {datetime.fromtimestamp(now, tz):%Y-%m-%d}: "
               f"{totals['success']} ok, {totals['failed']} failed, {totals['stopped']} stopped")
    down = sum(1 for o in data["outages"] if o["ongoing"])
    return subject + (f", {down} agent{'s' if down != 1 else ''} offline" if down else "")


def send_digest(conn: sqlite3.Connection, cfg: Config, since: float, now: float) -> Optional[bool]:
    """Send the digest for the period; None when there was nothing to report."""
    try:
        names = [agent.name for agent in load_agents()]
    except ConfigError:
        names = []
    data = digest_data(conn, cfg, names, since, now)
    if not (data["problems"] or data["outages"] or any(data["totals"].values())):
        return None
    text, html = mailer.render("digest", **data)
    return mailer.send(cfg.email, _digest_subject(data, now, ZoneInfo(cfg.timezone)), text, html)


def digest_task(conn: sqlite3.Connection, cfg: Config, now: float) -> None:
    """Send the digest once its cron schedule has fired since the last one."""
    digest = cfg.email.digest
    if not digest.enabled or not mailer.is_configured(cfg.email):
        return
    last_sent = db.get_meta(conn, DIGEST_LAST_SENT)
    if last_sent is None:  # the first period starts now rather than reporting history nobody asked for
        with db.transaction(conn):
            db.set_meta(conn, DIGEST_LAST_SENT, repr(now))
        return
    since = float(last_sent)
    next_fire = croniter(digest.schedule, datetime.fromtimestamp(since, ZoneInfo(cfg.timezone))).get_next(datetime)
    if next_fire.timestamp() > now:
        return
    result = send_digest(conn, cfg, since, now)
    if result is None:
        logger.info("digest: nothing to report since %s", _fmt(since, ZoneInfo(cfg.timezone)))
    if result is not False:  # a failed send is retried on the next tick
        with db.transaction(conn):
            db.set_meta(conn, DIGEST_LAST_SENT, repr(now))
