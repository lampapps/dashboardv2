"""Read-only JSON endpoints the web UI polls."""

import hashlib
import re
import time
from contextlib import closing
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, request

from .. import agents_report, db, jobs, logs, overview, runs, storage
from ..agents import resolve_hosts

bp = Blueprint("ui", __name__, url_prefix="/api/ui")

MAX_PARAM_LENGTH = 200


def _hosts(conn) -> dict:
    return resolve_hosts(conn, current_app.extensions["jabs_agents"].agents())


def _respond(conn, now: float, key: str, build: Callable[[], dict], rev_key: str = "rev") -> Response:
    """JSON from `build()`, or 304 while the data revision and the minute are unchanged.

    The minute keeps relative times and agent up/down fresh without any write.
    """
    etag = '"%s-%d-%s"' % (db.get_meta(conn, rev_key, "0"), now // 60, hashlib.sha1(key.encode()).hexdigest()[:10])
    response = Response(status=304) if request.headers.get("If-None-Match") == etag else jsonify(build())
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return response


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _text_arg(name: str) -> Optional[str]:
    value = request.args.get(name)
    return value if value is not None and len(value) <= MAX_PARAM_LENGTH else None


@bp.get("/jobs")
def list_jobs():
    tz = ZoneInfo(current_app.config["JABS_CONFIG"].timezone)
    with closing(db.connect()) as conn:
        return jsonify({"data": jobs.job_rollups(conn, tz, time.time(), _hosts(conn))})


@bp.get("/overview")
def overview_data():
    """Everything the Overview page shows."""
    cfg = current_app.config["JABS_CONFIG"]
    days = overview.clamp_days(request.args.get("days", type=int))
    now = time.time()

    def build() -> dict:
        payload = overview.build(conn, cfg, current_app.extensions["jabs_agents"].agents(), now, days)
        last_tick = db.get_meta(conn, "last_tick_ts")
        payload["tick_age_s"] = None if last_tick is None else round(now - float(last_tick))
        return payload

    with closing(db.connect()) as conn:
        return _respond(conn, now, f"overview {days}", build)


@bp.get("/agents")
def agents_data():
    """Per-agent status, up/down history and configuration; never includes keys or key hashes."""
    cfg = current_app.config["JABS_CONFIG"]
    range_key, _ = storage.range_seconds(request.args.get("range"))
    now = time.time()
    with closing(db.connect()) as conn:
        return _respond(conn, now, f"agents {range_key}", lambda: agents_report.build(
            conn, cfg, current_app.extensions["jabs_agents"].agents(), now, range_key))


@bp.get("/agents/<name>")
def agent_data(name: str):
    """One agent's card plus its activity chart, totals and job rows."""
    cfg = current_app.config["JABS_CONFIG"]
    agent = next((a for a in current_app.extensions["jabs_agents"].agents() if a.name == name), None)
    if agent is None:
        return _error("Unknown agent", 404)
    range_key, _ = storage.range_seconds(request.args.get("range"))
    now = time.time()
    with closing(db.connect()) as conn:
        return _respond(conn, now, f"agent {name} {range_key}", lambda: agents_report.detail(
            conn, cfg, agent, now, range_key))


@bp.get("/storage")
def storage_data():
    """Current usage and history for the Overview's storage charts."""
    cfg = current_app.config["JABS_CONFIG"]
    range_key, _ = storage.range_seconds(request.args.get("range"))
    now = time.time()
    with closing(db.connect()) as conn:
        return _respond(conn, now, f"storage {range_key}", lambda: {
            **storage.history(conn, cfg, now, range_key), "hosts": _hosts(conn)})


@bp.get("/job")
def job_data():
    """Summary, targets and duration trend for one job (agent + name)."""
    agent, name = _text_arg("agent"), _text_arg("name")
    if not agent or name is None:
        return _error("agent and name are required", 400)
    cfg = current_app.config["JABS_CONFIG"]
    now = time.time()
    with closing(db.connect()) as conn:
        summary = runs.job_summary(conn, cfg, now, agent, name, _hosts(conn))
        if summary is None:
            return _error("Unknown job", 404)
        return _respond(conn, now, f"job {agent} {name}", lambda: summary)


@bp.get("/job/runs")
def job_runs():
    """A page of a job's runs, newest first, with optional status, target and age filters."""
    agent, name = _text_arg("agent"), _text_arg("name")
    if not agent or name is None:
        return _error("agent and name are required", 400)
    limit = request.args.get("limit", runs.DEFAULT_PAGE_SIZE, type=int)
    offset = request.args.get("offset", 0, type=int)
    status, target = _text_arg("status"), _text_arg("target")
    since_key = request.args.get("since")
    now = time.time()
    since = now - storage.RANGES[since_key] if since_key in storage.RANGES else None
    key = f"runs {agent} {name} {limit} {offset} {status} {target} {since_key}"
    with closing(db.connect()) as conn:
        return _respond(conn, now, key, lambda: runs.list_runs(conn, agent, name, limit, offset, status, target, since))


@bp.get("/runs/<int:run_id>")
def run_data(run_id: int):
    """One run with its events."""
    now = time.time()
    with closing(db.connect()) as conn:
        detail = runs.run_detail(conn, run_id)
        if detail is None:
            return _error("Unknown run", 404)
        detail["run"]["host"] = _hosts(conn).get(detail["run"]["agent"], detail["run"]["agent"])
        return _respond(conn, now, f"run {run_id}", lambda: detail)


@bp.get("/logs")
def logs_list():
    """Every stored agent log with its size, time span and per-level counts."""
    now = time.time()
    cfg = current_app.config["JABS_CONFIG"]
    with closing(db.connect()) as conn:
        def build() -> dict:
            hosts = _hosts(conn)
            return {"logs": [{**entry, "host": hosts.get(entry["agent"], entry["agent"])} for entry in logs.list_logs(conn)],
                    "now": now, "timezone": cfg.timezone}
        return _respond(conn, now, "logs", build, rev_key="log_rev")


def _optional_int(name: str) -> Optional[int]:
    return request.args.get(name, type=int)


@bp.get("/logs/lines")
def logs_lines():
    """Lines of one log, oldest first: the newest page, the lines after a cursor, or the page before one."""
    agent, log = _text_arg("agent"), _text_arg("log")
    if not agent or not log:
        return _error("agent and log are required", 400)
    levels = [lv for lv in (request.args.get("levels") or "").split(",") if lv]
    query = _text_arg("q") or None
    with closing(db.connect()) as conn:
        page = logs.read_lines(
            conn, agent, log, limit=request.args.get("limit", logs.DEFAULT_PAGE, type=int),
            after=_optional_int("after"), before=_optional_int("before"), levels=levels, query=query)
    response = jsonify(page)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/logs/download")
def logs_download():
    """The whole stored log as a plain-text attachment."""
    agent, log = _text_arg("agent"), _text_arg("log")
    if not agent or not log:
        return _error("agent and log are required", 400)
    with closing(db.connect()) as conn:
        text = logs.export_text(conn, agent, log)
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", f"{agent}-{log}")
    return Response(text, mimetype="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{filename}.log"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
