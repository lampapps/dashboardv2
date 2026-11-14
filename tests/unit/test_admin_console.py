"""Console tests drive the real Textual app headlessly. Dialogs are filled in and buttons pressed like a user would."""

import asyncio

import pytest
from textual.widgets import Button, DataTable, Input, RichLog, Static, TabbedContent

from app import config, db, mailer, settings
from app.admin import service
from app.admin.app import AdminApp
from app.admin.panes.database import DatabasePane
from app.admin.panes.diagnostics import DiagnosticsPane
from app.admin.panes.files import FilesPane
from app.admin.panes.service import ServicePane


def drive(scenario, size=(130, 45)):
    """Run `scenario(app, pilot)` against a fresh console and return its result."""
    async def main():
        app = AdminApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await scenario(app, pilot)

    return asyncio.run(main())


async def wait_for(pilot, predicate, what="condition", timeout=5.0):
    waited = 0.0
    while waited < timeout:
        await pilot.pause(0.05)
        if predicate():
            return
        waited += 0.05
    raise AssertionError(f"timed out waiting for {what}")


def dialog_title(app):
    labels = list(app.screen.query(".dialog-title"))
    return str(labels[0].render()) if labels else None


async def wait_dialog(pilot, app, title):
    await wait_for(pilot, lambda: dialog_title(app) == title, f"dialog '{title}'")


async def press(pilot, app, button_id):
    app.screen.query_one(f"#{button_id}", Button).press()
    await pilot.pause(0.1)


def table_rows(app, table_id):
    table = app.query_one(f"#{table_id}", DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


class TestStartup:
    def test_opens_on_the_agents_tab_and_switches_with_number_keys(self, home):
        async def scenario(app, pilot):
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "agents"
            for key, tab in (("2", "files"), ("3", "database"), ("4", "diagnostics"), ("5", "service"), ("1", "agents")):
                await pilot.press(key)
                await pilot.pause(0.1)
                assert tabs.active == tab

        drive(scenario)

    def test_empty_registry_says_how_to_add_an_agent(self, home):
        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "No agents registered" in str(app.query_one("#agents-note", Static).render()))

        drive(scenario)


def fill(app, **values):
    for key, value in values.items():
        widget = app.screen.query_one(f"#f-{key}")
        widget.value = value


def dialog_text(app):
    return " ".join(str(s.render()) for s in app.screen.query(Static))


async def add_agent_through_the_console(app, pilot, name="nas1", **fields):
    app.query_one("#add", Button).press()
    await wait_dialog(pilot, app, "Add agent")
    fill(app, name=name, **fields)
    await press(pilot, app, "save")
    await wait_dialog(pilot, app, "New API key")
    key = dialog_text(app).split("JABS_AGENT_KEY=")[1].split()[0]
    await press(pilot, app, "close")
    return key


class TestAgents:
    def test_add_shows_the_key_once_and_stores_only_its_hash(self, home):
        async def scenario(app, pilot):
            key = await add_agent_through_the_console(app, pilot, host="NAS1", grace_minutes="90")
            (agent,) = config.load_agents()
            assert agent.key_hash == config.hash_key(key) and (agent.host, agent.grace_minutes) == ("NAS1", 90)
            assert key not in settings.AGENTS_PATH.read_text()
            await wait_for(pilot, lambda: table_rows(app, "agents-table"))
            assert table_rows(app, "agents-table")[0][:3] == ["nas1", "NAS1", "enabled"]

        drive(scenario)

    def test_an_invalid_form_stays_open_with_the_reason(self, home):
        async def scenario(app, pilot):
            app.query_one("#add", Button).press()
            await wait_dialog(pilot, app, "Add agent")
            fill(app, name="bad name")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "name" in str(app.screen.query_one("#error", Static).render()))
            assert dialog_title(app) == "Add agent" and config.load_agents() == []
            fill(app, name="x", grace_minutes="soon")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "whole number" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)

    def test_duplicate_name_is_refused(self, home):
        async def scenario(app, pilot):
            await add_agent_through_the_console(app, pilot)
            app.query_one("#add", Button).press()
            await wait_dialog(pilot, app, "Add agent")
            fill(app, name="nas1")
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "already exists" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)

    def test_edit_changes_settings_and_keeps_the_key(self, home):
        key = config.add_agent("nas1", grace_minutes=30)

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "agents-table"))
            app.query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "Edit agent nas1")
            fill(app, grace_minutes="15", notes="the NAS", allowed_ip="10.0.0.9")
            app.screen.query_one("#f-offline_alerts").value = False
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: config.load_agents()[0].grace_minutes == 15)
            (agent,) = config.load_agents()
            assert (agent.notes, agent.allowed_ip, agent.offline_alerts) == ("the NAS", "10.0.0.9", False)
            assert agent.key_hash == config.hash_key(key)

        drive(scenario)

    def test_enable_and_disable(self, home):
        config.add_agent("nas1")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "agents-table"))
            app.query_one("#toggle", Button).press()
            await wait_for(pilot, lambda: config.load_agents()[0].enabled is False)
            await wait_for(pilot, lambda: table_rows(app, "agents-table")[0][2] == "DISABLED")
            app.query_one("#toggle", Button).press()
            await wait_for(pilot, lambda: config.load_agents()[0].enabled is True)

        drive(scenario)

    def test_rekey_asks_first_and_replaces_the_key(self, home):
        old = config.add_agent("nas1")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "agents-table"))
            app.query_one("#rekey", Button).press()
            await wait_dialog(pilot, app, "New key for nas1")
            await press(pilot, app, "cancel")
            assert config.load_agents()[0].key_hash == config.hash_key(old)

            app.query_one("#rekey", Button).press()
            await wait_dialog(pilot, app, "New key for nas1")
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "New API key")
            new = dialog_text(app).split("JABS_AGENT_KEY=")[1].split()[0]
            assert new != old and config.load_agents()[0].key_hash == config.hash_key(new)

        drive(scenario)

    def test_remove_keeps_history_unless_asked(self, home):
        config.add_agent("a")
        config.add_agent("b")
        db.init_db()
        conn = db.connect()
        for agent in ("a", "b"):
            conn.execute("INSERT INTO runs (agent, job_name, status, started_at, updated_at) VALUES (?, 'J', 'success', 1, 1)", (agent,))
        conn.close()

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: len(table_rows(app, "agents-table")) == 2)
            app.query_one("#remove", Button).press()           # removes "a" (first row) but keeps its history
            await wait_dialog(pilot, app, "Remove a")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: [x.name for x in config.load_agents()] == ["b"])
            conn = db.connect()
            assert conn.execute("SELECT COUNT(*) FROM runs WHERE agent = 'a'").fetchone()[0] == 1
            conn.close()

            await wait_for(pilot, lambda: len(table_rows(app, "agents-table")) == 1)
            app.query_one("#remove", Button).press()
            await wait_dialog(pilot, app, "Remove b")
            app.screen.query_one("#option").value = True
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "History deleted")
            conn = db.connect()
            assert conn.execute("SELECT COUNT(*) FROM runs WHERE agent = 'b'").fetchone()[0] == 0
            conn.close()

        drive(scenario)

    def test_nothing_selected_explains_itself(self, home):
        async def scenario(app, pilot):
            app.query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "No agent selected")

        drive(scenario)

    def test_a_broken_registry_is_reported_not_fatal(self, home):
        settings.AGENTS_PATH.write_text("agents: [not valid\n")

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: "invalid" in str(app.query_one("#agents-note", Static).render()))

        drive(scenario)

    def test_last_contact_comes_from_the_database(self, home):
        config.add_agent("nas1")
        db.init_db()
        conn = db.connect()
        import time
        conn.execute("INSERT INTO agent_state (name, last_heartbeat, updated_at, status) VALUES ('nas1', ?, 1, 'up')",
                     (time.time() - 600,))
        conn.close()

        async def scenario(app, pilot):
            await wait_for(pilot, lambda: table_rows(app, "agents-table"))
            row = table_rows(app, "agents-table")[0]
            assert row[6] == "10 min ago" and row[7] == "up"

        drive(scenario)


async def open_tab(pilot, key):
    await pilot.press(key)
    await pilot.pause(0.2)


def log_text(app, selector="#output"):
    return "\n".join(line.text for line in app.query_one(selector, RichLog).lines)


class TestFiles:
    VALID = "timezone: UTC\n"
    INVALID = "server:\n  prot: 1\n"

    @pytest.fixture
    def editor(self, monkeypatch):
        """Replace the editor: `script` is a list of texts written to the file on successive launches."""
        class Fake:
            script = []
            launches = []
            error = None

        async def run_editor(self, path):
            if Fake.error:
                raise Fake.error
            Fake.launches.append(path)
            if Fake.script:
                path.write_text(Fake.script.pop(0), encoding="utf-8")
            return 0

        restarts = []

        async def restart_service(self):
            restarts.append(1)

        Fake.script, Fake.launches, Fake.error = [], [], None
        Fake.restarts = restarts
        monkeypatch.setattr(AdminApp, "run_editor", run_editor)
        monkeypatch.setattr(AdminApp, "restart_service", restart_service)
        return Fake

    def test_lists_the_three_files_with_their_state(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: len(table_rows(app, "files-table")) == 3)
            rows = {r[0]: r[1] for r in table_rows(app, "files-table")}
            assert rows == {"config.yaml": "OK", "agents.yaml": "missing", ".env": "missing"}

        drive(scenario)

    def test_an_invalid_file_is_flagged_with_the_reason(self, home):
        settings.CONFIG_PATH.write_text(self.INVALID)

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table")[:1] and table_rows(app, "files-table")[0][1] == "INVALID")
            assert "server.prot" in str(app.query_one("#files-detail", Static).render())

        drive(scenario)

    def test_a_valid_edit_offers_a_restart(self, home, editor):
        editor.script = [self.VALID]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml changed")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: editor.restarts)

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == self.VALID

    def test_declining_the_restart_keeps_the_change(self, home, editor):
        editor.script = [self.VALID]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml changed")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert not editor.restarts

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == self.VALID

    def test_no_change_means_no_restart_prompt(self, home, editor):
        before = settings.CONFIG_PATH.read_text()

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.3)
            assert dialog_title(app) is None

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == before

    def test_an_invalid_edit_can_be_reverted(self, home, editor):
        before = settings.CONFIG_PATH.read_text()
        editor.script = [self.INVALID]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            assert "server.prot" in dialog_text(app)
            await press(pilot, app, "choice-1")
            await wait_dialog(pilot, app, "Reverted")
            assert not editor.restarts

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == before

    def test_an_invalid_edit_can_be_fixed_by_editing_again(self, home, editor):
        editor.script = [self.INVALID, self.VALID]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            await press(pilot, app, "choice-0")
            await wait_dialog(pilot, app, "config.yaml changed")
            assert len(editor.launches) == 2

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == self.VALID

    def test_an_invalid_edit_can_be_kept(self, home, editor):
        editor.script = [self.INVALID]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml is not valid")
            await press(pilot, app, "choice-2")
            await wait_dialog(pilot, app, "config.yaml changed")

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == self.INVALID

    def test_editing_agents_yaml_applies_live_without_a_restart_prompt(self, home, editor):
        config.add_agent("nas1")
        editor.script = ["agents:\n- name: other\n  key_hash: " + "a" * 64 + "\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one("#files-table", DataTable).focus()
            await pilot.press("down")
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_for(pilot, lambda: editor.launches)
            await pilot.pause(0.3)
            assert dialog_title(app) is None and not editor.restarts

        drive(scenario)
        assert [a.name for a in config.load_agents()] == ["other"]

    def test_a_missing_config_starts_from_the_example_and_a_new_env_is_private(self, home, editor):
        example = settings.CONFIG_PATH.read_text()
        (settings.CONFIG_PATH.parent / "config.example.yaml").write_text(example)
        settings.CONFIG_PATH.unlink()
        editor.script = [example + "# edited\n"]

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "config.yaml changed")
            await press(pilot, app, "cancel")
            await pilot.pause(0.1)
            app.query_one("#files-table", DataTable).focus()
            await pilot.press("down", "down")
            editor.script = ["SECRET=1\n"]
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, ".env changed")

        drive(scenario)
        assert settings.CONFIG_PATH.read_text().endswith("# edited\n")
        assert (settings.ENV_PATH.stat().st_mode & 0o777) == 0o600

    def test_no_editor_available_is_reported(self, home, editor):
        before = settings.CONFIG_PATH.read_text()
        from app.admin.editor import EditorError
        editor.error = EditorError("no editor found; set the EDITOR environment variable")

        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#edit", Button).press()
            await wait_dialog(pilot, app, "Error")
            assert "EDITOR" in dialog_text(app)

        drive(scenario)
        assert settings.CONFIG_PATH.read_text() == before

    def test_validate_all_reports_every_file(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "2")
            await wait_for(pilot, lambda: table_rows(app, "files-table"))
            app.query_one(FilesPane).query_one("#validate", Button).press()
            await wait_dialog(pilot, app, "Validation")
            text = dialog_text(app)
            assert "config.yaml: OK" in text and "agents.yaml: missing" in text and ".env: missing" in text

        drive(scenario)


def db_button(app, button_id):
    return app.query_one(DatabasePane).query_one(f"#{button_id}", Button)


def add_run(agent="a", job="J"):
    conn = db.connect()
    conn.execute("INSERT INTO runs (agent, job_name, status, started_at, updated_at) VALUES (?, ?, 'success', 1, 1)",
                 (agent, job))
    conn.close()


def count_runs():
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()


class TestDatabase:
    def test_without_a_database_it_offers_to_create_one(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: "No database yet" in str(app.query_one("#db-info", Static).render()))
            db_button(app, "migrate").press()
            await wait_dialog(pilot, app, "Database")
            assert settings.DB_PATH.exists()

        drive(scenario)

    def test_it_shows_version_size_and_table_counts(self, home):
        db.init_db()
        add_run()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            await wait_for(pilot, lambda: table_rows(app, "db-tables"))
            info = str(app.query_one("#db-info", Static).render())
            assert f"Schema version: {len(db.MIGRATIONS)}" in info and "Integrity: not checked" in info
            assert ["runs", "1"] in table_rows(app, "db-tables")

        drive(scenario)

    def test_backup_writes_a_copy(self, home):
        db.init_db()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "backup").press()
            await wait_dialog(pilot, app, "Back up the database")
            await press(pilot, app, "save")
            await wait_dialog(pilot, app, "Backup written")

        drive(scenario)
        assert len(list(settings.BACKUP_DIR.glob("jabs-*.sqlite"))) == 1

    def test_backup_refuses_to_overwrite(self, home):
        db.init_db()
        target = settings.BACKUP_DIR
        target.mkdir(parents=True)
        (target / "x.sqlite").write_text("keep me")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "backup").press()
            await wait_dialog(pilot, app, "Back up the database")
            fill(app, file=str(target / "x.sqlite"))
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "already exists" in str(app.screen.query_one("#error", Static).render()))

        drive(scenario)
        assert (target / "x.sqlite").read_text() == "keep me"

    def test_integrity_check_updates_the_panel(self, home):
        db.init_db()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "check").press()
            await wait_for(pilot, lambda: "Integrity: ok" in str(app.query_one("#db-info", Static).render()))

        drive(scenario)

    def test_vacuum_confirms_then_reports_sizes(self, home):
        db.init_db()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "vacuum").press()
            await wait_dialog(pilot, app, "Vacuum")
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "Vacuum")
            assert "->" in dialog_text(app)

        drive(scenario)

    def test_reset_needs_the_word_and_then_empties_the_database(self, home):
        db.init_db()
        add_run()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "reset").press()
            await wait_dialog(pilot, app, "Reset the database")
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word", Input).value = "reset"
            await pilot.pause(0.1)
            assert app.screen.query_one("#ok", Button).disabled is True
            app.screen.query_one("#word", Input).value = "RESET"
            await pilot.pause(0.1)
            assert app.screen.query_one("#ok", Button).disabled is False
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "Reset")

        drive(scenario)
        assert count_runs() == 0

    def test_cancelling_reset_changes_nothing(self, home):
        db.init_db()
        add_run()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "reset").press()
            await wait_dialog(pilot, app, "Reset the database")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert dialog_title(app) is None

        drive(scenario)
        assert count_runs() == 1

    def test_restore_from_the_backup_list(self, home):
        db.init_db()
        add_run(job="before")
        backup = db.backup(settings.BACKUP_DIR / "good.sqlite")
        add_run(job="after")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "restore").press()
            await wait_dialog(pilot, app, "Restore from which backup?")
            await pilot.press("enter")
            await wait_dialog(pilot, app, "Restore the database")
            assert str(backup) in dialog_text(app)
            app.screen.query_one("#word", Input).value = "RESTORE"
            await pilot.pause(0.1)
            await press(pilot, app, "ok")
            await wait_dialog(pilot, app, "Restored")

        drive(scenario)
        assert count_runs() == 1
        assert list(settings.BACKUP_DIR.glob("pre-restore-*.sqlite"))

    def test_restore_rejects_a_bad_file_before_asking(self, home):
        db.init_db()
        junk = home / "junk.sqlite"
        junk.write_text("nope")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "restore").press()
            await wait_dialog(pilot, app, "Restore from which backup?")
            app.screen.query_one("#custom", Input).value = str(junk)
            app.screen.query_one("#custom", Input).focus()
            await pilot.press("enter")
            await wait_dialog(pilot, app, "Error")

        drive(scenario)

    def test_delete_runs_by_job(self, home):
        db.init_db()
        add_run("a", "J1")
        add_run("a", "J2")

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "delete_runs").press()
            await wait_dialog(pilot, app, "Delete stored runs for...")
            await pilot.press("down", "enter")                 # "a / J1" under "a: all 2 run(s)"
            await wait_dialog(pilot, app, "Delete runs")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "Deleted 1 run" in dialog_text(app))

        drive(scenario)
        assert count_runs() == 1

    def test_purge_storage_samples(self, home):
        db.init_db()
        conn = db.connect()
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r', '', 1, 5, NULL)")
        conn.close()

        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "purge_storage").press()
            await wait_dialog(pilot, app, "Purge storage samples for...")
            await pilot.press("enter")
            await wait_dialog(pilot, app, "Purge storage samples")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "Deleted 1 sample" in dialog_text(app))

        drive(scenario)

    def test_actions_need_a_current_database(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "3")
            db_button(app, "vacuum").press()
            await wait_dialog(pilot, app, "Error")

        drive(scenario)
        assert not settings.DB_PATH.exists()  # nothing was created behind the user's back


class TestDiagnostics:
    def test_key_check_names_the_owner_and_never_stores_the_key(self, home):
        key = config.add_agent("nas1")
        config.set_agent_enabled("nas1", False)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#key", Button).press()
            await wait_dialog(pilot, app, "Which agent has this key?")
            fill(app, key=key)
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "belongs to agent 'nas1'" in log_text(app))
            assert "DISABLED" in log_text(app) and key not in log_text(app)

        drive(scenario)

    def test_key_check_with_an_unknown_key(self, home):
        config.add_agent("nas1")

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#key", Button).press()
            await wait_dialog(pilot, app, "Which agent has this key?")
            fill(app, key="0" * 40)
            await press(pilot, app, "save")
            await wait_for(pilot, lambda: "no registered agent has this key" in log_text(app))

        drive(scenario)

    def test_tick_asks_then_lists_every_task(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#tick", Button).press()
            await wait_dialog(pilot, app, "Run the periodic tasks")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: "daily rollup" in log_text(app) and "s3 usage" in log_text(app))
            assert "FAILED" not in log_text(app)

        drive(scenario)
        conn = db.connect()
        assert db.get_meta(conn, "last_tick_ts")
        conn.close()

    def test_cancelling_the_tick_runs_nothing(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#tick", Button).press()
            await wait_dialog(pilot, app, "Run the periodic tasks")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert log_text(app) == ""

        drive(scenario)
        assert not settings.DB_PATH.exists()

    def test_meta_lists_internal_state_with_ages(self, home):
        db.init_db()
        conn = db.connect()
        db.set_meta(conn, "last_tick_ts", "1")
        conn.close()

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#meta", Button).press()
            await wait_for(pilot, lambda: "last_tick_ts = 1" in log_text(app))

        drive(scenario)

    def test_testmail_says_when_email_is_not_configured(self, home):
        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#testmail", Button).press()
            await wait_for(pilot, lambda: "email is not configured" in log_text(app))

        drive(scenario)

    def test_testmail_sends_and_reports(self, home, monkeypatch):
        settings.CONFIG_PATH.write_text("email:\n  smtp_server: smtp.test\n  to_addrs: [a@example.com]\n  from_addr: j@example.com\n")
        sent = []
        monkeypatch.setattr(mailer, "send", lambda email, subject, text, html: sent.append(subject) or True)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#testmail", Button).press()
            await wait_for(pilot, lambda: "sent to a@example.com" in log_text(app))

        drive(scenario)
        assert sent == ["JABS test message"]

    def test_digest_preview_then_optional_send(self, home, monkeypatch):
        db.init_db()
        settings.CONFIG_PATH.write_text("email:\n  smtp_server: smtp.test\n  to_addrs: [a@example.com]\n  from_addr: j@example.com\n")
        sent = []
        monkeypatch.setattr(mailer, "send", lambda email, subject, text, html: sent.append(subject) or True)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#digest", Button).press()
            await wait_dialog(pilot, app, "Send the digest")
            assert "Subject: JABS digest" in log_text(app)
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert not sent

        drive(scenario)

    def test_alert_preview_and_test_send_are_marked_and_change_no_state(self, home, monkeypatch):
        db.init_db()
        config.add_agent("nas1")
        settings.CONFIG_PATH.write_text("email:\n  smtp_server: smtp.test\n  to_addrs: [a@example.com]\n  from_addr: j@example.com\n")
        sent = []
        monkeypatch.setattr(mailer, "send", lambda email, subject, text, html: sent.append(subject) or True)

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#alert", Button).press()
            await wait_dialog(pilot, app, "Preview the offline alert for...")
            await pilot.press("enter")
            await wait_dialog(pilot, app, "Send as a test")
            assert "is offline" in log_text(app)
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: sent)

        drive(scenario)
        assert sent[0].startswith("[TEST] JABS alert: agent nas1 is offline")
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM agent_state WHERE alert_count > 0").fetchone()[0] == 0
        conn.close()

    def test_dev_server_confirms_then_hands_over_the_terminal(self, home, monkeypatch):
        ran = []
        monkeypatch.setattr(AdminApp, "run_external", lambda self, func: ran.append(func))

        async def scenario(app, pilot):
            await open_tab(pilot, "4")
            app.query_one(DiagnosticsPane).query_one("#dev_server", Button).press()
            await wait_dialog(pilot, app, "Run the development server")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: ran)

        drive(scenario)


class TestService:
    @pytest.fixture
    def units(self, monkeypatch):
        calls = []
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: "jabs-dashboard-v2")
        monkeypatch.setattr(service, "unit_status", lambda unit: {
            "active": "active (running)", "enabled": "enabled", "since": "today", "next": "-"})

        async def run_systemctl(self, action, unit):
            calls.append((action, unit))
            return 0

        monkeypatch.setattr(AdminApp, "run_systemctl", run_systemctl)
        return calls

    def test_lists_the_three_units(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: len(table_rows(app, "units")) == 3)
            assert [r[0] for r in table_rows(app, "units")] == [
                "jabs-dashboard-v2.service", "jabs-dashboard-v2-tick.service", "jabs-dashboard-v2-tick.timer"]
            assert table_rows(app, "units")[0][2] == "active (running)"

        drive(scenario)

    def test_restart_asks_first(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            app.query_one(ServicePane).query_one("#restart", Button).press()
            await wait_dialog(pilot, app, "Restart jabs-dashboard-v2.service")
            await press(pilot, app, "cancel")
            await pilot.pause(0.2)
            assert units == []
            app.query_one(ServicePane).query_one("#restart", Button).press()
            await wait_dialog(pilot, app, "Restart jabs-dashboard-v2.service")
            await press(pilot, app, "ok")
            await wait_for(pilot, lambda: units == [("restart", "jabs-dashboard-v2.service")])

        drive(scenario)

    def test_start_does_not_ask(self, home, units):
        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            app.query_one("#units", DataTable).focus()
            await pilot.press("down", "down")                  # the timer
            app.query_one(ServicePane).query_one("#start", Button).press()
            await wait_for(pilot, lambda: units == [("start", "jabs-dashboard-v2-tick.timer")])

        drive(scenario)

    def test_a_failed_command_is_reported(self, home, units, monkeypatch):
        async def failing(self, action, unit):
            return 5

        monkeypatch.setattr(AdminApp, "run_systemctl", failing)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: table_rows(app, "units"))
            app.query_one(ServicePane).query_one("#start", Button).press()
            await wait_dialog(pilot, app, "Error")
            assert "status 5" in dialog_text(app)

        drive(scenario)

    def test_without_systemd_it_says_so(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: False)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "systemd is not available" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)

    def test_unknown_install_points_at_the_unit_option(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "detect_base", lambda override=None: None)

        async def scenario(app, pilot):
            await open_tab(pilot, "5")
            await wait_for(pilot, lambda: "--unit" in str(app.query_one("#service-note", Static).render()))

        drive(scenario)

    def test_restart_after_a_config_change_uses_the_detected_unit(self, home, units):
        async def scenario(app, pilot):
            app.spawn(app.restart_service)
            await wait_for(pilot, lambda: units == [("restart", "jabs-dashboard-v2.service")])

        drive(scenario)
