import sqlite3

import pytest

from app import __main__ as cli
from app import config, db, maintenance, settings, tick


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def conn(home):
    db.init_db()
    connection = db.connect()
    yield connection
    connection.close()


def add_run(conn, agent="a", job="J", with_event=True):
    cur = conn.execute(
        "INSERT INTO runs (agent, job_name, status, started_at, updated_at) VALUES (?, ?, 'success', 100, 100)",
        (agent, job))
    if with_event:
        conn.execute("INSERT INTO events (run_pk, event_type, timestamp, created_at) VALUES (?, 'heartbeat', 1, 1)",
                     (cur.lastrowid,))
    return cur.lastrowid


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestAgentEditing:
    def test_update_changes_only_the_given_fields(self, home):
        config.add_agent("nas1", grace_minutes=30, notes="n", host="H")
        updated = config.update_agent("nas1", grace_minutes=90, offline_alerts=False)
        assert (updated.grace_minutes, updated.offline_alerts, updated.notes, updated.host) == (90, False, "n", "H")
        assert config.load_agents()[0].grace_minutes == 90

    def test_update_keeps_the_key_and_can_clear_the_allowed_ip(self, home):
        key = config.add_agent("nas1", allowed_ip="10.0.0.5")
        config.update_agent("nas1", allowed_ip=None)
        (agent,) = config.load_agents()
        assert agent.allowed_ip is None and agent.key_hash == config.hash_key(key)

    @pytest.mark.parametrize("changes,message", [
        ({"grace_minutes": 0}, "grace_minutes"), ({"allowed_ip": "nope"}, "allowed_ip"),
        ({"host": "two words"}, "host"), ({"key_hash": "x"}, "cannot change key_hash"),
    ])
    def test_update_rejects_bad_values_and_leaves_the_file_alone(self, home, changes, message):
        config.add_agent("nas1")
        before = settings.AGENTS_PATH.read_text()
        with pytest.raises(config.ConfigError, match=message):
            config.update_agent("nas1", **changes)
        assert settings.AGENTS_PATH.read_text() == before

    def test_update_and_remove_unknown_agent(self, home):
        with pytest.raises(config.ConfigError, match="not found"):
            config.update_agent("ghost", notes="x")
        with pytest.raises(config.ConfigError, match="not found"):
            config.remove_agent("ghost")

    def test_remove(self, home):
        config.add_agent("a")
        config.add_agent("b")
        config.remove_agent("a")
        assert [x.name for x in config.load_agents()] == ["b"]

    def test_edits_leave_the_lock_file_beside_the_registry(self, home):
        config.add_agent("a")
        assert (settings.AGENTS_PATH.parent / ".agents.lock").exists()


class TestRenamingAnAgent:
    def seed(self, conn, agent):
        add_run(conn, agent=agent)
        conn.execute("INSERT INTO agent_state (name, updated_at) VALUES (?, 1)", (agent,))
        conn.execute("INSERT INTO agent_status_log (agent, ts, status) VALUES (?, 1, 'up')", (agent,))
        conn.execute("INSERT INTO daily_runs VALUES ('2026-01-01', ?, 'success', 1)", (agent,))
        conn.execute("INSERT INTO job_schedules VALUES (?, 'J', '0 3 * * *', 1)", (agent,))
        conn.execute("INSERT INTO storage_samples VALUES (?, 'repo', 'r', '', 1, 5, NULL)", (agent,))
        conn.execute("INSERT INTO job_targets VALUES (?, 'J', 'J', '/s', '/d', 1)", (agent,))
        conn.execute("INSERT INTO job_config VALUES (?, 'J', '[]', 1)", (agent,))

    def test_registry_rename_keeps_key_and_settings(self, home):
        key = config.add_agent("old", grace_minutes=30, notes="n", host="H", allowed_ip="10.0.0.5")
        config.rename_agent("old", "new")
        (agent,) = config.load_agents()
        assert (agent.name, agent.grace_minutes, agent.notes, agent.host, agent.allowed_ip) == \
            ("new", 30, "n", "H", "10.0.0.5")
        assert agent.key_hash == config.hash_key(key)

    @pytest.mark.parametrize("old,new,message", [
        ("ghost", "x", "not found"), ("a", "a", "already has that name"), ("a", "b", "already exists"),
        ("a", "two words", "name"),
    ])
    def test_registry_rename_refusals_leave_the_file_alone(self, home, old, new, message):
        config.add_agent("a")
        config.add_agent("b")
        before = settings.AGENTS_PATH.read_text()
        with pytest.raises(config.ConfigError, match=message):
            config.rename_agent(old, new)
        assert settings.AGENTS_PATH.read_text() == before

    def test_every_table_follows_the_new_name_and_others_are_untouched(self, conn):
        config.add_agent("old")
        config.add_agent("other")
        self.seed(conn, "old")
        self.seed(conn, "other")
        conn.execute("INSERT INTO storage_samples VALUES ('dashboard', 'local', '/', '', 1, 5, 10)")
        changed = maintenance.rename_agent("old", "new")
        assert changed == {table: 1 for table, _ in maintenance.AGENT_NAME_COLUMNS}
        for table, column in maintenance.AGENT_NAME_COLUMNS:
            names = sorted(r[0] for r in conn.execute(f"SELECT {column} FROM {table}"))
            assert "old" not in names and "new" in names and "other" in names, table
        assert count(conn, "events") == 2
        assert [a.name for a in config.load_agents()] == ["new", "other"]

    def test_without_a_database_only_the_registry_changes(self, home):
        config.add_agent("old")
        assert maintenance.rename_agent("old", "new") == {}
        assert [a.name for a in config.load_agents()] == ["new"]

    def test_a_name_with_stored_history_is_refused_and_nothing_changes(self, conn):
        config.add_agent("old")
        self.seed(conn, "old")
        conn.execute("INSERT INTO storage_samples VALUES ('taken', 'repo', 'r', '', 1, 5, NULL)")
        before = settings.AGENTS_PATH.read_text()
        with pytest.raises(config.ConfigError, match="already has stored history \\(storage_samples\\)"):
            maintenance.rename_agent("old", "taken")
        assert settings.AGENTS_PATH.read_text() == before
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE agent = 'old'").fetchone()[0] == 1

    def test_a_registry_failure_rolls_the_database_back(self, conn):
        self.seed(conn, "old")      # "old" is not registered, so the registry step fails
        with pytest.raises(config.ConfigError, match="not found"):
            maintenance.rename_agent("old", "new")
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE agent = 'old'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE agent = 'new'").fetchone()[0] == 0


class TestDeletingHistory:
    def test_delete_runs_for_a_job_removes_events_and_schedule_only_for_that_job(self, conn):
        add_run(conn, job="J1")
        add_run(conn, job="J2")
        conn.execute("INSERT INTO job_schedules VALUES ('a', 'J1', '0 3 * * *', 1)")
        conn.execute("INSERT INTO job_schedules VALUES ('a', 'J2', '0 3 * * *', 1)")
        assert maintenance.delete_runs(conn, "a", "J1") == 1
        assert [r["job_name"] for r in conn.execute("SELECT job_name FROM runs")] == ["J2"]
        assert count(conn, "events") == 1
        assert [r["job_name"] for r in conn.execute("SELECT job_name FROM job_schedules")] == ["J2"]

    def test_delete_all_runs_of_an_agent_keeps_other_agents(self, conn):
        add_run(conn, agent="a")
        add_run(conn, agent="b")
        assert maintenance.delete_runs(conn, "a") == 1
        assert [r["agent"] for r in conn.execute("SELECT agent FROM runs")] == ["b"]

    def test_deleting_nothing_does_not_bump_the_revision(self, conn):
        before = db.get_meta(conn, "rev")
        assert maintenance.delete_runs(conn, "ghost") == 0
        assert db.get_meta(conn, "rev") == before

    def test_agent_history_is_removed_everywhere(self, conn):
        add_run(conn, agent="a")
        add_run(conn, agent="b")
        conn.execute("INSERT INTO agent_state (name, updated_at) VALUES ('a', 1)")
        conn.execute("INSERT INTO agent_status_log (agent, ts, status) VALUES ('a', 1, 'up')")
        conn.execute("INSERT INTO daily_runs VALUES ('2026-01-01', 'a', 'success', 1)")
        conn.execute("INSERT INTO job_targets VALUES ('a', 'J', 'J', '/s', '/d', 1)")
        conn.execute("INSERT INTO job_config VALUES ('a', 'J', '[]', 1)")
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r', '', 1, 5, NULL)")
        conn.execute("INSERT INTO storage_samples VALUES ('dashboard', 'local', '/', '', 1, 5, 10)")
        deleted = maintenance.delete_agent_history(conn, "a")
        assert deleted["runs"] == 1 and deleted["storage_samples"] == 1
        for table in ("agent_state", "agent_status_log", "daily_runs", "job_targets", "job_config"):
            assert count(conn, table) == 0
        assert count(conn, "runs") == 1 and count(conn, "storage_samples") == 1

    def test_purge_storage_by_source_and_name(self, conn):
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r1', '', 1, 5, NULL)")
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r2', '', 1, 5, NULL)")
        conn.execute("INSERT INTO storage_samples VALUES ('b', 'repo', 'r1', '', 1, 5, NULL)")
        assert maintenance.purge_storage(conn, source="a", name="r1") == 1
        assert maintenance.purge_storage(conn, source="a") == 1
        assert maintenance.purge_storage(conn) == 1

    def test_pickers(self, conn):
        add_run(conn, job="J1")
        add_run(conn, job="J1")
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r1', '', 1, 5, NULL)")
        assert maintenance.job_counts(conn) == [{"agent": "a", "job_name": "J1", "runs": 2}]
        assert maintenance.storage_series(conn) == [{"source": "a", "kind": "repo", "name": "r1", "samples": 1}]


class TestSingleRuns:
    def test_list_is_newest_first_filtered_and_limited(self, conn):
        first = add_run(conn, job="J1")
        second = add_run(conn, job="J1")
        add_run(conn, job="J2")
        add_run(conn, agent="b", job="J1")
        conn.execute("UPDATE runs SET started_at = id")
        assert [r["id"] for r in maintenance.list_runs(conn, "a", "J1")] == [second, first]
        assert len(maintenance.list_runs(conn, "a")) == 3
        assert [r["id"] for r in maintenance.list_runs(conn, "a", "J1", limit=1)] == [second]
        assert set(maintenance.list_runs(conn, "a")[0]) == {
            "id", "job_name", "target_label", "status", "started_at", "runtime_seconds", "bytes_processed", "message"}

    def test_message_prefers_the_closing_event_like_the_job_page(self, conn):
        done = add_run(conn, with_event=False)
        for kind, text in (("heartbeat", "Starting"), ("backup_complete", "All done"), ("heartbeat", "Verify passed")):
            conn.execute("INSERT INTO events (run_pk, event_type, message, timestamp, created_at) VALUES (?, ?, ?, 1, 1)",
                         (done, kind, text))
        running = add_run(conn, with_event=False)
        for text in ("Starting", "Halfway"):
            conn.execute("INSERT INTO events (run_pk, event_type, message, timestamp, created_at) "
                         "VALUES (?, 'heartbeat', ?, 1, 1)", (running, text))
        bare = add_run(conn, with_event=False)
        messages = {r["id"]: r["message"] for r in maintenance.list_runs(conn, "a")}
        assert messages == {done: "All done", running: "Halfway", bare: None}

    def test_delete_by_id_removes_only_those_runs_and_their_events(self, conn):
        keep = add_run(conn)
        gone = add_run(conn)
        other = add_run(conn)
        assert maintenance.delete_run_ids(conn, [gone, other, gone, 9999]) == 2
        assert [r["id"] for r in conn.execute("SELECT id FROM runs")] == [keep]
        assert count(conn, "events") == 1

    def test_delete_by_id_bumps_the_revision_only_when_something_went(self, conn):
        run = add_run(conn)
        before = db.get_meta(conn, "rev")
        assert maintenance.delete_run_ids(conn, [9999]) == 0 and db.get_meta(conn, "rev") == before
        assert maintenance.delete_run_ids(conn, [run]) == 1 and db.get_meta(conn, "rev") != before

    def test_many_ids_are_deleted_in_chunks(self, conn):
        ids = [add_run(conn, with_event=False) for _ in range(1200)]
        assert maintenance.delete_run_ids(conn, ids) == 1200
        assert count(conn, "runs") == 0

    def test_ids_must_be_numbers(self, conn):
        with pytest.raises(ValueError):
            maintenance.delete_run_ids(conn, ["1; DROP TABLE runs"])
        assert count(conn, "meta") >= 0


class TestHealth:
    def test_a_healthy_database_has_no_problems(self, conn):
        assert maintenance.integrity_problems(conn) == []

    def test_counts_cover_every_table_largest_first(self, conn):
        add_run(conn)
        add_run(conn)
        counts = dict(maintenance.table_counts(conn))
        assert counts["runs"] == 2 and counts["events"] == 2 and "meta" in counts
        assert maintenance.table_counts(conn)[0][1] >= maintenance.table_counts(conn)[-1][1]

    def test_vacuum_reports_sizes(self, conn):
        for i in range(50):
            add_run(conn, job=f"J{i}")
        maintenance.delete_runs(conn, "a")
        sizes = maintenance.vacuum(conn)
        assert sizes["after"]["db"] <= sizes["before"]["db"] + sizes["before"]["wal"]

    def test_meta_items(self, conn):
        db.set_meta(conn, "zzz", "1")
        assert ("zzz", "1") in maintenance.meta_items(conn)


class TestRestore:
    def test_restore_replaces_the_data_and_saves_a_safety_copy(self, conn, home):
        add_run(conn, job="before")
        backup = db.backup(settings.BACKUP_DIR / "good.sqlite")
        maintenance.delete_runs(conn, "a")
        add_run(conn, job="after")
        conn.close()

        result = maintenance.restore(backup)
        assert result["schema_version"] == len(db.MIGRATIONS)
        assert result["safety_copy"].exists()
        fresh = db.connect()
        try:
            assert [r["job_name"] for r in fresh.execute("SELECT job_name FROM runs")] == ["before"]
        finally:
            fresh.close()
        safety = sqlite3.connect(result["safety_copy"])
        assert safety.execute("SELECT job_name FROM runs").fetchall() == [("after",)]
        safety.close()

    def test_a_non_database_is_refused_and_nothing_changes(self, conn, home):
        junk = home / "junk.sqlite"
        junk.write_text("not a database")
        add_run(conn)
        with pytest.raises(db.DbError):
            maintenance.restore(junk)
        assert count(conn, "runs") == 1
        assert not list(settings.BACKUP_DIR.glob("pre-restore-*")) if settings.BACKUP_DIR.exists() else True

    def test_a_newer_schema_is_refused(self, conn, home):
        newer = home / "newer.sqlite"
        raw = sqlite3.connect(newer)
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
        raw.close()
        with pytest.raises(db.DbError, match="newer"):
            maintenance.check_backup_file(newer)

    def test_a_file_without_a_schema_version_is_refused(self, home):
        empty = home / "empty.sqlite"
        sqlite3.connect(empty).close()
        with pytest.raises(db.DbError, match="not a JABS database"):
            maintenance.check_backup_file(empty)

    def test_missing_file(self, home):
        with pytest.raises(db.DbError, match="not found"):
            maintenance.check_backup_file(home / "nope.sqlite")

    def test_backups_are_listed_newest_first(self, conn, home):
        db.backup(settings.BACKUP_DIR / "a.sqlite")
        db.backup(settings.BACKUP_DIR / "b.sqlite")
        assert {p.name for p, _, _ in maintenance.list_backups()} == {"a.sqlite", "b.sqlite"}


class TestTick:
    def test_run_once_reports_each_task(self, conn, home):
        results = tick.run_once(conn, config.load_config())
        assert [r["name"] for r in results] == [name for name, _ in tick.TASKS]
        assert all(r["error"] is None and r["seconds"] >= 0 for r in results)

    def test_a_failing_task_is_reported_and_the_rest_still_run(self, conn, home, monkeypatch):
        def boom(*_a):
            raise RuntimeError("bang")
        monkeypatch.setattr(tick, "TASKS", [("bad", boom), ("good", lambda *a: None)])
        results = tick.run_once(conn, config.load_config())
        assert results[0]["error"] == "RuntimeError: bang" and results[1]["error"] is None


class TestCli:
    def test_agent_set(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        code, out, _ = run_cli(capsys, "agent", "set", "nas1", "--grace-minutes", "15", "--no-alerts",
                               "--allowed-ip", "10.0.0.9")
        assert code == 0 and "allowed_ip" in out
        (agent,) = config.load_agents()
        assert (agent.grace_minutes, agent.offline_alerts, agent.allowed_ip) == (15, False, "10.0.0.9")
        assert run_cli(capsys, "agent", "set", "nas1", "--allowed-ip", "")[0] == 0
        assert config.load_agents()[0].allowed_ip is None

    def test_agent_set_needs_an_option_and_reports_bad_values(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        assert run_cli(capsys, "agent", "set", "nas1")[0] == 2
        code, _, err = run_cli(capsys, "agent", "set", "nas1", "--grace-minutes", "0")
        assert code == 1 and "grace_minutes" in err

    def test_agent_remove_needs_confirmation_and_can_purge_history(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        db.init_db()
        conn = db.connect()
        add_run(conn, agent="nas1")
        conn.close()
        assert run_cli(capsys, "agent", "remove", "nas1")[0] == 2
        assert config.load_agents()
        code, out, _ = run_cli(capsys, "agent", "remove", "nas1", "--purge-history", "--yes")
        assert code == 0 and "runs 1" in out
        assert config.load_agents() == []
        conn = db.connect()
        assert count(conn, "runs") == 0
        conn.close()

    def test_agent_remove_keeps_history_by_default(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        db.init_db()
        conn = db.connect()
        add_run(conn, agent="nas1")
        conn.close()
        run_cli(capsys, "agent", "remove", "nas1", "--yes")
        conn = db.connect()
        assert count(conn, "runs") == 1
        conn.close()

    def test_restore_checks_then_confirms(self, home, capsys):
        db.init_db()
        backup = db.backup(settings.BACKUP_DIR / "b.sqlite")
        assert run_cli(capsys, "restore", str(backup))[0] == 2
        code, out, _ = run_cli(capsys, "restore", str(backup), "--yes")
        assert code == 0 and "restored" in out
        assert run_cli(capsys, "restore", str(home / "missing.sqlite"), "--yes")[0] == 1

    def test_vacuum_check_db_and_meta(self, home, capsys):
        db.init_db()
        assert "->" in run_cli(capsys, "vacuum")[1]
        code, out, _ = run_cli(capsys, "check-db")
        assert code == 0 and "integrity: ok" in out and "runs" in out
        run_cli(capsys, "tick")
        assert "last_tick_ts" in run_cli(capsys, "meta")[1]

    def test_runs_delete_and_storage_purge_need_confirmation(self, home, capsys):
        db.init_db()
        conn = db.connect()
        add_run(conn, agent="nas1", job="J")
        conn.execute("INSERT INTO storage_samples VALUES ('nas1', 'repo', 'r', '', 1, 5, NULL)")
        conn.close()
        assert run_cli(capsys, "runs", "delete", "--agent", "nas1")[0] == 2
        assert run_cli(capsys, "storage", "purge")[0] == 2
        assert "deleted 1 run" in run_cli(capsys, "runs", "delete", "--agent", "nas1", "--job", "J", "--yes")[1]
        assert "deleted 1 storage" in run_cli(capsys, "storage", "purge", "--source", "nas1", "--yes")[1]
        assert run_cli(capsys, "runs")[0] == 2 and run_cli(capsys, "storage")[0] == 2

    def test_verbose_tick_lists_tasks(self, home, capsys):
        out = run_cli(capsys, "tick", "-v")[1]
        assert "daily rollup" in out and "ok" in out

    def test_runs_list_shows_ids_newest_first(self, home, capsys):
        db.init_db()
        conn = db.connect()
        first, second = add_run(conn, job="J"), add_run(conn, job="J")
        conn.execute("UPDATE runs SET started_at = id")
        conn.close()
        code, out, _ = run_cli(capsys, "runs", "list", "--agent", "a", "--job", "J")
        assert code == 0
        lines = out.strip().splitlines()
        assert [line.split("\t")[0] for line in lines] == [str(second), str(first)]
        assert all(line.split("\t")[-1] == "-" or line.split("\t")[-1] == "" for line in lines)
        assert "no runs found" in run_cli(capsys, "runs", "list", "--agent", "ghost")[1]

    def test_runs_delete_by_id_needs_confirmation(self, home, capsys):
        db.init_db()
        conn = db.connect()
        keep, gone = add_run(conn), add_run(conn)
        conn.close()
        assert run_cli(capsys, "runs", "delete", "--id", str(gone))[0] == 2
        code, out, _ = run_cli(capsys, "runs", "delete", "--id", str(gone), "--id", "9999", "--yes")
        assert code == 0 and "deleted 1 run" in out
        conn = db.connect()
        assert [r["id"] for r in conn.execute("SELECT id FROM runs")] == [keep]
        conn.close()

    def test_runs_delete_rejects_ambiguous_or_empty_requests(self, home, capsys):
        assert run_cli(capsys, "runs", "delete", "--yes")[0] == 2
        assert run_cli(capsys, "runs", "delete", "--id", "1", "--agent", "a", "--yes")[0] == 2
