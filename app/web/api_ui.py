"""Read-only JSON endpoints the web UI polls."""

import time
from contextlib import closing
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify

from .. import db, jobs

bp = Blueprint("ui", __name__, url_prefix="/api/ui")


@bp.get("/jobs")
def list_jobs():
    tz = ZoneInfo(current_app.config["JABS_CONFIG"].timezone)
    with closing(db.connect()) as conn:
        return jsonify({"data": jobs.job_rollups(conn, tz, time.time())})
