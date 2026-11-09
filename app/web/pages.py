"""HTML pages. They are shells; data arrives from the /api/ui endpoints."""

from flask import Blueprint, current_app, render_template

from .. import settings

bp = Blueprint("pages", __name__)


@bp.get("/")
def overview():
    cfg = current_app.config["JABS_CONFIG"]
    return render_template("overview.html", badges=cfg.badges, version=settings.VERSION, page="overview")
