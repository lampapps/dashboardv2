"""Authentication and request-shape contract for the agent endpoints."""

import pytest


@pytest.mark.parametrize("path", ["/api/monitoring/events", "/api/monitoring/target-purged"])
class TestAuth:
    BODY = {"target_id": "T", "external_ids": ["x"], "version": "1"}

    def test_missing_key_is_401(self, backend, path):
        resp = backend.post(path, self.BODY)
        assert resp.status_code == 401
        assert "error" in resp.json()

    def test_unknown_key_is_403(self, backend, path):
        resp = backend.post(path, self.BODY, key="not-a-real-key")
        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_disabled_agent_is_403_and_not_recorded(self, backend, agent, path):
        backend.set_enabled(agent, False)
        resp = backend.post(path, self.BODY, key=agent.key)
        assert resp.status_code == 403
        assert backend.agent_info(agent)["last_heartbeat"] is None

    def test_reenabled_agent_works_again(self, backend, agent, path):
        backend.set_enabled(agent, False)
        backend.set_enabled(agent, True)
        resp = backend.post(path, self.BODY, key=agent.key)
        assert resp.status_code in (200, 201)


def test_malformed_json_is_rejected(backend, agent):
    resp = backend.post("/api/monitoring/events", data="{not json", key=agent.key)
    assert resp.status_code == 400


def test_non_json_content_type_is_rejected(backend, agent):
    resp = backend.post("/api/monitoring/events", data="x=1", key=agent.key,
                        content_type="text/plain")
    assert resp.status_code in (400, 415)


@pytest.mark.v1_bug("v1 raises AttributeError on a non-object JSON body and returns 500")
def test_json_array_body_is_400(backend, agent):
    resp = backend.post("/api/monitoring/events", [1, 2], key=agent.key)
    assert resp.status_code == 400


@pytest.mark.v1_bug("v1 calls .strip() on null field values and returns 500")
def test_null_field_values_are_treated_as_missing(backend, agent):
    resp = backend.post(
        "/api/monitoring/events",
        {"version": "1.0", "target_id": None, "job_name": None},
        key=agent.key,
    )
    assert resp.status_code == 201
