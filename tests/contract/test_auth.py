"""Authentication and request-shape contract for the agent endpoints."""

import pytest

PATHS = ["/api/v2/events", "/api/v2/heartbeat", "/api/v2/purge", "/api/v2/jobs", "/api/v2/storage"]
BODY = {"job_id": "J", "target_id": "T", "external_ids": ["x"], "version": "1", "jobs": [],
        "samples": [{"name": "v", "used_bytes": 1}]}


@pytest.mark.parametrize("path", PATHS)
class TestAuth:
    def test_missing_key_is_401(self, backend, path):
        resp = backend.post(path, BODY)
        assert resp.status_code == 401
        assert "error" in resp.json()

    def test_unknown_key_is_403(self, backend, path):
        resp = backend.post(path, BODY, key="not-a-real-key")
        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_disabled_agent_is_403_and_not_recorded(self, backend, agent, path):
        backend.set_enabled(agent, False)
        resp = backend.post(path, BODY, key=agent.key)
        assert resp.status_code == 403
        assert backend.agent_info(agent)["last_heartbeat"] is None

    def test_reenabled_agent_works_again(self, backend, agent, path):
        backend.set_enabled(agent, False)
        backend.set_enabled(agent, True)
        resp = backend.post(path, BODY, key=agent.key)
        assert resp.status_code in (200, 201)


def test_the_old_endpoints_do_not_exist(backend, agent):
    for path in ("/api/monitoring/events", "/api/monitoring/target-purged"):
        assert backend.post(path, BODY, key=agent.key).status_code == 404


def test_malformed_json_is_rejected(backend, agent):
    resp = backend.post("/api/v2/events", data="{not json", key=agent.key)
    assert resp.status_code == 400


def test_non_json_content_type_is_rejected(backend, agent):
    resp = backend.post("/api/v2/events", data="x=1", key=agent.key, content_type="text/plain")
    assert resp.status_code in (400, 415)


def test_json_array_body_is_400(backend, agent):
    resp = backend.post("/api/v2/events", [1, 2], key=agent.key)
    assert resp.status_code == 400


def test_null_optional_field_values_are_treated_as_missing(backend, agent):
    resp = backend.post(
        "/api/v2/events",
        {"version": "1.0", "job_id": "J", "target_id": "T", "job_name": None, "message": None},
        key=agent.key,
    )
    assert resp.status_code == 201


@pytest.mark.parametrize("body", [{}, {"job_id": "J"}, {"target_id": "T"}, {"job_id": None, "target_id": "T"}])
def test_events_need_a_job_id_and_a_target_id(backend, agent, body):
    resp = backend.post("/api/v2/events", body, key=agent.key)
    assert resp.status_code == 400
    assert "error" in resp.json()
