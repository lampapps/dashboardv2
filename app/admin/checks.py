"""State and validation of the files an administrator edits: config.yaml, agents.yaml and .env."""

import stat
from pathlib import Path
from typing import Optional

from .. import config, settings

EXAMPLE_NAME = "config.example.yaml"


def managed_files() -> list:
    """The editable files in display order; paths are read at call time."""
    return [
        {"key": "config", "label": "config.yaml", "path": settings.CONFIG_PATH, "live": False,
         "note": "dashboard settings; restart the service after changing"},
        {"key": "agents", "label": "agents.yaml", "path": settings.AGENTS_PATH, "live": True,
         "note": "agent registry; changes apply without a restart"},
        {"key": "env", "label": ".env", "path": settings.ENV_PATH, "live": False,
         "note": "secrets (SMTP, AWS); restart the service after changing"},
    ]


def mode_text(path: Path) -> str:
    try:
        return format(stat.S_IMODE(path.stat().st_mode), "04o")
    except OSError:
        return "-"


def check_file(entry: dict) -> dict:
    """{'state': 'ok'|'warn'|'error'|'missing', 'message': str} for one managed file."""
    path = entry["path"]
    if not path.exists():
        hint = " (no agents registered yet)" if entry["key"] == "agents" else ""
        return {"state": "missing", "message": f"{path} does not exist{hint}"}
    try:
        if entry["key"] == "config":
            cfg = config.load_config(path)
            message = f"valid (timezone {cfg.timezone}, {len(cfg.badges)} badges)"
        elif entry["key"] == "agents":
            message = f"valid ({len(config.load_agents(path))} agents)"
        else:
            message = "present"
    except config.ConfigError as exc:
        return {"state": "error", "message": str(exc)}
    except OSError as exc:
        return {"state": "error", "message": f"cannot read {path}: {exc}"}
    if entry["key"] in ("env", "agents") and path.stat().st_mode & 0o007:
        return {"state": "warn", "message": f"{message}; readable by all users (chmod 640 or 600)"}
    if entry["key"] == "env" and path.stat().st_mode & 0o070:
        return {"state": "warn", "message": f"{message}; readable by the group (chmod 600 is safer)"}
    return {"state": "ok", "message": message}


def starter_text(entry: dict) -> Optional[str]:
    """Text a missing file should start with (config.yaml starts as a copy of the example)."""
    if entry["key"] != "config":
        return None
    example = settings.CONFIG_PATH.parent / EXAMPLE_NAME
    return example.read_text(encoding="utf-8") if example.exists() else None


def snapshot(path: Path) -> Optional[bytes]:
    """The file's bytes before an edit, or None if it did not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def revert(path: Path, before: Optional[bytes]) -> None:
    """Put the file back as it was (in place, so its owner and mode stay)."""
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)
