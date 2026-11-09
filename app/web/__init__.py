"""Flask application factory for the read-only UI and the agent API."""

import time
from contextlib import closing
from typing import Optional

from flask import Flask, jsonify

from .. import db, settings
from ..agents import AgentRegistry
from ..config import Config, load_config

# Agent payloads are small JSON; anything larger is a mistake or abuse.
MAX_REQUEST_BYTES = 1024 * 1024


def create_app(cfg: Optional[Config] = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["JABS_CONFIG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.extensions["jabs_agents"] = AgentRegistry()

    from . import api_ingest, api_ui, pages

    app.register_blueprint(api_ingest.bp)
    app.register_blueprint(api_ui.bp)
    app.register_blueprint(pages.bp)

    @app.get("/healthz")
    def healthz():
        """200 while the database is readable; reports how long ago the timer last ran."""
        try:
            with closing(db.connect()) as conn:
                last_tick = db.get_meta(conn, "last_tick_ts")
        except Exception as exc:  # pylint: disable=broad-except
            app.logger.error("healthz database check failed: %s", exc)
            return jsonify({"status": "error"}), 503
        tick_age = None if last_tick is None else round(time.time() - float(last_tick))
        return jsonify({"status": "ok", "version": settings.VERSION, "tick_age_s": tick_age})

    return app
