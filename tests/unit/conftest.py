"""Fixtures for fast unit tests that never touch the real install."""

import shutil
from pathlib import Path

import pytest

from app import settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway install directory with the example config and empty paths for the rest."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.example.yaml", config_dir / "config.yaml")
    monkeypatch.setattr(settings, "BASE_DIR", tmp_path)
    monkeypatch.setattr(settings, "CONFIG_PATH", config_dir / "config.yaml")
    monkeypatch.setattr(settings, "AGENTS_PATH", config_dir / "agents.yaml")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "data" / "jabs.sqlite")
    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "data" / "backups")
    monkeypatch.setattr(settings, "ENV_PATH", tmp_path / ".env")
    return tmp_path
