"""Modal dialogs for the admin console. Text shown here may come from files or agents, so markup is off."""

import asyncio
from typing import Any, Callable, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import config, db
from . import clipboard


def _print_plain(text: str) -> None:
    """Show text on the real terminal, where the terminal's own mouse selection and copy work."""
    print(f"\n{text}\n")
    try:
        input("Select and copy it with your terminal, then press Enter to return to the console... ")
    except EOFError:
        pass


class _Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(self.cancel_value())

    def cancel_value(self) -> Any:
        return None


class ConfirmDialog(_Dialog):
    """Yes/no question. Returns (confirmed, option_checked); `type_word` makes the user type a word to enable Yes."""

    def __init__(self, title: str, message: str, confirm: str = "Yes", danger: bool = False,
                 type_word: Optional[str] = None, option: Optional[str] = None):
        super().__init__()
        self._title, self._message, self._confirm = title, message, confirm
        self._danger, self._word, self._option = danger, type_word, option

    def cancel_value(self) -> tuple:
        return (False, False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._message, markup=False, classes="dialog-body")
            if self._word:
                yield Label(f"Type {self._word} to confirm:")
                yield Input(id="word", placeholder=self._word)
            if self._option:
                yield Checkbox(self._option, id="option")
            with Horizontal(classes="buttons"):
                yield Button(self._confirm, id="ok", variant="error" if self._danger else "primary",
                             disabled=bool(self._word))
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#word" if self._word else "#cancel").focus()

    @on(Input.Changed, "#word")
    def _word_changed(self, event: Input.Changed) -> None:
        self.query_one("#ok", Button).disabled = event.value.strip() != self._word

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            option = self.query_one("#option", Checkbox).value if self._option else False
            self.dismiss((True, option))
        else:
            self.dismiss((False, False))


class MessageDialog(_Dialog):
    """Read-only text (output, errors, a key shown once). `copy_text` adds Copy and Show in terminal buttons."""

    def __init__(self, title: str, body: str, copy_text: Optional[str] = None, error: bool = False):
        super().__init__()
        self._title, self._body, self._copy, self._error = title, body, copy_text, error

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide" + (" error" if self._error else "")):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self._body, markup=False, classes="dialog-body mono")
            with Horizontal(classes="buttons"):
                if self._copy is not None:
                    yield Button("Copy", id="copy")
                    yield Button("Show in terminal", id="plain")
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close").focus()

    @on(Button.Pressed, "#copy")
    def _copy_pressed(self) -> None:
        text = self._copy or ""
        tool = clipboard.copy_with_tool(text)
        self.app.copy_to_clipboard(text)  # also the terminal's own clipboard (OSC 52), which not every terminal has
        if tool:
            self.notify(f"Copied to the clipboard ({tool})")
        else:
            self.notify("Sent to the terminal's clipboard. If pasting gives nothing, use Show in terminal.",
                        severity="warning", timeout=10)

    @on(Button.Pressed, "#plain")
    def _plain_pressed(self) -> None:
        with self.app.suspend():
            _print_plain(self._copy or "")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)


class ChoiceDialog(_Dialog):
    """A question with several buttons; returns the chosen value (None on Escape)."""

    def __init__(self, title: str, message: str, choices: list):
        super().__init__()
        self._title, self._message, self._choices = title, message, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self._message, markup=False, classes="dialog-body")
            with Horizontal(classes="buttons"):
                for index, (label, _value) in enumerate(self._choices):
                    yield Button(label, id=f"choice-{index}", variant="primary" if index == 0 else "default")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._choices[int(event.button.id.split("-")[1])][1])


class PickDialog(_Dialog):
    """Choose one of several options; returns its value. With `custom_label`, a free-text entry is offered too."""

    def __init__(self, title: str, options: list, custom_label: Optional[str] = None):
        super().__init__()
        self._title, self._options, self._custom = title, options, custom_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            yield OptionList(*[Option(label, id=str(i)) for i, (label, _v) in enumerate(self._options)],
                             markup=False, id="options")
            if self._custom:
                yield Label(self._custom)
                yield Input(id="custom")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        options = self.query_one("#options", OptionList)
        if self._options:
            options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._options[int(event.option_id)][1])

    @on(Input.Submitted, "#custom")
    def _custom_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class MultiPickDialog(_Dialog):
    """Mark any number of rows (Space or Enter toggles, a = all, n = none), then act on them.

    `rows` are (key, cells); cell text is shown literally. Returns the marked keys in row order, or None when cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"), Binding("space", "toggle_mark", "Mark"),
        Binding("a", "mark_all", "All"), Binding("n", "mark_none", "None"),
    ]

    def __init__(self, title: str, columns: list, rows: list, action: str, note: str = "", danger: bool = False):
        super().__init__()
        self._title, self._columns, self._rows = title, columns, rows
        self._action, self._note, self._danger = action, note, danger
        self._marked: set = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog huge"):
            yield Label(self._title, classes="dialog-title")
            if self._note:
                yield Static(self._note, markup=False, classes="help")
            yield DataTable(id="rows", cursor_type="row", zebra_stripes=True)
            yield Static("", id="count", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(self._action, id="ok", variant="error" if self._danger else "primary", disabled=True)
                yield Button("Mark all", id="all")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.add_column("", key="mark", width=3)
        for index, name in enumerate(self._columns):
            table.add_column(name, key=f"c{index}")
        for key, cells in self._rows:
            table.add_row("( )", *[Text(str(cell)) for cell in cells], key=str(key))
        table.focus()
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#rows", DataTable)
        for key, _cells in self._rows:
            table.update_cell(str(key), "mark", "(x)" if str(key) in self._marked else "( )")
        self.query_one("#count", Static).update(f"{len(self._marked)} of {len(self._rows)} marked")
        self.query_one("#ok", Button).disabled = not self._marked

    def _current(self) -> Optional[str]:
        table = self.query_one("#rows", DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_toggle_mark(self) -> None:
        key = self._current()
        if key is not None:
            self._marked.symmetric_difference_update({key})
            self._refresh()

    def action_mark_all(self) -> None:
        self._marked = {str(key) for key, _cells in self._rows}
        self._refresh()

    def action_mark_none(self) -> None:
        self._marked = set()
        self._refresh()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_toggle_mark()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss([key for key, _cells in self._rows if str(key) in self._marked])
        elif event.button.id == "all":
            self.action_mark_all()
        else:
            self.dismiss(None)


class FormDialog(_Dialog):
    """A form. `fields` are dicts: key, label, kind ('text'|'int'|'bool'|'password'), value, help.

    `submit(values)` runs in a thread; a ConfigError/DbError/ValueError it raises is shown in the form, which stays
    open. The dialog returns submit's result, or None when cancelled.
    """

    def __init__(self, title: str, fields: list, submit: Callable[[dict], Any], save_label: str = "Save"):
        super().__init__()
        self._title, self._fields, self._submit, self._save = title, fields, submit, save_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                for field in self._fields:
                    if field["kind"] == "bool":
                        yield Checkbox(field["label"], bool(field.get("value")), id=f"f-{field['key']}")
                    else:
                        yield Label(field["label"])
                        yield Input(str(field.get("value") if field.get("value") is not None else ""),
                                    password=field["kind"] == "password", id=f"f-{field['key']}")
                    if field.get("help"):
                        yield Static(field["help"], markup=False, classes="help")
            yield Static("", id="error", markup=False, classes="error-text")
            with Horizontal(classes="buttons"):
                yield Button(self._save, id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        first = self.query("Input")
        if first:
            first.first().focus()

    def _values(self) -> dict:
        values = {}
        for field in self._fields:
            widget = self.query_one(f"#f-{field['key']}")
            values[field["key"]] = widget.value if field["kind"] == "bool" else widget.value.strip()
        return values

    async def _save_now(self) -> None:
        error = self.query_one("#error", Static)
        values = self._values()
        for field in self._fields:
            if field["kind"] == "int":
                try:
                    values[field["key"]] = int(values[field["key"]])
                except ValueError:
                    error.update(f"{field['label']} must be a whole number")
                    return
        try:
            result = await asyncio.to_thread(self._submit, values)
        except (config.ConfigError, db.DbError, ValueError, OSError) as exc:
            error.update(str(exc))
            return
        self.dismiss(result)

    @on(Input.Submitted)
    async def _submitted(self) -> None:
        await self._save_now()

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            await self._save_now()
        else:
            self.dismiss(None)
