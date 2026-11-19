"""Backend adapter for the agent-ingest contract tests.

Writes go over HTTP exactly as an agent would send them to the /api/v2 endpoints. State is read from the
harness-owned SQLite file, so the tests check what the dashboard really stored.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

import requests

from app import config as v2_config


class Agent(NamedTuple):
    name: str
    key: str
    id: str


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class V2Backend:
    """Runs this repo's app under gunicorn with its own JABS_HOME, config and database."""

    name = "v2"
    TIMEZONE = "Asia/Kolkata"  # fixed offset with minutes, so a UTC-only implementation fails
    base_url = ""

    def __init__(self, repo_root: Path):
        self._repo_root = repo_root
        self._home = Path(tempfile.mkdtemp(prefix="jabs-v2-contract-"))
        self._proc: Optional[subprocess.Popen] = None
        self._log = None
        self._agents_path = self._home / "config" / "agents.yaml"

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        (self._home / "config").mkdir()
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        (self._home / "config" / "config.yaml").write_text(
            f"timezone: {self.TIMEZONE}\nserver:\n  host: 127.0.0.1\n  port: {port}\n"
            "  workers: 2\n  threads: 2\n"
        )
        env = {**os.environ, "JABS_HOME": str(self._home)}
        self._log = open(self._home / "server.log", "wb")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app", "web"], cwd=self._repo_root, env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + 30
        while time.time() < deadline and self._proc.poll() is None:
            try:
                if requests.get(self.base_url + "/healthz", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.2)
        tail = (self._home / "server.log").read_text(errors="replace")[-2000:]
        raise RuntimeError(f"test instance did not start:\n{tail}")

    def stop(self) -> None:
        import shutil

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._log:
            self._log.close()
        shutil.rmtree(self._home, ignore_errors=True)

    # ------------------------------------------------------------------ HTTP

    def post(self, path: str, payload=None, key: Optional[str] = None,
             data: Optional[str] = None, content_type: str = "application/json"):
        headers = {"Content-Type": content_type}
        if key is not None:
            headers["X-API-Key"] = key
        body = data if data is not None else (None if payload is None else json.dumps(payload))
        return requests.post(self.base_url + path, data=body, headers=headers, timeout=10)

    def get(self, path: str):
        return requests.get(self.base_url + path, timeout=10)

    # ------------------------------------------------------------------ registry

    def register_agent(self) -> Agent:
        name = f"agent-{uuid.uuid4().hex[:8]}"
        key = v2_config.add_agent(name, path=self._agents_path)
        return Agent(name=name, key=key, id=v2_config.find_agent(name, self._agents_path).id)

    def set_enabled(self, agent: Agent, enabled: bool) -> None:
        v2_config.set_agent_enabled(agent.name, enabled, path=self._agents_path)

    def rename(self, agent: Agent, new_name: str) -> Agent:
        v2_config.rename_agent(agent.name, new_name, path=self._agents_path)
        return agent._replace(name=new_name)

    # ------------------------------------------------------------------ stored state

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._home / 'data' / 'jabs.sqlite'}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def runs(self, agent: Agent) -> list:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM run_view WHERE agent_id = ? ORDER BY id", (agent.id,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, run_id: str) -> list:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT e.* FROM events e JOIN runs r ON r.id = e.run_pk WHERE r.run_id = ? ORDER BY e.id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def agent_info(self, agent: Agent) -> dict:
        with self._db() as conn:
            row = conn.execute(
                "SELECT name, version, agent_type, last_heartbeat FROM agents WHERE id = ?", (agent.id,)
            ).fetchone()
        return dict(row) if row else {"name": None, "version": None, "agent_type": None, "last_heartbeat": None}

    def jobs(self, agent: Agent, active_only: bool = True) -> dict:
        """{job_id: {"name", "active", "targets": {target_id: label}}} as stored for the agent."""
        with self._db() as conn:
            rows = conn.execute("SELECT id, job_key, name, active FROM jobs WHERE agent_id = ?", (agent.id,)).fetchall()
            found = {}
            for row in rows:
                if active_only and not row["active"]:
                    continue
                targets = {t["target_key"]: t["label"] for t in conn.execute(
                    "SELECT target_key, label FROM targets WHERE job_id = ? AND (? = 0 OR active = 1)",
                    (row["id"], int(active_only)))}
                found[row["job_key"]] = {"name": row["name"], "active": bool(row["active"]), "targets": targets}
        return found

    def schedules(self, agent: Agent) -> dict:
        """{job_id: [(cron, enabled), ...]} as stored for the agent."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT j.job_key, s.cron, s.enabled FROM job_schedules s JOIN jobs j ON j.id = s.job_id "
                "WHERE j.agent_id = ? ORDER BY j.job_key, s.cron", (agent.id,)).fetchall()
        found: dict = {}
        for row in rows:
            found.setdefault(row["job_key"], []).append((row["cron"], bool(row["enabled"])))
        return found

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.TIMEZONE))

    def rollups(self, agent: Agent) -> list:
        data = self.get("/api/ui/jobs").json()["data"]
        return [
            {
                "job_id": row["job_key"],
                "job_name": row["job_name"],
                "last_status": row["last_status"],
                "status_counts": row["status_counts"],
                "has_next_event": row["next_event_at"] is not None,
                "schedule_drifted": row["schedule_drifted"],
            }
            for row in data
            if row["agent_id"] == agent.id
        ]
