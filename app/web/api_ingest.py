"""Agent-facing endpoints. These are the only routes that write; they require an API key."""

import logging
import socket
import sqlite3
import time
from contextlib import closing
from typing import Optional

from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError

from .. import db, ingest, logs, settings, storage
from ..agents import AgentRegistry
from ..config import AgentConfig

logger = logging.getLogger(__name__)
bp = Blueprint("ingest", __name__)


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _authenticate() -> tuple:
    """Return (agent, None) or (None, error_response)."""
    key = request.headers.get("X-API-Key", "").strip()
    if not key:
        return None, _error("Missing API key (X-API-Key header)", 401)
    registry: AgentRegistry = current_app.extensions["jabs_agents"]
    agent: Optional[AgentConfig] = registry.find_by_key(key)
    if agent is None:
        return None, _error("Invalid API key", 403)
    if not agent.enabled:
        return None, _error(f"Agent '{agent.name}' is disabled", 403)
    if agent.allowed_ip and request.remote_addr != agent.allowed_ip:
        return None, _error("API key not accepted from this address", 403)
    return agent, None


def _parse(model):
    """Return (payload, None) or (None, error_response) for the JSON request body."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, _error("Request body must be a JSON object", 400)
    try:
        return model.model_validate(body), None
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return None, _error(f"Invalid value for '{field}': {first['msg']}", 400)


@bp.post("/api/monitoring/events")
def submit_event():
    agent, error = _authenticate()
    if error:
        return error
    payload, error = _parse(ingest.EventPayload)
    if error:
        return error
    try:
        with closing(db.connect()) as conn:
            return jsonify(ingest.record_event(conn, agent.name, payload, time.time())), 201
    except sqlite3.Error:
        logger.exception("failed to record event from agent %s", agent.name)
        return _error("Failed to process event", 500)


@bp.post("/api/monitoring/target-purged")
def target_purged():
    agent, error = _authenticate()
    if error:
        return error
    payload, error = _parse(ingest.PurgePayload)
    if error:
        return error
    if not payload.target_id:
        return _error("Missing required field: target_id", 400)
    if not payload.clean_external_ids():
        return _error("Missing required field: external_ids (non-empty list)", 400)
    try:
        with closing(db.connect()) as conn:
            return jsonify(ingest.record_purge(conn, agent.name, payload, time.time())), 200
    except sqlite3.Error:
        logger.exception("failed to record purge from agent %s", agent.name)
        return _error("Failed to record job target purge", 500)


@bp.post("/api/v2/storage")
def submit_storage():
    """Agent-reported storage usage (a NAS volume, a restic repository); opt-in per agent."""
    agent, error = _authenticate()
    if error:
        return error
    payload, error = _parse(storage.AgentStoragePayload)
    if error:
        return error
    now = time.time()
    try:
        with closing(db.connect()) as conn:
            ingest.touch_agent(conn, agent.name, now)
            stored = storage.store_samples(conn, storage.agent_samples(agent.name, payload, now))
    except sqlite3.Error:
        logger.exception("failed to record storage from agent %s", agent.name)
        return _error("Failed to record storage samples", 500)
    return jsonify({"success": True, "stored": stored}), 201


@bp.post("/api/v2/logs")
def submit_logs():
    """Agent log tails; an optional batch_id makes a retried post harmless. Opt-in per agent."""
    agent, error = _authenticate()
    if error:
        return error
    payload, error = _parse(logs.LogBatch)
    if error:
        return error
    cfg = current_app.config["JABS_CONFIG"]
    now = time.time()
    try:
        with closing(db.connect()) as conn:
            with db.transaction(conn):
                if ingest.touch_agent(conn, agent.name, now):
                    db.bump_rev(conn)
            result = logs.store_batch(conn, cfg, agent.name, payload, now)
    except logs.LogError as exc:
        return _error(str(exc), 400)
    except sqlite3.Error:
        logger.exception("failed to record logs from agent %s", agent.name)
        return _error("Failed to record log lines", 500)
    return jsonify(result), (200 if result.get("duplicate") else 201)


@bp.get("/api/heartbeat")
def heartbeat():
    """Health check polled by Uptime Kuma and other monitors; same shape as v1."""
    with closing(db.connect()) as conn:
        errors = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'error'").fetchone()[0]
    return jsonify({
        "hostname": socket.gethostname(),
        "version": settings.VERSION,
        "status": "ok",
        "error_event_count": errors,
    })
