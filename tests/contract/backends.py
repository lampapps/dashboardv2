"""Backend adapters for the agent-ingest contract tests.

Writes go over HTTP exactly as an agent would send them. State is read from
the harness-owned SQLite file, so the same tests can target v1 today and v2
later by adding another adapter.
"""

import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import NamedTuple, Optional

import requests

_V1_COPY_IGNORE = (
    "venv", ".venv", ".git", "data", "logs", "__pycache__", ".env",
    "jabs_server.pid", ".vscode", "locks",
)

_V1_SERVER_CODE = """
import sys
sys.path.insert(0, ".")
from waitress import serve
from app import create_app
from app.models.db_core import init_db

init_db()
serve(create_app(), host="127.0.0.1", port=int(sys.argv[1]), threads=4)
"""


class Agent(NamedTuple):
    name: str
    key: str
    id: int


class Backend:
    """Common HTTP helpers; subclasses implement registration and state reads."""

    name = "base"
    base_url = ""

    def post(self, path: str, payload=None, key: Optional[str] = None,
             data: Optional[str] = None, content_type: str = "application/json"):
        headers = {"Content-Type": content_type}
        if key is not None:
            headers["X-API-Key"] = key
        body = data if data is not None else (None if payload is None else _dumps(payload))
        return requests.post(self.base_url + path, data=body, headers=headers, timeout=10)

    def get(self, path: str):
        return requests.get(self.base_url + path, timeout=10)

    def register_agent(self) -> Agent:
        raise NotImplementedError

    def set_enabled(self, agent: Agent, enabled: bool) -> None:
        raise NotImplementedError

    def runs(self, agent: Agent) -> list:
        raise NotImplementedError

    def events(self, run_id: str) -> list:
        raise NotImplementedError

    def agent_info(self, agent: Agent) -> dict:
        raise NotImplementedError

    def schedules(self, agent: Agent) -> dict:
        raise NotImplementedError

    def rollups(self, agent: Agent) -> list:
        raise NotImplementedError


def _dumps(payload) -> str:
    import json
    return json.dumps(payload)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class V1Backend(Backend):
    """Runs a throwaway copy of the v1 dashboard on a free port with its own DB."""

    name = "v1"

    def __init__(self, v1_dir: Path, python: Path):
        self._v1_dir = v1_dir
        self._python = python
        self._tmp = Path(tempfile.mkdtemp(prefix="jabs-v1-contract-"))
        self._proc: Optional[subprocess.Popen] = None
        self._log = None

    def start(self) -> None:
        import shutil

        app_dir = self._tmp / "v1"
        shutil.copytree(self._v1_dir, app_dir, ignore=shutil.ignore_patterns(*_V1_COPY_IGNORE))
        # The live global.yaml is per-install; the example is a safe, complete config.
        shutil.copy(app_dir / "config" / "global-example.yaml", app_dir / "config" / "global.yaml")

        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        self._db_path = app_dir / "data" / "jabs.sqlite"
        self._log = open(self._tmp / "server.log", "wb")
        self._proc = subprocess.Popen(
            [str(self._python), "-c", _V1_SERVER_CODE, str(port)],
            cwd=app_dir, stdout=self._log, stderr=subprocess.STDOUT,
        )
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                if requests.get(self.base_url + "/api/heartbeat", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.2)
        tail = (self._tmp / "server.log").read_text(errors="replace")[-2000:]
        raise RuntimeError(f"v1 test instance did not start:\n{tail}")

    def stop(self) -> None:
        import shutil

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._log:
            self._log.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def register_agent(self) -> Agent:
        name = f"agent-{uuid.uuid4().hex[:8]}"
        resp = requests.post(
            self.base_url + "/agents/add",
            json={"hostname": name, "ip_address": "127.0.0.1"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        return Agent(name=name, key=body["agent_key"], id=body["agent_id"])

    def set_enabled(self, agent: Agent, enabled: bool) -> None:
        resp = requests.post(
            self.base_url + f"/agents/{agent.id}/edit", json={"enabled": enabled}, timeout=10
        )
        resp.raise_for_status()

    def runs(self, agent: Agent) -> list:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM backup_jobs WHERE agent_id = ? ORDER BY id", (agent.id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def events(self, run_id: str) -> list:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT e.* FROM events e JOIN backup_jobs b ON b.id = e.backup_job_id "
                "WHERE b.run_id = ? ORDER BY e.id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def agent_info(self, agent: Agent) -> dict:
        with self._db() as conn:
            row = conn.execute(
                "SELECT agent_version AS version, agent_type, last_heartbeat, enabled "
                "FROM agents WHERE id = ?",
                (agent.id,),
            ).fetchone()
        return dict(row)

    def schedules(self, agent: Agent) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT job_name, cron_schedule FROM job_schedules WHERE agent_id = ?", (agent.id,)
            ).fetchall()
        return {r["job_name"]: r["cron_schedule"] for r in rows}

    def rollups(self, agent: Agent) -> list:
        data = self.get("/api/job_targets").json()["data"]
        return [
            {
                "job_name": row["job_name"],
                "last_status": row["last_status"],
                "status_counts": row["status_counts"],
                "has_next_event": bool(row["next_event"]),
                "schedule_drifted": row["schedule_drifted"],
            }
            for row in data
            if row["agent_id"] == agent.id
        ]
