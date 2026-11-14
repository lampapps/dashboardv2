"""The admin console application: tabs, shared dialogs and helpers, and the base class of every tab."""

import asyncio
import getpass
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Footer, Header, TabbedContent, TabPane

from .. import config, db, settings
from . import dialogs, editor, service
from .logs import Capture


class Pane(Vertical):
    """One tab. `ACTIONS` maps console action ids to the methods that perform them (see admin.COVERAGE)."""

    TAB_ID = ""
    TAB_TITLE = ""
    ACTIONS: dict = {}

    async def reload(self) -> None:
        """Refresh what the tab shows; called when it is opened and on 'r'."""

    def focus_primary(self) -> None:
        """Put the keyboard on the tab's main widget so its key bindings show in the footer."""
        for widget in (*self.query(DataTable), *self.query(Button)):
            widget.focus()
            return

    @property
    def admin(self) -> "AdminApp":
        return self.app  # type: ignore[return-value]


def _run_visible(command: list) -> int:
    print(f"$ {' '.join(command)}")
    code = subprocess.run(command, check=False).returncode
    try:
        input("\nPress Enter to return to the console... ")
    except EOFError:
        pass
    return code


class AdminApp(App):
    TITLE = "JABS admin"
    CSS_PATH = "admin.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_pane", "Refresh"),
        Binding("1", "tab('agents')", "Agents", show=False),
        Binding("2", "tab('files')", "Files", show=False),
        Binding("3", "tab('database')", "Database", show=False),
        Binding("4", "tab('diagnostics')", "Diagnostics", show=False),
        Binding("5", "tab('service')", "Service", show=False),
        Binding("6", "tab('docs')", "Docs", show=False),
    ]

    def __init__(self, unit: Optional[str] = None, capture: Optional[Capture] = None):
        super().__init__()
        self.unit = unit
        self.capture = capture or Capture()
        self.unit_base: Optional[str] = None
        self._unit_checked = False

    # ---------------------------------------------------------------- layout

    def panes(self) -> list:
        from .panes import PANES  # late: panes import this module

        return PANES

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="agents"):
            for pane in self.panes():
                with TabPane(f"{pane.TAB_TITLE}", id=pane.TAB_ID):
                    yield pane()
        yield Footer()

    def on_mount(self) -> None:
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = "?"
        self.sub_title = f"{user} · {settings.BASE_DIR}"

    def active_pane(self) -> Optional[Pane]:
        pane = self.query_one(TabbedContent).active_pane
        return pane.query(Pane).first() if pane is not None else None

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane.query(Pane).first() if event.pane is not None else None
        if pane is not None:
            pane.focus_primary()
            self.spawn(pane.reload)

    def action_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh_pane(self) -> None:
        pane = self.active_pane()
        if pane is not None:
            self.spawn(pane.reload)

    # ---------------------------------------------------------------- running work

    def spawn(self, func: Callable[..., Awaitable], *args: Any) -> None:
        """Run an async action in a worker (where dialogs can be awaited); problems show in an error dialog."""
        async def runner() -> None:
            try:
                await func(*args)
            except (config.ConfigError, db.DbError, editor.EditorError, ValueError) as exc:
                await self.error(str(exc))
            except (OSError, sqlite3.Error) as exc:
                await self.error(f"{type(exc).__name__}: {exc}")

        self.run_worker(runner(), group="actions", exit_on_error=True)

    def run_external(self, func: Callable[[], Any]) -> Any:
        """Hand the terminal to a program (an editor, sudo) and come back afterwards."""
        with self.suspend():
            return func()

    async def run_editor(self, path: Path) -> int:
        return self.run_external(lambda: editor.run_editor(path))

    async def run_systemctl(self, action: str, unit: str) -> int:
        command = service.control_command(action, unit)
        return self.run_external(lambda: _run_visible(command))

    # ---------------------------------------------------------------- dialogs (await inside spawn)

    async def confirm(self, title: str, message: str, **options: Any) -> tuple:
        return await self.push_screen_wait(dialogs.ConfirmDialog(title, message, **options))

    async def show(self, title: str, body: str, copy_text: Optional[str] = None, error: bool = False) -> None:
        await self.push_screen_wait(dialogs.MessageDialog(title, body, copy_text, error))

    async def error(self, message: str) -> None:
        await self.show("Error", message, error=True)

    async def choose(self, title: str, message: str, choices: list) -> Any:
        return await self.push_screen_wait(dialogs.ChoiceDialog(title, message, choices))

    async def pick(self, title: str, options: list, custom_label: Optional[str] = None) -> Any:
        return await self.push_screen_wait(dialogs.PickDialog(title, options, custom_label))

    async def form(self, title: str, fields: list, submit: Callable[[dict], Any], save_label: str = "Save") -> Any:
        return await self.push_screen_wait(dialogs.FormDialog(title, fields, submit, save_label))

    async def multipick(self, title: str, columns: list, rows: list, action: str, **options: Any) -> Any:
        return await self.push_screen_wait(dialogs.MultiPickDialog(title, columns, rows, action, **options))

    # ---------------------------------------------------------------- service helpers

    async def service_base(self) -> Optional[str]:
        """The systemd unit base name for this install, looked up once."""
        if not self._unit_checked:
            self.unit_base = await asyncio.to_thread(service.detect_base, self.unit)
            self._unit_checked = True
        return self.unit_base

    async def restart_service(self) -> None:
        base = await self.service_base()
        if base is None:
            await self.show("Restart the service", "No systemd unit for this install was found. Restart it yourself, "
                            "or start the console with --unit NAME.")
            return
        code = await self.run_systemctl("restart", f"{base}.service")
        await self.show("Restart the service", f"systemctl restart {base}.service "
                        + ("succeeded." if code == 0 else f"exited with status {code}."), error=code != 0)
