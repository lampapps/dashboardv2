"""Agent registry backed by agents.yaml, reloaded when the file changes."""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

from . import settings
from .config import AgentConfig, ConfigError, hash_key, load_agents

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self, path: Optional[Union[str, Path]] = None):
        self._path = Path(path or settings.AGENTS_PATH)
        self._lock = threading.Lock()
        self._signature: Optional[tuple] = None
        self._by_hash: dict = {}
        self.error: Optional[str] = None  # why the file could not be loaded, None while it is fine

    def _current_signature(self) -> Optional[tuple]:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        signature = self._current_signature()
        with self._lock:
            if signature == self._signature:
                return
            try:
                agents = load_agents(self._path)
            except ConfigError as exc:
                # Keep serving the last good registry; remember the signature so we log once.
                self.error = str(exc)
                self._signature = signature
                if self._by_hash:
                    logger.error("agents.yaml not reloaded, keeping the previous agents: %s", exc)
                else:
                    logger.error("agents.yaml is invalid and no agents are loaded, so every agent key is rejected "
                                 "until it is fixed (check it with `python -m app config`): %s", exc)
                return
            self._by_hash = {agent.key_hash: agent for agent in agents}
            self._signature = signature
            if self.error:
                logger.info("agents.yaml is valid again: %d agent(s) loaded", len(agents))
            self.error = None

    def find_by_key(self, key: str) -> Optional[AgentConfig]:
        self._refresh()
        return self._by_hash.get(hash_key(key))

    def find_by_id(self, agent_id: str) -> Optional[AgentConfig]:
        self._refresh()
        return next((a for a in self._by_hash.values() if a.id == agent_id), None)

    def agents(self) -> list:
        self._refresh()
        return list(self._by_hash.values())


def resolve_hosts(conn: sqlite3.Connection, agents: list) -> dict:
    """Host shown for each agent id: what the agent reported, else its agents.yaml host, else its name."""
    reported = {row["id"]: row["hostname"] for row in conn.execute(
        "SELECT id, hostname FROM agents WHERE hostname IS NOT NULL")}
    return {agent.id: reported.get(agent.id) or agent.host or agent.name for agent in agents}


def host_source(reported: Optional[str], agent) -> str:
    """Where an agent's displayed host comes from: reported, configured or agent (its own name)."""
    return "reported" if reported else "configured" if agent.host else "agent"
