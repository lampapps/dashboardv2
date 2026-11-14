"""Service tab: state of the systemd units, start/stop/restart, and the live journal."""

import asyncio
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, RichLog, Static
from textual.widgets.data_table import RowDoesNotExist

from .. import service
from ..app import Pane
from ..logs import audit

REFRESH_S = 5


class ServicePane(Pane):
    TAB_ID = "service"
    TAB_TITLE = "5 Service"
    ACTIONS = {"service.start": "do_start", "service.stop": "do_stop", "service.restart": "do_restart",
               "service.logs": "do_logs"}
    BINDINGS = [
        Binding("u", "start", "Start"), Binding("p", "stop", "Stop"), Binding("e", "restart", "Restart"),
        Binding("l", "logs", "Follow journal"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._journal: Optional[asyncio.subprocess.Process] = None
        self._journal_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Static("", id="service-note", markup=False, classes="note")
        yield DataTable(id="units", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Start", id="start")
            yield Button("Stop", id="stop", variant="warning")
            yield Button("Restart", id="restart", variant="primary")
            yield Button("Follow journal", id="logs")
        yield RichLog(id="journal", wrap=True, markup=False, max_lines=2000)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Unit", "Name", "State", "Enabled", "Since", "Next run")
        self.set_interval(REFRESH_S, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.admin.active_pane() is self:
            self.admin.spawn(self.reload)

    async def on_unmount(self) -> None:
        await self._stop_journal()

    def _selected_unit(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#service-note", Static)
        table = self.query_one(DataTable)
        if not await asyncio.to_thread(service.available):
            table.clear()
            note.update("systemd is not available on this host.")
            return
        base = await self.admin.service_base()
        if base is None:
            table.clear()
            note.update("No systemd unit for this install was found. Start the console with --unit NAME "
                        "if it was installed under another name (see: systemctl list-unit-files 'jabs*').")
            return
        note.update(f"Units for {base}. Changing them uses sudo, which asks for your password.")
        keep = self._selected_unit()
        rows = []
        for label, unit in service.unit_names(base):
            rows.append((label, unit, await asyncio.to_thread(service.unit_status, unit)))
        table.clear()
        for label, unit, status in rows:
            table.add_row(unit, label, status["active"], status["enabled"], status["since"], status["next"], key=unit)
        if keep:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:
                pass

    # ---------------------------------------------------------------- actions

    async def _control(self, action: str) -> None:
        unit = self._selected_unit()
        if unit is None:
            await self.admin.show("No unit", "Select a unit first.")
            return
        if action != "start":
            ok, _ = await self.admin.confirm(f"{action.capitalize()} {unit}", f"Run systemctl {action} {unit}?",
                                             confirm=action.capitalize(), danger=action == "stop")
            if not ok:
                return
        code = await self.admin.run_systemctl(action, unit)
        audit(f"service {action}", unit=unit, exit=code)
        await self.reload()
        if code != 0:
            await self.admin.error(f"systemctl {action} {unit} exited with status {code}.")

    async def do_start(self) -> None:
        await self._control("start")

    async def do_stop(self) -> None:
        await self._control("stop")

    async def do_restart(self) -> None:
        await self._control("restart")

    async def _stop_journal(self) -> None:
        if self._journal_task is not None:
            self._journal_task.cancel()
            self._journal_task = None
        if self._journal is not None and self._journal.returncode is None:
            self._journal.terminate()
            await self._journal.wait()
        self._journal = None

    async def _follow(self, unit: str) -> None:
        log = self.query_one("#journal", RichLog)
        try:
            self._journal = await asyncio.create_subprocess_exec(
                *service.journal_command(unit), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            log.write(f"cannot run journalctl: {exc}")
            return
        if self._journal.stdout is None:
            return
        async for line in self._journal.stdout:
            log.write(line.decode("utf-8", "replace").rstrip())

    async def do_logs(self) -> None:
        if self._journal_task is not None:
            await self._stop_journal()
            self.notify("Stopped following the journal")
            return
        unit = self._selected_unit()
        if unit is None:
            await self.admin.show("No unit", "Select a unit first.")
            return
        log = self.query_one("#journal", RichLog)
        log.clear()
        log.write(f"--- journal of {unit} (press l to stop) ---")
        self._journal_task = asyncio.create_task(self._follow(unit))

    # ---------------------------------------------------------------- bindings and buttons

    def action_start(self) -> None:
        self.admin.spawn(self.do_start)

    def action_stop(self) -> None:
        self.admin.spawn(self.do_stop)

    def action_restart(self) -> None:
        self.admin.spawn(self.do_restart)

    def action_logs(self) -> None:
        self.admin.spawn(self.do_logs)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
