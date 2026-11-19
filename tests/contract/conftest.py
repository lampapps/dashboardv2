"""Fixtures for the agent-ingest contract tests.

The suite is black-box: it starts the real app under gunicorn with a throwaway JABS_HOME and talks to it over HTTP
exactly like an agent does, then checks what the dashboard stored.
"""

import uuid
from pathlib import Path

import pytest

from tests.contract.backends import Agent, V2Backend

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def backend():
    instance = V2Backend(REPO_ROOT)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def agent(backend: V2Backend) -> Agent:
    return backend.register_agent()


@pytest.fixture
def other_agent(backend: V2Backend) -> Agent:
    return backend.register_agent()


def new_run_id() -> str:
    return uuid.uuid4().hex


class Reporter:
    """Posts events, purge reports and job reports for one agent, like an agent's dashboard client."""

    def __init__(self, backend: V2Backend, agent: Agent):
        self.backend = backend
        self.agent = agent

    def event(self, **fields):
        return self.backend.post("/api/v2/events", fields, key=self.agent.key)

    def heartbeat(self, **fields):
        return self.backend.post("/api/v2/heartbeat", fields, key=self.agent.key)

    def jobs(self, jobs: list, **fields):
        return self.backend.post("/api/v2/jobs", {"jobs": jobs, **fields}, key=self.agent.key)

    def start(self, run_id: str = None, **extra):
        run_id = run_id or new_run_id()
        fields = {
            "event_type": "heartbeat", "message": "Starting", "stage": "Starting backup",
            "run_id": run_id, "job_id": "J", "job_name": "J", "backup_type": "full",
            "target_id": "T", "target_label": "T label",
        }
        fields.update(extra)
        resp = self.event(**fields)
        assert resp.status_code == 201, resp.text
        return run_id

    def complete(self, run_id: str, event_type: str = "backup_complete", **extra):
        fields = {
            "event_type": event_type, "message": "Done", "run_id": run_id,
            "job_id": "J", "job_name": "J", "backup_type": "full", "target_id": "T",
            "duration_seconds": 1.5,
        }
        fields.update(extra)
        resp = self.event(**fields)
        assert resp.status_code == 201, resp.text
        return resp

    def purge(self, **fields):
        return self.backend.post("/api/v2/purge", fields, key=self.agent.key)

    def run(self, run_id: str) -> dict:
        matches = [r for r in self.backend.runs(self.agent) if r["run_id"] == run_id]
        assert len(matches) == 1, f"expected one run for {run_id}, got {len(matches)}"
        return matches[0]


@pytest.fixture
def reporter(backend: V2Backend, agent: Agent) -> Reporter:
    return Reporter(backend, agent)


@pytest.fixture
def other_reporter(backend: V2Backend, other_agent: Agent) -> Reporter:
    return Reporter(backend, other_agent)
