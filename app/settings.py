"""Paths and constants. Tunables live in config/config.yaml, secrets in .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

VERSION = "0.1.0"

# JABS_HOME relocates everything the app owns (used by tests and side-by-side installs).
BASE_DIR = Path(os.environ.get("JABS_HOME") or Path(__file__).resolve().parent.parent)
CONFIG_DIR = BASE_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
AGENTS_PATH = CONFIG_DIR / "agents.yaml"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "jabs.sqlite"
BACKUP_DIR = DATA_DIR / "backups"
ENV_PATH = BASE_DIR / ".env"

# Seconds between expected `tick` runs; gaps longer than twice this mean the dashboard was down.
TICK_INTERVAL_S = 300


def load_env() -> None:
    """Load .env without overriding variables already set in the environment."""
    load_dotenv(ENV_PATH)


def env_mode() -> str:
    """'development' or 'production' (default), from ENV_MODE."""
    return os.environ.get("ENV_MODE", "production")
