"""An agent's up/down state from its configuration and last contact."""

from typing import Optional

from .config import AgentConfig


def agent_status(agent: AgentConfig, last_heartbeat: Optional[float], now: float) -> str:
    """One of 'disabled', 'new' (never reported), 'up', or 'down' (silent longer than its grace period)."""
    if not agent.enabled:
        return "disabled"
    if last_heartbeat is None:
        return "new"
    return "up" if now - last_heartbeat <= agent.grace_minutes * 60 else "down"
