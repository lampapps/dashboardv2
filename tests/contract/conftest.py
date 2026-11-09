"""Fixtures for the agent-ingest contract tests.

Select the target with JABS_CONTRACT_BACKEND (default: v1). For v1, the copy is
made from JABS_V1_DIR (default: ../dashboard) and run with JABS_V1_PYTHON
(default: <JABS_V1_DIR>/venv/bin/python). The real v1 instance and its DB are
never touched.
"""

import os
import uuid
from pathlib import Path

import pytest

from tests.contract.backends import Agent, Backend, V1Backend

TARGET = os.environ.get("JABS_CONTRACT_BACKEND", "v1")
REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items):
    if TARGET != "v1":
        return
    for item in items:
        marker = item.get_closest_marker("v1_bug")
        if marker:
            reason = marker.args[0] if marker.args else "known v1 bug"
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))


@pytest.fixture(scope="session")
def backend():
    if TARGET != "v1":
        pytest.skip(f"no contract adapter for backend '{TARGET}' yet")
    v1_dir = Path(os.environ.get("JABS_V1_DIR", REPO_ROOT.parent / "dashboard")).resolve()
    python = Path(os.environ.get("JABS_V1_PYTHON", v1_dir / "venv" / "bin" / "python"))
    if not python.exists():
        pytest.exit(f"v1 interpreter not found: {python} (set JABS_V1_PYTHON)", returncode=2)
    instance = V1Backend(v1_dir, python)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def agent(backend: Backend) -> Agent:
    return backend.register_agent()


@pytest.fixture
def other_agent(backend: Backend) -> Agent:
    return backend.register_agent()


def new_run_id() -> str:
    return uuid.uuid4().hex


class Reporter:
    """Posts events and purge reports for one agent, like an agent's monitoring client."""

    def __init__(self, backend: Backend, agent: Agent):
        self.backend = backend
        self.agent = agent

    def event(self, **fields):
        return self.backend.post("/api/monitoring/events", fields, key=self.agent.key)

    def start(self, run_id: str = None, **extra):
        run_id = run_id or new_run_id()
        fields = {
            "event_type": "heartbeat", "message": "Starting", "stage": "Starting backup",
            "run_id": run_id, "job_name": "J", "backup_type": "full",
            "target_id": "T", "target_label": "T label",
        }
        fields.update(extra)
        resp = self.event(**fields)
        assert resp.status_code == 201, resp.text
        return run_id

    def complete(self, run_id: str, event_type: str = "backup_complete", **extra):
        fields = {
            "event_type": event_type, "message": "Done", "run_id": run_id,
            "job_name": "J", "backup_type": "full", "target_id": "T",
            "duration_seconds": 1.5,
        }
        fields.update(extra)
        resp = self.event(**fields)
        assert resp.status_code == 201, resp.text
        return resp

    def purge(self, **fields):
        return self.backend.post("/api/monitoring/target-purged", fields, key=self.agent.key)

    def run(self, run_id: str) -> dict:
        matches = [r for r in self.backend.runs(self.agent) if r["run_id"] == run_id]
        assert len(matches) == 1, f"expected one run for {run_id}, got {len(matches)}"
        return matches[0]


@pytest.fixture
def reporter(backend: Backend, agent: Agent) -> Reporter:
    return Reporter(backend, agent)


@pytest.fixture
def other_reporter(backend: Backend, other_agent: Agent) -> Reporter:
    return Reporter(backend, other_agent)
