"""Flask application factory for the read-only UI and the agent API."""

import time
from typing import Optional

from flask import Flask, jsonify

from .. import db, settings
from ..config import Config, load_config


def create_app(cfg: Optional[Config] = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["JABS_CONFIG"] = cfg

    @app.get("/healthz")
    def healthz():
        """200 while the database is readable; reports how long ago the timer last ran."""
        try:
            conn = db.connect()
            try:
                last_tick = db.get_meta(conn, "last_tick_ts")
            finally:
                conn.close()
        except Exception as exc:  # pylint: disable=broad-except
            app.logger.error("healthz database check failed: %s", exc)
            return jsonify({"status": "error"}), 503
        tick_age = None if last_tick is None else round(time.time() - float(last_tick))
        return jsonify({"status": "ok", "version": settings.VERSION, "tick_age_s": tick_age})

    return app
