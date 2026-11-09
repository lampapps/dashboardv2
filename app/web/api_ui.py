"""Read-only JSON endpoints the web UI polls."""

import time
from contextlib import closing
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, request

from .. import db, jobs, overview, storage

bp = Blueprint("ui", __name__, url_prefix="/api/ui")


@bp.get("/jobs")
def list_jobs():
    tz = ZoneInfo(current_app.config["JABS_CONFIG"].timezone)
    with closing(db.connect()) as conn:
        return jsonify({"data": jobs.job_rollups(conn, tz, time.time())})


@bp.get("/overview")
def overview_data():
    """Everything the Overview page shows. Answers 304 until data changes or the minute rolls over."""
    cfg = current_app.config["JABS_CONFIG"]
    days = overview.clamp_days(request.args.get("days", type=int))
    now = time.time()
    with closing(db.connect()) as conn:
        rev = db.get_meta(conn, "rev", "0")
        # The minute bucket keeps relative times and agent up/down fresh without any write.
        etag = f'"{rev}-{int(now // 60)}-{days}"'
        if request.headers.get("If-None-Match") == etag:
            response = Response(status=304)
        else:
            configured = current_app.extensions["jabs_agents"].agents()
            payload = overview.build(conn, cfg, configured, now, days)
            last_tick = db.get_meta(conn, "last_tick_ts")
            payload["tick_age_s"] = None if last_tick is None else round(now - float(last_tick))
            response = jsonify(payload)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.get("/storage")
def storage_data():
    """Current usage and history for the Overview's storage charts."""
    cfg = current_app.config["JABS_CONFIG"]
    range_key, _ = storage.range_seconds(request.args.get("range"))
    now = time.time()
    with closing(db.connect()) as conn:
        rev = db.get_meta(conn, "rev", "0")
        etag = f'"{rev}-{int(now // 60)}-{range_key}"'
        if request.headers.get("If-None-Match") == etag:
            response = Response(status=304)
        else:
            response = jsonify(storage.history(conn, cfg, now, range_key))
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return response
