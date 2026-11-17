"""Agents tab: manage the registry in agents.yaml."""

import asyncio
import socket
import sqlite3
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static
from textual.widgets.data_table import RowDoesNotExist

from ... import config, maintenance, settings
from ..app import Pane
from ..logs import audit
from ..util import ago, run_db


def _states() -> dict:
    """{agent: {last_heartbeat, status}} from the database; empty when it does not exist or is too old."""
    if not settings.DB_PATH.exists():
        return {}
    try:
        return run_db(lambda conn: {row["name"]: dict(row) for row in conn.execute(
            "SELECT name, last_heartbeat, status FROM agent_state")})
    except sqlite3.Error:
        return {}


def key_text(name: str, key: str) -> tuple:
    """(dialog body, copy text) for a key shown once, with the lines to put in the agent's .env."""
    try:
        port = config.load_config().server.port
    except config.ConfigError:
        port = 5002
    lines = f"JABS_DASHBOARD_URL=http://{socket.gethostname()}:{port}\nJABS_AGENT_KEY={key}"
    body = (f"Agent '{name}'. This key is shown only once and is not stored; only its hash is kept.\n\n"
            f"Put these lines in the agent's .env:\n\n{lines}")
    return body, lines


class AgentsPane(Pane):
    TAB_ID = "agents"
    TAB_TITLE = "1 Agents"
    ACTIONS = {
        "agents.list": "reload", "agents.add": "do_add", "agents.set": "do_edit", "agents.rekey": "do_rekey",
        "agents.rename": "do_rename",
        "agents.enable": "do_toggle", "agents.disable": "do_toggle", "agents.remove": "do_remove",
    }
    BINDINGS = [
        Binding("a", "add", "Add"), Binding("e", "edit", "Edit"), Binding("t", "toggle", "Enable/disable"),
        Binding("k", "rekey", "New key"), Binding("n", "rename", "Rename"), Binding("x", "remove", "Remove"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="agents-note", markup=False, classes="note")
        yield DataTable(id="agents-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Add", id="add", variant="primary")
            yield Button("Edit", id="edit")
            yield Button("Enable / disable", id="toggle")
            yield Button("New key", id="rekey")
            yield Button("Rename", id="rename")
            yield Button("Remove", id="remove", variant="error")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Name", "Host", "State", "Grace", "Allowed IP", "Alerts", "Last contact", "Status")
        self.set_interval(15, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.admin.active_pane() is self:
            self.admin.spawn(self.reload)

    # ---------------------------------------------------------------- data

    def selected(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    async def reload(self) -> None:
        note = self.query_one("#agents-note", Static)
        try:
            agents = await asyncio.to_thread(config.load_agents)
        except config.ConfigError as exc:
            agents = []
            note.update(f"{settings.AGENTS_PATH} is invalid:\n{exc}")
        else:
            note.update(f"{len(agents)} agent(s) in {settings.AGENTS_PATH}" if agents
                        else f"No agents registered in {settings.AGENTS_PATH}. Press a to add one.")
        states = await asyncio.to_thread(_states)
        table = self.query_one(DataTable)
        keep = self.selected()
        table.clear()
        for agent in agents:
            state = states.get(agent.name, {})
            table.add_row(agent.name, agent.host or "-", "enabled" if agent.enabled else "DISABLED",
                          f"{agent.grace_minutes} min", agent.allowed_ip or "any", "on" if agent.offline_alerts else "off",
                          ago(state.get("last_heartbeat")), state.get("status") or "-", key=agent.name)
        if keep is not None:
            try:
                table.move_cursor(row=table.get_row_index(keep))
            except RowDoesNotExist:  # the row is gone after a removal
                pass

    async def _agent(self) -> Optional[config.AgentConfig]:
        name = self.selected()
        if name is None:
            await self.admin.show("No agent selected", "Select an agent in the table first (or press a to add one).")
            return None
        agents = await asyncio.to_thread(config.load_agents)
        return next((a for a in agents if a.name == name), None)

    # ---------------------------------------------------------------- actions

    async def do_add(self) -> None:
        fields = [
            {"key": "name", "label": "Name", "kind": "text", "help": "Letters, digits, . _ and -. It identifies the agent's history."},
            {"key": "host", "label": "Host", "kind": "text", "help": "The machine it runs on, shown beside its jobs. Empty: use the name."},
            {"key": "grace_minutes", "label": "Grace (minutes without contact before it counts as down)", "kind": "int", "value": 60},
            {"key": "notes", "label": "Notes", "kind": "text"},
            {"key": "allowed_ip", "label": "Allowed IP", "kind": "text", "help": "Only accept the key from this address. Empty: any."},
        ]

        def submit(values: dict) -> tuple:
            key = config.add_agent(values["name"], grace_minutes=values["grace_minutes"], notes=values["notes"],
                                   allowed_ip=values["allowed_ip"] or None, host=values["host"])
            return values["name"], key

        result = await self.admin.form("Add agent", fields, submit, "Add")
        if result is None:
            return
        name, key = result
        audit("agent added", name=name)
        body, copy = key_text(name, key)
        await self.reload()
        await self.admin.show("New API key", body, copy_text=copy)

    async def do_edit(self) -> None:
        agent = await self._agent()
        if agent is None:
            return
        fields = [
            {"key": "host", "label": "Host", "kind": "text", "value": agent.host, "help": "Empty: use the agent name."},
            {"key": "grace_minutes", "label": "Grace (minutes)", "kind": "int", "value": agent.grace_minutes},
            {"key": "notes", "label": "Notes", "kind": "text", "value": agent.notes},
            {"key": "allowed_ip", "label": "Allowed IP", "kind": "text", "value": agent.allowed_ip or "",
             "help": "Empty: accept the key from any address."},
            {"key": "offline_alerts", "label": "Send offline alert emails", "kind": "bool", "value": agent.offline_alerts},
        ]

        def submit(values: dict) -> bool:
            config.update_agent(agent.name, host=values["host"], grace_minutes=values["grace_minutes"],
                                notes=values["notes"], allowed_ip=values["allowed_ip"] or None,
                                offline_alerts=values["offline_alerts"])
            return True

        if await self.admin.form(f"Edit agent {agent.name}", fields, submit):
            audit("agent updated", name=agent.name)
            await self.reload()

    async def do_toggle(self) -> None:
        agent = await self._agent()
        if agent is None:
            return
        await asyncio.to_thread(config.set_agent_enabled, agent.name, not agent.enabled)
        audit("agent disabled" if agent.enabled else "agent enabled", name=agent.name)
        await self.reload()

    async def do_rekey(self) -> None:
        agent = await self._agent()
        if agent is None:
            return
        ok, _ = await self.admin.confirm(
            f"New key for {agent.name}",
            "The old key stops working immediately, so the agent is rejected until its .env is updated with the new key.",
            confirm="Issue new key", danger=True)
        if not ok:
            return
        key = await asyncio.to_thread(config.rekey_agent, agent.name)
        audit("agent rekeyed", name=agent.name)
        body, copy = key_text(agent.name, key)
        await self.admin.show("New API key", body, copy_text=copy)

    async def do_rename(self) -> None:
        agent = await self._agent()
        if agent is None:
            return
        fields = [{"key": "name", "label": "New name", "kind": "text", "value": agent.name,
                   "help": "Letters, digits, . _ and -. The key, settings and stored history move to the new name; "
                           "the agent needs no change."}]

        def submit(values: dict) -> str:
            maintenance.rename_agent(agent.name, values["name"])
            return values["name"]

        new_name = await self.admin.form(f"Rename agent {agent.name}", fields, submit, "Rename")
        if new_name is None:
            return
        audit("agent renamed", name=agent.name, new_name=new_name)
        await self.reload()

    async def do_remove(self) -> None:
        agent = await self._agent()
        if agent is None:
            return
        ok, purge = await self.admin.confirm(
            f"Remove {agent.name}",
            "Its key is rejected from now on. The agent's history stays in the database unless you tick the box.",
            confirm="Remove", danger=True, option="Also delete its stored runs, status and storage samples")
        if not ok:
            return
        await asyncio.to_thread(config.remove_agent, agent.name)
        deleted = None
        if purge and settings.DB_PATH.exists():
            deleted = await asyncio.to_thread(run_db, maintenance.delete_agent_history, agent.name)
        audit("agent removed", name=agent.name, history_deleted=bool(purge))
        await self.reload()
        if deleted is not None:
            await self.admin.show("History deleted", "\n".join(f"{table}: {count}" for table, count in deleted.items()))

    # ---------------------------------------------------------------- bindings and buttons

    def action_add(self) -> None:
        self.admin.spawn(self.do_add)

    def action_edit(self) -> None:
        self.admin.spawn(self.do_edit)

    def action_toggle(self) -> None:
        self.admin.spawn(self.do_toggle)

    def action_rekey(self) -> None:
        self.admin.spawn(self.do_rekey)

    def action_rename(self) -> None:
        self.admin.spawn(self.do_rename)

    def action_remove(self) -> None:
        self.admin.spawn(self.do_remove)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_edit()
