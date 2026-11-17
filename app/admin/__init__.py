"""Interactive administration console (`python -m app admin`). Textual is imported only when it runs."""

import os
import pwd
import sys
from typing import Optional

from .. import settings

# Every command-line subcommand must be doable in the console: "subcommand" -> the action id a tab registers in its
# ACTIONS. tests/unit/test_admin_parity.py fails when a subcommand is added without an entry here.
COVERAGE = {
    "initdb": "initdb",
    "config": "files.validate",
    "web": "web",
    "tick": "tick",
    "testmail": "testmail",
    "backup": "backup",
    "restore": "restore",
    "vacuum": "vacuum",
    "check-db": "check-db",
    "meta": "meta",
    "reset": "reset",
    "runs delete": "runs.delete",
    "runs list": "runs.list",
    "storage purge": "storage.purge",
    "agent add": "agents.add",
    "agent list": "agents.list",
    "agent set": "agents.set",
    "agent rename": "agents.rename",
    "agent rekey": "agents.rekey",
    "agent enable": "agents.enable",
    "agent disable": "agents.disable",
    "agent remove": "agents.remove",
}
# Subcommands that are not administration tasks.
NOT_COVERED = {"admin"}


def check_user() -> Optional[str]:
    """A message when running as this user would damage the install, else None."""
    if os.geteuid() == 0:
        try:
            owner = settings.DATA_DIR.stat().st_uid
        except OSError:
            return None
        if owner != 0:
            try:
                name = pwd.getpwuid(owner).pw_name
            except KeyError:
                name = str(owner)
            return ("Run the console as the user that owns the install, not as root: files created as root can stop "
                    f"the dashboard from writing its own database.\n  sudo -u {name} {sys.executable} -m app admin")
        return None
    if settings.DB_PATH.exists() and not os.access(settings.DB_PATH, os.W_OK):
        return (f"{settings.DB_PATH} is not writable by this user. Run the console as the user that owns the install "
                "(the one the service runs as).")
    return None


def run(unit: Optional[str] = None) -> int:
    try:
        import textual  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        print("The console needs the 'textual' package: .venv/bin/pip install -r requirements.txt", file=sys.stderr)
        return 1
    problem = check_user()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    from . import logs  # pylint: disable=import-outside-toplevel
    from .app import AdminApp  # pylint: disable=import-outside-toplevel

    capture = logs.install()
    AdminApp(unit, capture).run()
    return 0
