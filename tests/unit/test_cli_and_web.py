import sqlite3

from app import __main__ as cli
from app import config, db, settings
from app.web import create_app


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


class TestCli:
    def test_initdb(self, home, capsys):
        code, out, _ = run_cli(capsys, "initdb")
        assert code == 0
        assert f"schema version {len(db.MIGRATIONS)}" in out
        assert settings.DB_PATH.exists()

    def test_config_reports_agents(self, home, capsys):
        code, out, _ = run_cli(capsys, "config")
        assert code == 0
        assert "0 agents" in out

    def test_config_fails_clearly_on_bad_yaml(self, home, capsys):
        settings.CONFIG_PATH.write_text("server:\n  prot: 1\n")
        code, _, err = run_cli(capsys, "config")
        assert code == 1
        assert "server.prot" in err

    def test_agent_add_prints_the_key_once_and_stores_its_hash(self, home, capsys):
        code, out, _ = run_cli(capsys, "agent", "add", "nas1", "--grace-minutes", "90")
        assert code == 0
        key = out.strip().rsplit(" ", 1)[-1]
        assert key not in settings.AGENTS_PATH.read_text()
        (agent,) = config.load_agents()
        assert agent.key_hash == config.hash_key(key)
        assert agent.grace_minutes == 90

    def test_agent_add_duplicate_fails(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        code, _, err = run_cli(capsys, "agent", "add", "nas1")
        assert code == 1
        assert "already exists" in err

    def test_agent_list(self, home, capsys):
        assert "no agents" in run_cli(capsys, "agent", "list")[1]
        run_cli(capsys, "agent", "add", "nas1", "--notes", "the NAS")
        out = run_cli(capsys, "agent", "list")[1]
        assert "nas1" in out and "enabled" in out and "the NAS" in out

    def test_bare_agent_command_is_a_usage_error(self, home, capsys):
        assert run_cli(capsys, "agent")[0] == 2

    def test_agent_disable_and_enable(self, home, capsys):
        run_cli(capsys, "agent", "add", "nas1")
        assert run_cli(capsys, "agent", "disable", "nas1")[0] == 0
        assert config.load_agents()[0].enabled is False
        assert "disabled" in run_cli(capsys, "agent", "list")[1]
        assert run_cli(capsys, "agent", "enable", "nas1")[0] == 0
        assert config.load_agents()[0].enabled is True

    def test_disabling_an_unknown_agent_fails(self, home, capsys):
        code, _, err = run_cli(capsys, "agent", "disable", "ghost")
        assert code == 1
        assert "not found" in err

    def test_tick_records_the_time_it_ran(self, home, capsys):
        assert run_cli(capsys, "tick")[0] == 0
        conn = db.connect()
        assert float(db.get_meta(conn, "last_tick_ts")) > 0
        conn.close()

    def test_backup_writes_a_database_copy(self, home, capsys):
        run_cli(capsys, "initdb")
        code, _, _ = run_cli(capsys, "backup")
        assert code == 0
        (copy,) = settings.BACKUP_DIR.glob("jabs-*.sqlite")
        conn = sqlite3.connect(copy)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        conn.close()

    def test_reset_requires_confirmation(self, home, capsys):
        run_cli(capsys, "initdb")
        conn = db.connect()
        db.set_meta(conn, "k", "v")
        conn.close()

        assert run_cli(capsys, "reset")[0] == 2
        conn = db.connect()
        assert db.get_meta(conn, "k") == "v"
        conn.close()

        assert run_cli(capsys, "reset", "--yes")[0] == 0
        conn = db.connect()
        assert db.get_meta(conn, "k") is None
        conn.close()


class TestHealthz:
    def test_ok_before_and_after_a_tick(self, home, capsys):
        db.init_db()
        client = create_app(config.load_config()).test_client()

        body = client.get("/healthz").get_json()
        assert body["status"] == "ok"
        assert body["tick_age_s"] is None

        run_cli(capsys, "tick")
        assert client.get("/healthz").get_json()["tick_age_s"] in (0, 1)

    def test_reports_503_when_the_database_is_unreadable(self, home, monkeypatch):
        def broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "connect", broken)
        client = create_app(config.load_config()).test_client()
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.get_json() == {"status": "error"}
