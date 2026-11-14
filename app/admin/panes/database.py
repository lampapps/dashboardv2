"""Database tab: backups, restore, integrity, vacuum, migration, and deleting stored history."""

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ... import config, db, maintenance, settings
from ..app import Pane
from ..logs import audit
from ..util import db_state, format_bytes, run_db

RUN_LIST_LIMIT = 500


class DatabasePane(Pane):
    TAB_ID = "database"
    TAB_TITLE = "3 Database"
    ACTIONS = {
        "initdb": "do_migrate", "backup": "do_backup", "restore": "do_restore", "vacuum": "do_vacuum",
        "check-db": "do_check", "reset": "do_reset", "runs.delete": "do_delete_runs", "runs.list": "do_delete_runs",
        "storage.purge": "do_purge_storage",
    }
    BINDINGS = [
        Binding("b", "backup", "Backup"), Binding("o", "restore", "Restore"), Binding("c", "check", "Check"),
        Binding("v", "vacuum", "Vacuum"), Binding("m", "migrate", "Migrate"), Binding("d", "delete_runs", "Delete runs"),
        Binding("p", "purge_storage", "Purge storage"), Binding("z", "reset", "Reset"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._integrity = "not checked"

    def compose(self) -> ComposeResult:
        yield Static("", id="db-info", markup=False, classes="detail")
        yield DataTable(id="db-tables", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="buttons"):
            yield Button("Backup", id="backup", variant="primary")
            yield Button("Restore", id="restore")
            yield Button("Check", id="check")
            yield Button("Vacuum", id="vacuum")
            yield Button("Migrate", id="migrate")
        with Horizontal(classes="buttons"):
            yield Button("Delete runs", id="delete_runs", variant="warning")
            yield Button("Purge storage", id="purge_storage", variant="warning")
            yield Button("Reset", id="reset", variant="error")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Table", "Rows")

    async def reload(self) -> None:
        info = self.query_one("#db-info", Static)
        table = self.query_one(DataTable)
        table.clear()
        state = await asyncio.to_thread(db_state)
        lines = [f"Database: {settings.DB_PATH}"]
        if not state["exists"]:
            info.update("\n".join(lines + ["No database yet. Press m to create it."]))
            return
        if state["error"]:
            info.update("\n".join(lines + [f"Cannot read it: {state['error']}"]))
            return
        sizes = maintenance.file_sizes()
        backups = await asyncio.to_thread(maintenance.list_backups)
        lines.append(f"Schema version: {state['version']} (this code supports {state['supported']})")
        lines.append(f"Size: {format_bytes(sizes['db'])} + {format_bytes(sizes['wal'])} WAL")
        lines.append(f"Backups: {len(backups)} in {settings.BACKUP_DIR}")
        lines.append(f"Integrity: {self._integrity}")
        if state["version"] != state["supported"]:
            lines.append("The schema does not match this code."
                         + (" Press m to migrate." if state["version"] < state["supported"]
                            else " It is newer than this code; upgrade the application or restore a backup."))
        info.update("\n".join(lines))
        if state["version"] == state["supported"]:
            for name, rows in await asyncio.to_thread(run_db, maintenance.table_counts):
                table.add_row(name, f"{rows:,}")

    async def _require_current(self) -> bool:
        state = await asyncio.to_thread(db_state)
        if not state["exists"] or state["error"] or state["version"] != state["supported"]:
            await self.admin.error("The database is missing or its schema does not match this code. "
                                   "Use Migrate (m) or Restore first.")
            return False
        return True

    # ---------------------------------------------------------------- actions

    async def do_migrate(self) -> None:
        state = await asyncio.to_thread(db_state)
        if state["version"] is not None and state["version"] > state["supported"]:
            await self.admin.error(f"The database is at schema version {state['version']}, newer than this code "
                                   f"({state['supported']}). Upgrade the application or restore a backup.")
            return
        version = await asyncio.to_thread(db.init_db)
        audit("database initialised", version=version)
        await self.reload()
        await self.admin.show("Database", f"Schema is at version {version}.")

    async def do_backup(self) -> None:
        if not await self._require_current():
            return
        default = settings.BACKUP_DIR / f"jabs-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        fields = [{"key": "file", "label": "Write the backup to", "kind": "text", "value": str(default)}]

        def submit(values: dict) -> Path:
            if not values["file"]:
                raise ValueError("enter a file name")
            return db.backup(Path(values["file"]))

        path = await self.admin.form("Back up the database", fields, submit, "Back up")
        if path:
            audit("database backed up", file=path)
            await self.reload()
            await self.admin.show("Backup written", f"{path}\n{format_bytes(path.stat().st_size)}")

    async def do_restore(self) -> None:
        backups = await asyncio.to_thread(maintenance.list_backups)
        options = [(f"{p.name}   {format_bytes(size)}   {datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}", str(p))
                   for p, size, mtime in backups]
        chosen = await self.admin.pick("Restore from which backup?", options, "Or enter the path of a backup file:")
        if not chosen:
            return
        version = await asyncio.to_thread(maintenance.check_backup_file, Path(chosen))
        ok, _ = await self.admin.confirm(
            "Restore the database",
            f"Replace the live database with {chosen} (schema version {version}).\n\nThe current database is saved first "
            "as pre-restore-<time>.sqlite in the backup directory. A running dashboard sees the restored data at once.",
            confirm="Restore", danger=True, type_word="RESTORE")
        if not ok:
            return
        result = await asyncio.to_thread(maintenance.restore, Path(chosen))
        audit("database restored", file=chosen)
        self._integrity = "not checked"
        await self.reload()
        saved = f"\nPrevious database saved as {result['safety_copy']}" if result["safety_copy"] else ""
        await self.admin.show("Restored", f"Schema version {result['schema_version']}.{saved}")

    async def do_check(self) -> None:
        if not await self._require_current():
            return
        problems = await asyncio.to_thread(run_db, maintenance.integrity_problems)
        self._integrity = "ok" if not problems else f"{len(problems)} problem(s)"
        await self.reload()
        if problems:
            await self.admin.show("Integrity check", "\n".join(problems[:50]), error=True)
        else:
            self.notify("Integrity check passed")

    async def do_vacuum(self) -> None:
        if not await self._require_current():
            return
        ok, _ = await self.admin.confirm("Vacuum", "Rebuilds the database file to reclaim space. Writers wait "
                                         "briefly while it runs.", confirm="Vacuum")
        if not ok:
            return
        sizes = await asyncio.to_thread(run_db, maintenance.vacuum)
        before = sizes["before"]["db"] + sizes["before"]["wal"]
        after = sizes["after"]["db"] + sizes["after"]["wal"]
        audit("database vacuumed", before=before, after=after)
        await self.reload()
        await self.admin.show("Vacuum", f"{format_bytes(before)} -> {format_bytes(after)}")

    async def do_delete_runs(self) -> None:
        if not await self._require_current():
            return
        jobs = await asyncio.to_thread(run_db, maintenance.job_counts)
        if not jobs:
            await self.admin.show("Delete runs", "There are no stored runs.")
            return
        totals: dict = {}
        for job in jobs:
            totals[job["agent"]] = totals.get(job["agent"], 0) + job["runs"]
        options = []
        for agent, total in totals.items():
            options.append((f"{agent}: all {total} run(s)", (agent, None, total)))
            options += [(f"    {agent} / {j['job_name'] or '(unnamed)'}: {j['runs']} run(s)", (agent, j["job_name"], j["runs"]))
                        for j in jobs if j["agent"] == agent]
        choice = await self.admin.pick("Delete stored runs for...", options)
        if choice is None:
            return
        agent, job, total = choice
        if job is not None:
            what = await self.admin.choose(
                f"{agent} / {job or '(unnamed)'}", f"{total} stored run(s). Delete them all, or pick individual runs?",
                [("Choose individual runs", "pick"), (f"Delete all {total}", "all"), ("Cancel", None)])
            if what is None:
                return
            if what == "pick":
                await self._delete_chosen_runs(agent, job)
                return
        scope = f"agent '{agent}'" + (f", job '{job}'" if job is not None else " (every job)")
        ok, _ = await self.admin.confirm(
            "Delete runs", f"Delete the stored runs and events for {scope}. Per-day counts already rolled up are kept.",
            confirm="Delete", danger=True)
        if not ok:
            return
        deleted = await asyncio.to_thread(run_db, maintenance.delete_runs, agent, job)
        audit("runs deleted", agent=agent, job=job, runs=deleted)
        await self.reload()
        await self.admin.show("Delete runs", f"Deleted {deleted} run(s).")

    async def _delete_chosen_runs(self, agent: str, job: str) -> None:
        runs = await asyncio.to_thread(run_db, maintenance.list_runs, agent, job, RUN_LIST_LIMIT)
        try:
            tz = ZoneInfo((await asyncio.to_thread(config.load_config)).timezone)
        except config.ConfigError:
            tz = None
        rows = [(run["id"], (datetime.fromtimestamp(run["started_at"], tz).strftime("%Y-%m-%d %H:%M:%S"), run["status"],
                             run["target_label"] or "-",
                             "-" if run["runtime_seconds"] is None else f"{run['runtime_seconds']:.0f} s",
                             format_bytes(run["bytes_processed"] or 0), run["id"])) for run in runs]
        note = (f"Newest {RUN_LIST_LIMIT} shown." if len(runs) >= RUN_LIST_LIMIT else "") + " Space marks a run."
        ids = await self.admin.multipick(
            f"Delete runs of {agent} / {job or '(unnamed)'}", ["Started", "Status", "Target", "Took", "Size", "Id"], rows,
            "Delete marked", note=note.strip(), danger=True)
        if not ids:
            return
        ok, _ = await self.admin.confirm(
            "Delete runs", f"Delete {len(ids)} run(s) and their events? Inside the retention window the activity "
            "chart follows at the next tick; counts already rolled up for older days stay.", confirm="Delete", danger=True)
        if not ok:
            return
        deleted = await asyncio.to_thread(run_db, maintenance.delete_run_ids, ids)
        audit("runs deleted", agent=agent, job=job, run_ids=",".join(str(i) for i in ids), runs=deleted)
        await self.reload()
        await self.admin.show("Delete runs", f"Deleted {deleted} run(s).")

    async def do_purge_storage(self) -> None:
        if not await self._require_current():
            return
        series = await asyncio.to_thread(run_db, maintenance.storage_series)
        if not series:
            await self.admin.show("Purge storage samples", "There are no stored storage samples.")
            return
        options = [(f"All storage samples ({sum(s['samples'] for s in series)})", (None, None))]
        options += [(f"{s['source']} / {s['name']} ({s['kind']}): {s['samples']} sample(s)", (s["source"], s["name"]))
                    for s in series]
        choice = await self.admin.pick("Purge storage samples for...", options)
        if choice is None:
            return
        source, name = choice
        ok, _ = await self.admin.confirm("Purge storage samples", "Delete these samples; they are collected again from "
                                         "the next sampling.", confirm="Purge", danger=True)
        if not ok:
            return
        deleted = await asyncio.to_thread(run_db, maintenance.purge_storage, source, name)
        audit("storage samples purged", source=source, name=name, samples=deleted)
        await self.reload()
        await self.admin.show("Purge storage samples", f"Deleted {deleted} sample(s).")

    async def do_reset(self) -> None:
        ok, _ = await self.admin.confirm(
            "Reset the database",
            "Deletes the database and ALL history (runs, events, status, storage samples) and recreates it empty. "
            "Agents and settings in the YAML files are not touched. Take a backup first if you may want this back.",
            confirm="Reset everything", danger=True, type_word="RESET")
        if not ok:
            return
        version = await asyncio.to_thread(maintenance.reset_database)
        audit("database reset")
        self._integrity = "not checked"
        await self.reload()
        await self.admin.show("Reset", f"The database was recreated at schema version {version}.")

    # ---------------------------------------------------------------- bindings and buttons

    def action_backup(self) -> None:
        self.admin.spawn(self.do_backup)

    def action_restore(self) -> None:
        self.admin.spawn(self.do_restore)

    def action_check(self) -> None:
        self.admin.spawn(self.do_check)

    def action_vacuum(self) -> None:
        self.admin.spawn(self.do_vacuum)

    def action_migrate(self) -> None:
        self.admin.spawn(self.do_migrate)

    def action_delete_runs(self) -> None:
        self.admin.spawn(self.do_delete_runs)

    def action_purge_storage(self) -> None:
        self.admin.spawn(self.do_purge_storage)

    def action_reset(self) -> None:
        self.admin.spawn(self.do_reset)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
