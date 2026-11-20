import io
import logging
import types

import pytest

from app import __main__ as cli
from app import config, db
from app.agents import AgentRegistry
from app.config import ConfigError
from app.web import create_app


def event():
    return {"version": "1.0", "agent_type": "Snapshot", "event_type": "heartbeat", "message": "hi",
            "job_id": "job-1", "target_id": "T"}


class TestCheckKey:
    def test_cli_reads_the_key_from_stdin(self, home, capsys, monkeypatch):
        key = config.add_agent("nas1")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(key + "\n"))
        assert cli.main(["agent", "check-key"]) == 0
        out = capsys.readouterr().out
        assert "belongs to agent 'nas1'" in out and "it will be accepted" in out and key not in out

    def test_cli_with_an_unknown_key(self, home, capsys, monkeypatch):
        config.add_agent("nas1")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("0" * 40))
        assert cli.main(["agent", "check-key"]) == 1
        assert "no registered agent has this key" in capsys.readouterr().out

    def test_cli_prompts_without_echo_on_a_terminal(self, home, capsys, monkeypatch):
        key = config.add_agent("nas1")
        config.set_agent_enabled("nas1", False)
        monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: key)
        assert cli.main(["agent", "check-key"]) == 0
        assert "DISABLED" in capsys.readouterr().out

    def test_an_empty_key_is_refused(self, home, capsys, monkeypatch):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))
        with pytest.raises(ValueError, match="paste an API key"):
            cli.main(["agent", "check-key"])


class TestRekey:
    def test_the_new_key_works_and_the_old_one_does_not(self, home):
        old = config.add_agent("nas1", host="P3Tiny", notes="the NAS", grace_minutes=30)
        new = config.rekey_agent("nas1")
        registry = AgentRegistry()
        assert new != old and len(new) == len(old)
        assert registry.find_by_key(old) is None
        agent = registry.find_by_key(new)
        assert (agent.name, agent.host, agent.notes, agent.grace_minutes, agent.enabled) == (
            "nas1", "P3Tiny", "the NAS", 30, True)

    def test_other_agents_keep_their_keys_and_the_order(self, home):
        keys = {name: config.add_agent(name) for name in ("a", "b", "c")}
        config.rekey_agent("b")
        assert [a.name for a in config.load_agents()] == ["a", "b", "c"]
        registry = AgentRegistry()
        assert registry.find_by_key(keys["a"]).name == "a" and registry.find_by_key(keys["c"]).name == "c"
        assert registry.find_by_key(keys["b"]) is None

    def test_a_disabled_agent_stays_disabled(self, home):
        config.add_agent("a")
        config.set_agent_enabled("a", False)
        config.rekey_agent("a")
        assert config.load_agents()[0].enabled is False

    def test_unknown_agent(self, home):
        config.add_agent("a")
        with pytest.raises(ConfigError, match="not found"):
            config.rekey_agent("ghost")

    def test_the_key_is_never_written_to_the_file(self, home):
        config.add_agent("a")
        key = config.rekey_agent("a")
        assert key not in (home / "config" / "agents.yaml").read_text()

    def test_cli(self, home, capsys):
        old = config.add_agent("a")
        assert cli.main(["agent", "rekey", "a"]) == 0
        out = capsys.readouterr().out
        new = out.split(": ")[-1].strip()
        assert old not in out and AgentRegistry().find_by_key(new).name == "a" and "old one no longer works" in out
        assert cli.main(["agent", "rekey", "ghost"]) == 1
        assert "not found" in capsys.readouterr().err

    def test_history_stays_with_the_agent(self, home):
        db.init_db()
        client = create_app(config.load_config()).test_client()
        old = config.add_agent("nas1")
        assert client.post("/api/v2/events", json=event(), headers={"X-API-Key": old}).status_code == 201
        agent_id = config.find_agent("nas1").id
        new = config.rekey_agent("nas1")
        assert config.find_agent("nas1").id == agent_id
        assert client.post("/api/v2/events", json=event(), headers={"X-API-Key": old}).status_code == 403
        assert client.post("/api/v2/events", json=event(), headers={"X-API-Key": new}).status_code == 201
        conn = db.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM agents WHERE id = ?", (agent_id,)).fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM jobs WHERE agent_id = ?", (agent_id,)).fetchone()[0] == 1
        finally:
            conn.close()


class TestInvalidRegistryFile:
    def write(self, home, text):
        (home / "config" / "agents.yaml").write_text(text, encoding="utf-8")

    def test_a_broken_file_at_startup_is_logged_clearly(self, home, caplog):
        key = config.add_agent("good")
        self.write(home, (home / "config" / "agents.yaml").read_text().replace("name: good", "name: bad name"))
        registry = AgentRegistry()
        with caplog.at_level(logging.ERROR, logger="app.agents"):
            assert registry.find_by_key(key) is None
        assert "no agents are loaded" in caplog.text and "python -m app config" in caplog.text
        assert "agents.0.name" in registry.error

    def test_a_file_that_breaks_while_running_keeps_the_last_good_agents(self, home, caplog):
        key = config.add_agent("good")
        registry = AgentRegistry()
        assert registry.find_by_key(key).name == "good"
        self.write(home, "agents: not-a-list\n")
        with caplog.at_level(logging.ERROR, logger="app.agents"):
            assert registry.find_by_key(key).name == "good"
        assert "keeping the previous agents" in caplog.text and registry.error

    def test_fixing_the_file_recovers_and_logs_once(self, home, caplog):
        key = config.add_agent("good")
        good = (home / "config" / "agents.yaml").read_text()
        self.write(home, good.replace("name: good", "name: bad name"))
        registry = AgentRegistry()
        registry.agents()
        self.write(home, good + "\n# fixed\n")
        with caplog.at_level(logging.INFO, logger="app.agents"):
            assert registry.find_by_key(key).name == "good"
            registry.agents()
        assert registry.error is None and caplog.text.count("valid again") == 1

    def test_the_error_is_logged_once_per_edit_not_per_request(self, home, caplog):
        config.add_agent("good")
        self.write(home, "agents: not-a-list\n")
        registry = AgentRegistry()
        with caplog.at_level(logging.ERROR, logger="app.agents"):
            for _ in range(5):
                registry.agents()
        assert caplog.text.count("agents.yaml is invalid") == 1

    def test_the_web_app_logs_the_problem_at_startup_and_on_rejected_keys(self, home, caplog):
        db.init_db()
        key = config.add_agent("good")
        self.write(home, "agents: not-a-list\n")
        with caplog.at_level(logging.WARNING):
            client = create_app(config.load_config()).test_client()
            assert "no agents are loaded" in caplog.text
            resp = client.post("/api/v2/events", json=event(), headers={"X-API-Key": key})
        assert resp.status_code == 403 and resp.get_json() == {"error": "Invalid API key"}
        assert "rejected an agent key while agents.yaml is invalid" in caplog.text
