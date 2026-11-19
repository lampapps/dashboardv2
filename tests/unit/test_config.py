import re
import pytest

from app import config

from tests.unit.conftest import REPO_ROOT


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestConfig:
    def test_example_config_is_valid(self):
        cfg = config.load_config(REPO_ROOT / "config" / "config.example.yaml")
        assert cfg.server.port == 5002
        assert cfg.storage.drives[0].path == "/"

    def test_empty_file_uses_defaults(self, tmp_path):
        cfg = config.load_config(write(tmp_path / "c.yaml", ""))
        assert cfg.timezone == "UTC"
        assert cfg.retention.runs_days == 30

    def test_missing_file_points_at_the_example(self, tmp_path):
        with pytest.raises(config.ConfigError, match="config.example.yaml"):
            config.load_config(tmp_path / "nope.yaml")

    def test_unknown_key_is_rejected_with_its_location(self, tmp_path):
        path = write(tmp_path / "c.yaml", "server:\n  prot: 5000\n")
        with pytest.raises(config.ConfigError, match=r"server\.prot"):
            config.load_config(path)

    @pytest.mark.parametrize("text", [
        "timezone: Mars/Base\n",
        "server:\n  port: 70000\n",
        "email:\n  digest:\n    schedule: '0 8 *'\n",
        "retention:\n  runs_days: 0\n",
    ])
    def test_invalid_values_are_rejected(self, tmp_path, text):
        with pytest.raises(config.ConfigError):
            config.load_config(write(tmp_path / "c.yaml", text))

    def test_invalid_yaml_is_a_config_error(self, tmp_path):
        with pytest.raises(config.ConfigError, match="not valid YAML"):
            config.load_config(write(tmp_path / "c.yaml", "a: [1, 2\n"))

    def test_non_mapping_top_level_is_rejected(self, tmp_path):
        with pytest.raises(config.ConfigError, match="mapping"):
            config.load_config(write(tmp_path / "c.yaml", "- a\n"))


class TestBadges:
    def test_http_badge_with_link_is_accepted(self, tmp_path):
        path = write(tmp_path / "c.yaml", (
            "badges:\n  - label: NAS\n    image_url: http://kuma.lan/api/badge/1/status\n"
            "    link: https://kuma.lan/\n"))
        (badge,) = config.load_config(path).badges
        assert badge.link == "https://kuma.lan/"

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x/y.png", "/relative.png", "http://"])
    def test_non_http_urls_are_rejected(self, tmp_path, url):
        path = write(tmp_path / "c.yaml", f"badges:\n  - label: X\n    image_url: '{url}'\n")
        with pytest.raises(config.ConfigError):
            config.load_config(path)


ID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class TestAgents:
    def test_missing_file_means_no_agents(self, tmp_path):
        assert config.load_agents(tmp_path / "agents.yaml") == []

    def test_add_agent_stores_only_a_hash_of_the_key(self, tmp_path):
        path = tmp_path / "agents.yaml"
        key = config.add_agent("nas1", grace_minutes=120, notes="NAS", path=path)

        assert key not in path.read_text()
        (agent,) = config.load_agents(path)
        assert agent.name == "nas1"
        assert agent.key_hash == config.hash_key(key)
        assert (agent.grace_minutes, agent.notes, agent.enabled) == (120, "NAS", True)

    def test_add_keeps_existing_agents(self, tmp_path):
        path = tmp_path / "agents.yaml"
        config.add_agent("a", path=path)
        config.add_agent("b", path=path)
        assert [a.name for a in config.load_agents(path)] == ["a", "b"]

    def test_keys_are_unique_and_long(self, tmp_path):
        path = tmp_path / "agents.yaml"
        keys = {config.add_agent(f"a{i}", path=path) for i in range(5)}
        assert len(keys) == 5
        assert all(len(k) == 40 for k in keys)

    def test_duplicate_name_is_rejected_and_file_unchanged(self, tmp_path):
        path = tmp_path / "agents.yaml"
        config.add_agent("a", path=path)
        before = path.read_text()
        with pytest.raises(config.ConfigError, match="already exists"):
            config.add_agent("a", path=path)
        assert path.read_text() == before

    @pytest.mark.parametrize("kwargs", [
        {"name": "has space"}, {"name": "-leading"}, {"name": "ok", "allowed_ip": "not-an-ip"},
        {"name": "ok", "grace_minutes": 0},
    ])
    def test_invalid_agent_is_rejected(self, tmp_path, kwargs):
        with pytest.raises(config.ConfigError):
            config.add_agent(path=tmp_path / "agents.yaml", **kwargs)

    def test_duplicate_hashes_in_the_file_are_rejected(self, tmp_path):
        h = "a" * 64
        path = write(tmp_path / "agents.yaml", (
            f"agents:\n  - id: {ID_A}\n    name: a\n    key_hash: {h}\n"
            f"  - id: {ID_B}\n    name: b\n    key_hash: {h}\n"))
        with pytest.raises(config.ConfigError, match="unique"):
            config.load_agents(path)

    def test_every_agent_gets_an_id_that_is_not_its_name_or_key(self, tmp_path):
        path = tmp_path / "agents.yaml"
        key = config.add_agent("a", path=path)
        (agent,) = config.load_agents(path)
        assert re.fullmatch(config.AGENT_ID_PATTERN, agent.id)
        assert agent.id not in (agent.name, key, agent.key_hash)
        assert config.add_agent("b", path=path) and config.load_agents(path)[1].id != agent.id

    def test_duplicate_ids_are_rejected(self, tmp_path):
        path = write(tmp_path / "agents.yaml", (
            f"agents:\n  - id: {ID_A}\n    name: a\n    key_hash: {'a' * 64}\n"
            f"  - id: {ID_A}\n    name: b\n    key_hash: {'b' * 64}\n"))
        with pytest.raises(config.ConfigError, match="unique"):
            config.load_agents(path)

    def test_an_agent_without_an_id_is_rejected_with_the_fix(self, tmp_path):
        path = write(tmp_path / "agents.yaml", f"agents:\n  - name: a\n    key_hash: {'a' * 64}\n")
        with pytest.raises(config.ConfigError, match="agent adopt"):
            config.load_agents(path)

    def test_adopt_gives_missing_ids_and_keeps_everything_else(self, tmp_path):
        path = write(tmp_path / "agents.yaml", (
            f"# my agents\nagents:\n  - id: {ID_A}\n    name: a\n    key_hash: {'a' * 64}\n"
            f"  - name: b\n    key_hash: {'b' * 64}\n    notes: keep me\n"))
        assert config.adopt_agents(path) == ["b"]
        a, b = config.load_agents(path)
        assert a.id == ID_A and b.id != ID_A and b.notes == "keep me"
        assert config.adopt_agents(path) == []

    def test_rename_changes_only_the_name(self, tmp_path):
        path = tmp_path / "agents.yaml"
        key = config.add_agent("old", path=path)
        before = config.load_agents(path)[0]
        config.rename_agent("old", "new", path=path)
        (after,) = config.load_agents(path)
        assert (after.name, after.id, after.key_hash) == ("new", before.id, config.hash_key(key))
        assert config.find_agent("new", path).id == before.id

    def test_hand_edited_file_without_a_hash_is_rejected(self, tmp_path):
        path = write(tmp_path / "agents.yaml", "agents:\n  - name: a\n    key: plaintext\n")
        with pytest.raises(config.ConfigError):
            config.load_agents(path)
