import sqlite3

import pytest

from app import __main__ as cli
from app import config, db, maintenance, settings, tick

from ..seed import add_run as seed_run, add_schedule, aid, ensure_agent, ensure_job


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


def add_run(conn, agent="a", job="J", with_event=True, name=None):
    """A run for `agent` (its id; `name` is its display name) with one heartbeat event."""
    return seed_run(conn, agent=agent, agent_name=name, job=job, started=100, runtime=1,
                    events=[("heartbeat", "")] if with_event else [])


def registered_id(name):
    return config.find_agent(name).id


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

    def test_the_id_and_the_history_do_not_move(self, conn):
        config.add_agent("old")
        agent_id = registered_id("old")
        add_run(conn, agent=agent_id, name="old")
        config.rename_agent("old", "new")
        assert registered_id("new") == agent_id
        assert conn.execute("SELECT COUNT(*) FROM run_view WHERE agent_id = ?", (agent_id,)).fetchone()[0] == 1

    def test_a_new_agent_may_reuse_a_name_without_inheriting_history(self, conn):
        config.add_agent("old")
        old_id = registered_id("old")
        add_run(conn, agent=old_id, name="old")
        config.rename_agent("old", "renamed")
        config.add_agent("old")
        assert registered_id("old") != old_id
        assert conn.execute("SELECT COUNT(*) FROM run_view WHERE agent_id = ?", (registered_id("old"),)).fetchone()[0] == 0


class TestDeletingHistory:
    def test_delete_runs_for_a_job_removes_their_events_only_for_that_job(self, conn):
        add_run(conn, job="J1")
        add_run(conn, job="J2")
        assert maintenance.delete_runs(conn, "a", "J1") == 1
        assert [r["job_key"] for r in conn.execute("SELECT job_key FROM run_view")] == ["J2"]
        assert count(conn, "events") == 1

    def test_a_job_the_agent_still_reports_stays_after_its_runs_go(self, conn):
        add_run(conn, job="J1")
        add_schedule(conn, "a", "J1", "0 3 * * *")
        maintenance.delete_runs(conn, "a", "J1")
        assert count(conn, "jobs") == 1 and count(conn, "job_schedules") == 1

    def test_a_job_the_agent_dropped_goes_with_its_runs(self, conn):
        add_run(conn, job="J1")
        add_schedule(conn, "a", "J1", "0 3 * * *")
        conn.execute("UPDATE jobs SET active = 0")
        maintenance.delete_runs(conn, "a", "J1")
        assert (count(conn, "jobs"), count(conn, "targets"), count(conn, "job_schedules")) == (0, 0, 0)

    def test_delete_all_runs_of_an_agent_keeps_other_agents(self, conn):
        add_run(conn, agent="a")
        add_run(conn, agent="b")
        assert maintenance.delete_runs(conn, "a") == 1
        assert [r["agent_id"] for r in conn.execute("SELECT agent_id FROM run_view")] == ["b"]

    def test_deleting_nothing_does_not_bump_the_revision(self, conn):
        before = db.get_meta(conn, "rev")
        assert maintenance.delete_runs(conn, "ghost") == 0
        assert db.get_meta(conn, "rev") == before

    def test_agent_history_is_removed_everywhere(self, conn):
        add_run(conn, agent="a")
        add_run(conn, agent="b")
        add_schedule(conn, "a", "J", "0 3 * * *")
        conn.execute("INSERT INTO agent_status_log (agent_id, ts, status) VALUES ('a', 1, 'up')")
        conn.execute("INSERT INTO daily_runs VALUES ('2026-01-01', 'a', 'success', 1)")
        conn.execute("INSERT INTO storage_samples VALUES ('a', 'repo', 'r', '', 1, 5, NULL)")
        conn.execute("INSERT INTO storage_samples VALUES ('dashboard', 'local', '/', '', 1, 5, 10)")
        deleted = maintenance.delete_agent_history(conn, "a")
        assert deleted["runs"] == 1 and deleted["jobs"] == 1 and deleted["storage_samples"] == 1
        for table in ("agent_status_log", "daily_runs", "job_schedules"):
            assert count(conn, table) == 0, table
        assert [r[0] for r in conn.execute("SELECT id FROM agents")] == ["b"]
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
        assert maintenance.job_counts(conn) == [
            {"agent_id": "a", "agent": "a", "job_key": "J1", "job_name": "J1", "runs": 2}]
        assert maintenance.storage_series(conn) == [{"source": "a", "kind": "repo", "name": "r1", "samples": 1}]


class TestResolveJob:
    def test_a_key_or_a_name_finds_the_job(self, conn):
        seed_run(conn, job="id-1", job_name="Nightly")
        assert maintenance.resolve_job(conn, "a", "id-1") == "id-1"
        assert maintenance.resolve_job(conn, "a", "Nightly") == "id-1"

    def test_unknown_and_ambiguous_names_are_refused(self, conn):
        seed_run(conn, job="id-1", job_name="Same")
        seed_run(conn, job="id-2", job_name="Same")
        with pytest.raises(db.DbError, match="no job"):
            maintenance.resolve_job(conn, "a", "ghost")
        with pytest.raises(db.DbError, match="id-1, id-2"):
            maintenance.resolve_job(conn, "a", "Same")
        assert maintenance.resolve_job(conn, "a", "id-2") == "id-2"

    def test_other_agents_jobs_are_not_found(self, conn):
        seed_run(conn, agent="b", job="id-1")
        with pytest.raises(db.DbError):
            maintenance.resolve_job(conn, "a", "id-1")


class TestOrphans:
    def test_an_agent_missing_from_the_registry_is_reported_with_its_runs(self, conn):
        add_run(conn, agent="gone")
        add_run(conn, agent="here")
        found = maintenance.orphans(conn, ["here"])
        assert [(a["id"], a["runs"]) for a in found["agents"]] == [("gone", 1)] and found["jobs"] == []

    def test_a_job_the_agent_no_longer_reports_is_listed(self, conn):
        add_run(conn, agent="here", job="old")
        add_run(conn, agent="here", job="current")
        conn.execute("UPDATE jobs SET active = 0 WHERE job_key = 'old'")
        found = maintenance.orphans(conn, ["here"])
        assert [(j["job_key"], j["runs"]) for j in found["jobs"]] == [("old", 1)] and found["agents"] == []

    def test_nothing_is_orphaned_when_everything_is_configured(self, conn):
        add_run(conn, agent="here")
        assert maintenance.orphans(conn, ["here"]) == {"agents": [], "jobs": []}

    def test_an_empty_registry_reports_every_agent(self, conn):
        add_run(conn, agent="x")
        assert [a["id"] for a in maintenance.orphans(conn, [])["agents"]] == ["x"]


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
            assert [r["job_name"] for r in fresh.execute("SELECT job_name FROM run_view")] == ["before"]
        finally:
            fresh.close()
        safety = sqlite3.connect(result["safety_copy"])
        assert safety.execute("SELECT job_name FROM run_view").fetchall() == [("after",)]
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
        add_run(conn, agent=registered_id("nas1"), name="nas1")
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
        add_run(conn, agent=registered_id("nas1"), name="nas1")
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
        run_cli(capsys, "agent", "add", "nas1")
        db.init_db()
        conn = db.connect()
        agent_id = registered_id("nas1")
        add_run(conn, agent=agent_id, name="nas1", job="J")
        conn.execute("INSERT INTO storage_samples VALUES (?, 'repo', 'r', '', 1, 5, NULL)", (agent_id,))
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
        run_cli(capsys, "agent", "add", "a")
        db.init_db()
        conn = db.connect()
        agent_id = registered_id("a")
        first = add_run(conn, agent=agent_id, name="a", job="J")
        second = add_run(conn, agent=agent_id, name="a", job="J")
        conn.execute("UPDATE runs SET started_at = id")
        conn.close()
        code, out, _ = run_cli(capsys, "runs", "list", "--agent", "a", "--job", "J")
        assert code == 0
        lines = out.strip().splitlines()
        assert [line.split("\t")[0] for line in lines] == [str(second), str(first)]
        assert all(line.split("\t")[-1] == "-" or line.split("\t")[-1] == "" for line in lines)
        code, _, err = run_cli(capsys, "runs", "list", "--agent", "ghost")
        assert code == 1 and "not found" in err

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

    def test_runs_commands_accept_a_job_name_or_key(self, home, capsys):
        run_cli(capsys, "agent", "add", "a")
        db.init_db()
        conn = db.connect()
        seed_run(conn, agent=registered_id("a"), agent_name="a", job="id-1", job_name="Nightly")
        conn.close()
        assert "success" in run_cli(capsys, "runs", "list", "--agent", "a", "--job", "Nightly")[1]
        assert "success" in run_cli(capsys, "runs", "list", "--agent", "a", "--job", "id-1")[1]
        code, _, err = run_cli(capsys, "runs", "list", "--agent", "a", "--job", "nope")
        assert code == 1 and "no job" in err

    def test_orphans_lists_removed_agents(self, home, capsys):
        run_cli(capsys, "agent", "add", "a")
        db.init_db()
        conn = db.connect()
        add_run(conn, agent="removed-id", name="removed")
        conn.close()
        code, out, _ = run_cli(capsys, "orphans")
        assert code == 0 and "removed" in out and "removed-id" in out
        run_cli(capsys, "reset", "--yes")
        assert "no orphaned history" in run_cli(capsys, "orphans")[1]
