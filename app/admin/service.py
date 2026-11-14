"""Reading and controlling the systemd units that run the dashboard."""

import os
import shutil
import subprocess
from typing import Optional

from .. import settings

PROPERTIES = ("ActiveState", "SubState", "UnitFileState", "ActiveEnterTimestamp", "NextElapseUSecRealtime", "Result")
ACTIONS = ("start", "stop", "restart")


def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)


def available() -> bool:
    """True on a host running systemd."""
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def _show(unit: str, *properties: str) -> dict:
    args = ["systemctl", "show", unit, "--no-pager"]
    for prop in properties:
        args += ["-p", prop]
    try:
        result = _run(args)
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (line.partition("=") for line in result.stdout.splitlines())
    return {key: value for key, _, value in pairs if key}


def detect_base(override: Optional[str] = None) -> Optional[str]:
    """The unit name this install uses (e.g. 'jabs-dashboard-v2'), from `override` or by looking for the
    jabs-dashboard* service whose WorkingDirectory is this directory."""
    if override:
        return override.removesuffix(".service")
    if not available():
        return None
    try:
        listing = _run(["systemctl", "list-unit-files", "jabs-dashboard*.service", "--no-legend", "--no-pager"])
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.split()[0].removesuffix(".service") for line in listing.stdout.splitlines() if line.strip()]
    names = [name for name in names if not name.endswith("-tick")]
    for name in names:
        if _show(f"{name}.service", "WorkingDirectory").get("WorkingDirectory") == str(settings.BASE_DIR):
            return name
    return names[0] if len(names) == 1 else None


def unit_names(base: str) -> list:
    """[(label, unit)] for the web service, the tick service and the tick timer."""
    return [("Web service", f"{base}.service"), ("Tick service", f"{base}-tick.service"),
            ("Tick timer", f"{base}-tick.timer")]


def unit_status(unit: str) -> dict:
    """{'active', 'enabled', 'since', 'next'} as text for one unit; '-' when unknown."""
    props = _show(unit, *PROPERTIES)
    if not props:
        return {"active": "unknown", "enabled": "-", "since": "-", "next": "-"}
    active = props.get("ActiveState", "unknown")
    if props.get("SubState") and props["SubState"] != active:
        active = f"{active} ({props['SubState']})"
    if props.get("Result") not in (None, "", "success"):
        active += f", last result {props['Result']}"
    return {"active": active, "enabled": props.get("UnitFileState") or "-",
            "since": props.get("ActiveEnterTimestamp") or "-", "next": props.get("NextElapseUSecRealtime") or "-"}


def control_command(action: str, unit: str) -> list:
    """The command that changes a unit's state; sudo is added for non-root users."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action '{action}'")
    command = ["systemctl", action, unit]
    return command if os.geteuid() == 0 else ["sudo", *command]


def journal_command(unit: str) -> list:
    return ["journalctl", "-u", unit, "-f", "-n", "200", "-o", "short-iso", "--no-pager"]
