"""Paths and constants. Tunables live in config/config.yaml, secrets in .env."""

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv


def read_version(path: Path) -> str:
    """The MAJOR.MINOR.PATCH number in a VERSION file, or 'unknown' when it is missing or malformed."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        logging.getLogger(__name__).warning("%s is missing or not MAJOR.MINOR.PATCH; version is unknown", path)
        return "unknown"
    return text


# The code's own file, not under JABS_HOME.
VERSION = read_version(Path(__file__).resolve().parent.parent / "VERSION")

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


def smtp_login() -> tuple:
    """(username, password) for the SMTP server. The older JABS_SMTP_USER / JABS_SMTP_PASS are still accepted."""
    user = os.environ.get("JABS_SMTP_USERNAME") or os.environ.get("JABS_SMTP_USER") or ""
    password = os.environ.get("JABS_SMTP_PASSWORD") or os.environ.get("JABS_SMTP_PASS") or ""
    return user, password
