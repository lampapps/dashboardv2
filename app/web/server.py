"""Serve the app with gunicorn (production) or Flask's dev server."""

from gunicorn.app.base import BaseApplication

from .. import db, settings
from ..config import load_config
from . import create_app


class _Gunicorn(BaseApplication):
    def __init__(self, app, options: dict):
        self._app = app
        self._options = options
        super().__init__()

    def load_config(self) -> None:
        for key, value in self._options.items():
            self.cfg.set(key, value)

    def load(self):
        return self._app


def serve(dev: bool = False) -> None:
    settings.load_env()
    cfg = load_config()
    db.init_db()
    app = create_app(cfg)
    if dev:
        app.run(host=cfg.server.host, port=cfg.server.port, debug=False)
        return
    _Gunicorn(
        app,
        {
            "bind": f"{cfg.server.host}:{cfg.server.port}",
            "workers": cfg.server.workers,
            "threads": cfg.server.threads,
            "worker_class": "gthread",
            "timeout": 30,
            "accesslog": "-",
            "errorlog": "-",
        },
    ).run()
