"""HTML pages. They are shells; data arrives from the /api/ui endpoints."""

import re

from flask import Blueprint, abort, current_app, render_template

from .. import settings
from ..config import AGENT_NAME_PATTERN

bp = Blueprint("pages", __name__)
_AGENT_NAME = re.compile(AGENT_NAME_PATTERN)


@bp.get("/")
def overview():
    cfg = current_app.config["JABS_CONFIG"]
    return render_template("overview.html", badges=cfg.badges, version=settings.VERSION, page="overview")


@bp.get("/agents")
def agents():
    return render_template("agents.html", version=settings.VERSION, page="agents")


@bp.get("/agents/<name>")
def agent(name: str):
    if not _AGENT_NAME.match(name):
        abort(404)
    return render_template("agent.html", version=settings.VERSION, page="agents", agent_name=name)


@bp.get("/agents/<name>/job")
def job(name: str):
    if not _AGENT_NAME.match(name):
        abort(404)
    return render_template("job.html", version=settings.VERSION, page="agents", agent_name=name)


@bp.get("/logs")
def logs():
    return render_template("logs.html", version=settings.VERSION, page="logs")
