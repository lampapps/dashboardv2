import sqlite3

import pytest

from app import db

from ..seed import add_run, ensure_agent


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def _run(conn, agent="a", run_id="r1"):
    return add_run(conn, agent=agent, run_id=run_id)


def test_init_creates_the_latest_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "t.sqlite"
    assert db.init_db(path) == len(db.MIGRATIONS)
    assert db.init_db(path) == len(db.MIGRATIONS)
    conn = db.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert {"meta", "agents", "jobs", "targets", "runs", "events", "job_schedules", "agent_status_log"} <= tables


def test_database_uses_wal(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_database_newer_than_the_code_is_refused(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(db.DbError, match="newer"):
        db.init_db(path)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", [["CREATE TABLE a (x)", "THIS IS NOT SQL"]])
    path = tmp_path / "t.sqlite"
    with pytest.raises(db.DbError, match="migration 1"):
        db.init_db(path)
    conn = db.connect(path)
    assert db.schema_version(conn) == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchone() is None
    conn.close()


def test_deleting_a_run_cascades_to_its_events(conn):
    _run(conn)
    run_pk = conn.execute("SELECT id FROM runs").fetchone()[0]
    conn.execute("INSERT INTO events (run_pk, timestamp, created_at) VALUES (?, 1, 1)", (run_pk,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_pk,))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_deleting_an_agent_removes_everything_it_owns(conn):
    run_pk = _run(conn, "a")
    _run(conn, "b")
    conn.execute("INSERT INTO events (run_pk, timestamp, created_at) VALUES (?, 1, 1)", (run_pk,))
    conn.execute("INSERT INTO agent_status_log (agent_id, ts, status) VALUES ('a', 1, 'up')")
    conn.execute("DELETE FROM agents WHERE id = 'a'")
    for table in ("jobs", "targets", "runs", "events", "agent_status_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE 1").fetchone()[0] == (1 if table in ("jobs", "targets", "runs") else 0)


def test_a_job_cannot_exist_without_its_agent(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (agent_id, job_key, name, updated_at) VALUES ('ghost', 'j', 'j', 1)")


def test_renaming_a_job_or_agent_leaves_runs_attached(conn):
    run_pk = _run(conn)
    conn.execute("UPDATE jobs SET name = 'renamed'")
    conn.execute("UPDATE agents SET name = 'renamed'")
    row = conn.execute("SELECT agent_id, job_key, job_name FROM run_view WHERE id = ?", (run_pk,)).fetchone()
    assert tuple(row) == ("a", "J", "renamed")


def test_run_id_is_unique_per_agent_but_not_across_agents(conn):
    _run(conn, "a", "r1")
    _run(conn, "b", "r1")
    with pytest.raises(sqlite3.IntegrityError):
        _run(conn, "a", "r1")


def test_runs_without_run_id_may_repeat(conn):
    for _ in range(2):
        add_run(conn)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_status_log_rejects_unknown_status(conn):
    ensure_agent(conn, "a")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent_status_log (agent_id, ts, status) VALUES ('a', 1, 'sleeping')")


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            db.set_meta(conn, "k", "v")
            raise RuntimeError("boom")
    assert db.get_meta(conn, "k") is None


def test_meta_roundtrip_and_overwrite(conn):
    assert db.get_meta(conn, "k", "dflt") == "dflt"
    db.set_meta(conn, "k", "1")
    db.set_meta(conn, "k", "2")
    assert db.get_meta(conn, "k") == "2"


def test_bump_rev_counts_up_from_one(conn):
    for expected in ("1", "2", "3"):
        db.bump_rev(conn)
        assert db.get_meta(conn, "rev") == expected


def test_backup_is_a_consistent_copy_and_never_overwrites(tmp_path, conn):
    _run(conn)
    source = tmp_path / "t.sqlite"
    dest = db.backup(tmp_path / "out" / "copy.sqlite", source)

    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    copy.close()
    with pytest.raises(db.DbError, match="already exists"):
        db.backup(dest, source)
