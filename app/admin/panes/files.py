"""Files tab: edit config.yaml, agents.yaml and .env in the system editor, then validate."""

import asyncio
import datetime
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..app import Pane
from ..checks import check_file, managed_files, mode_text, revert, snapshot, starter_text
from ..editor import EditorError
from ..logs import audit

STATE_TEXT = {"ok": "OK", "warn": "WARNING", "error": "INVALID", "missing": "missing"}


class FilesPane(Pane):
    TAB_ID = "files"
    TAB_TITLE = "2 Files"
    ACTIONS = {"files.validate": "do_validate", "files.edit": "do_edit"}
    BINDINGS = [Binding("e", "edit", "Edit in editor"), Binding("v", "validate", "Validate all")]

    def __init__(self) -> None:
        super().__init__()
        self._results: dict = {}

    def compose(self) -> ComposeResult:
        yield Static("Files are edited in your system editor ($VISUAL, $EDITOR or the system default) and checked "
                     "when you leave it.", markup=False, classes="note")
        yield DataTable(id="files-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="files-detail", markup=False, classes="detail")
        with Horizontal(classes="buttons"):
            yield Button("Edit", id="edit", variant="primary")
            yield Button("Validate all", id="validate")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("File", "State", "Mode", "Modified", "Applies")

    def _selected(self) -> Optional[dict]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((e for e in managed_files() if e["key"] == key), None)

    async def reload(self) -> None:
        entries = managed_files()
        self._results = {e["key"]: await asyncio.to_thread(check_file, e) for e in entries}
        table = self.query_one(DataTable)
        keep = table.cursor_row
        table.clear()
        for entry in entries:
            path = entry["path"]
            modified = (datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        if path.exists() else "-")
            table.add_row(entry["label"], STATE_TEXT[self._results[entry["key"]]["state"]], mode_text(path), modified,
                          "immediately" if entry["live"] else "after a restart", key=entry["key"])
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))
        self._show_detail()

    def _show_detail(self) -> None:
        entry = self._selected()
        detail = self.query_one("#files-detail", Static)
        if entry is None:
            detail.update("")
            return
        result = self._results.get(entry["key"], {"message": ""})
        detail.update(f"{entry['path']}\n{entry['note']}\n\n{result['message']}")

    @on(DataTable.RowHighlighted)
    def _highlighted(self) -> None:
        self._show_detail()

    # ---------------------------------------------------------------- actions

    async def do_validate(self) -> None:
        await self.reload()
        lines = [f"{e['label']}: {STATE_TEXT[self._results[e['key']]['state']]} - {self._results[e['key']]['message']}"
                 for e in managed_files()]
        bad = any(r["state"] == "error" for r in self._results.values())
        await self.admin.show("Validation", "\n\n".join(lines), error=bad)

    async def do_edit(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        path = entry["path"]
        before = snapshot(path)
        created = False
        if before is None:
            starter = starter_text(entry)
            path.parent.mkdir(parents=True, exist_ok=True)
            if starter is not None:
                path.write_text(starter, encoding="utf-8")
                created = True
        while True:
            try:
                await self.admin.run_editor(path)
            except EditorError as exc:
                if created:
                    revert(path, before)
                await self.admin.error(str(exc))
                await self.reload()
                return
            result = await asyncio.to_thread(check_file, entry)
            if result["state"] != "error":
                break
            choice = await self.admin.choose(
                f"{entry['label']} is not valid", result["message"],
                [("Edit again", "again"), ("Revert my changes", "revert"), ("Keep it anyway", "keep")])
            if choice == "again":
                continue
            if choice == "keep":
                break
            revert(path, before)
            audit("file edit reverted", file=entry["label"])
            await self.reload()
            await self.admin.show("Reverted", f"{path} was put back as it was before the edit.")
            return
        after = snapshot(path)
        if entry["key"] == "env" and before is None and path.exists():
            path.chmod(0o600)
        await self.reload()
        if after == before:
            return
        audit("file edited", file=entry["label"])
        if not entry["live"]:
            ok, _ = await self.admin.confirm(
                f"{entry['label']} changed",
                "The dashboard reads this file at start-up. Restart the service now to apply the change?",
                confirm="Restart now")
            if ok:
                await self.admin.restart_service()

    def action_edit(self) -> None:
        self.admin.spawn(self.do_edit)

    def action_validate(self) -> None:
        self.admin.spawn(self.do_validate)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "edit":
            self.action_edit()
        else:
            self.action_validate()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_edit()
