"""Agent registry backed by agents.yaml, reloaded when the file changes."""

import logging
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
                logger.error("agents.yaml not reloaded, keeping previous agents: %s", exc)
                self._signature = signature
                return
            self._by_hash = {agent.key_hash: agent for agent in agents}
            self._signature = signature

    def find_by_key(self, key: str) -> Optional[AgentConfig]:
        self._refresh()
        return self._by_hash.get(hash_key(key))

    def agents(self) -> list:
        self._refresh()
        return list(self._by_hash.values())
