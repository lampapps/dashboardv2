"""HTML pages. They are shells; data arrives from the /api/ui endpoints."""

import re

from flask import Blueprint, abort, current_app, render_template

from .. import settings
from ..config import AGENT_ID_PATTERN

bp = Blueprint("pages", __name__)
_AGENT_ID = re.compile(AGENT_ID_PATTERN)


@bp.get("/")
def overview():
    cfg = current_app.config["JABS_CONFIG"]
    return render_template("overview.html", badges=cfg.badges, version=settings.VERSION, page="overview")


@bp.get("/agents")
def agents():
    return render_template("agents.html", version=settings.VERSION, page="agents")


def _find(agent_id: str):
    found = current_app.extensions["jabs_agents"].find_by_id(agent_id) if _AGENT_ID.match(agent_id) else None
    if found is None:
        abort(404)
    return found


@bp.get("/agents/<agent_id>")
def agent(agent_id: str):
    found = _find(agent_id)
    return render_template("agent.html", version=settings.VERSION, page="agents", agent_id=found.id, agent_name=found.name)


@bp.get("/agents/<agent_id>/jobs/<job_key>")
def job(agent_id: str, job_key: str):
    found = _find(agent_id)
    return render_template("job.html", version=settings.VERSION, page="agents", agent_id=found.id,
                           agent_name=found.name, job_key=job_key)
