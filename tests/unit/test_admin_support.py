import argparse
import logging
import os
import stat
import subprocess
import sys

import pytest

from app import __main__ as cli
from app import admin, config, settings
from app.admin import checks, editor, logs, service, util
from app.admin.panes import PANES


def leaf_commands(parser, prefix=""):
    """'agent add' style paths of every runnable subcommand in the argparse tree."""
    leaves = []
    for action in parser._actions:  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                below = leaf_commands(sub, path)
                leaves += below or [path]
    return leaves


class TestParity:
    """The console must be able to do everything the command line can (README: 'Command line')."""

    def test_every_subcommand_has_a_console_action(self):
        missing = [c for c in leaf_commands(cli.build_parser()) if c not in admin.COVERAGE and c not in admin.NOT_COVERED]
        assert not missing, f"add these to admin.COVERAGE (and a tab action): {missing}"

    def test_no_stale_entries(self):
        leaves = set(leaf_commands(cli.build_parser()))
        stale = [c for c in list(admin.COVERAGE) + list(admin.NOT_COVERED) if c not in leaves]
        assert not stale, f"not subcommands any more: {stale}"

    def test_every_action_id_points_at_a_real_method_on_a_tab(self):
        registered = {}
        for pane in PANES:
            for action_id, method in pane.ACTIONS.items():
                assert callable(getattr(pane, method, None)), f"{pane.__name__}.{method} does not exist"
                registered[action_id] = pane
        for command, action_id in admin.COVERAGE.items():
            assert action_id in registered, f"'{command}' maps to unknown action '{action_id}'"

    def test_tab_ids_and_numbers_are_unique(self):
        assert len({p.TAB_ID for p in PANES}) == len(PANES)
        assert [p.TAB_TITLE.split()[0] for p in PANES] == [str(i) for i in range(1, len(PANES) + 1)]


class TestRunGuards:
    def test_root_is_refused_when_the_install_belongs_to_someone_else(self, home, monkeypatch):
        (home / "data").mkdir()
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        message = admin.check_user()
        if os.stat(home / "data").st_uid == 0:
            assert message is None
        else:
            assert "not as root" in message and "sudo -u" in message

    def test_a_user_who_cannot_write_the_database_is_told_to_switch(self, home):
        if os.geteuid() == 0:
            pytest.skip("root can always write")
        (home / "data").mkdir()
        settings.DB_PATH.write_text("")
        settings.DB_PATH.chmod(0o444)
        assert "not writable" in admin.check_user()

    def test_a_normal_user_with_a_writable_install_is_fine(self, home):
        assert admin.check_user() is None

    def test_missing_textual_is_explained(self, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "textual", None)
        assert admin.run() == 1
        assert "textual" in capsys.readouterr().err

    def test_a_problem_user_stops_the_console_before_it_starts(self, home, monkeypatch, capsys):
        monkeypatch.setattr(admin, "check_user", lambda: "run it as someone else")
        assert admin.run() == 1
        assert "someone else" in capsys.readouterr().err


class TestEditor:
    def test_visual_beats_editor_and_arguments_are_kept(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "nano -w")
        monkeypatch.setenv("EDITOR", "vim")
        monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert editor.find_editor() == ["nano", "-w"]

    def test_an_unusable_variable_falls_through_to_the_system_default(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "")
        monkeypatch.setenv("EDITOR", "not-installed")
        monkeypatch.setattr(editor.shutil, "which", lambda name: "/usr/bin/sensible-editor" if name == "sensible-editor" else None)
        assert editor.find_editor() == ["/usr/bin/sensible-editor"]

    def test_falls_back_to_common_editors_in_order(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(editor.shutil, "which", lambda name: f"/bin/{name}" if name in ("vim", "vi") else None)
        assert editor.find_editor() == ["/bin/vim"]

    def test_none_found(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(editor.shutil, "which", lambda name: None)
        assert editor.find_editor() == []
        with pytest.raises(editor.EditorError, match="no editor"):
            editor.run_editor(settings.CONFIG_PATH)

    def test_runs_the_editor_on_the_file(self, tmp_path, monkeypatch):
        script = tmp_path / "ed.sh"
        script.write_text('#!/bin/sh\necho "edited" >> "$1"\n')
        script.chmod(0o755)
        monkeypatch.setenv("VISUAL", str(script))
        target = tmp_path / "f.txt"
        target.write_text("start\n")
        assert editor.run_editor(target) == 0
        assert target.read_text() == "start\nedited\n"


class TestChecks:
    def test_env_permissions_are_checked(self, home):
        entry = next(e for e in checks.managed_files() if e["key"] == "env")
        settings.ENV_PATH.write_text("A=1\n")
        settings.ENV_PATH.chmod(0o644)
        assert checks.check_file(entry)["state"] == "warn"
        settings.ENV_PATH.chmod(0o640)
        assert checks.check_file(entry)["state"] == "warn"
        settings.ENV_PATH.chmod(0o600)
        assert checks.check_file(entry)["state"] == "ok"

    def test_world_readable_agents_file_is_flagged(self, home):
        config.add_agent("a")
        entry = next(e for e in checks.managed_files() if e["key"] == "agents")
        assert checks.check_file(entry)["state"] == "ok"
        settings.AGENTS_PATH.chmod(0o644)
        assert checks.check_file(entry)["state"] == "warn"

    def test_missing_invalid_and_valid(self, home):
        entry = next(e for e in checks.managed_files() if e["key"] == "config")
        assert checks.check_file(entry)["state"] == "ok"
        settings.CONFIG_PATH.write_text("server:\n  prot: 1\n")
        result = checks.check_file(entry)
        assert result["state"] == "error" and "server.prot" in result["message"]
        settings.CONFIG_PATH.unlink()
        assert checks.check_file(entry)["state"] == "missing"

    def test_revert_restores_content_and_deletes_files_that_did_not_exist(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"old")
        before = checks.snapshot(path)
        path.write_bytes(b"new")
        checks.revert(path, before)
        assert path.read_bytes() == b"old"
        checks.revert(path, None)
        assert not path.exists()

    def test_revert_keeps_the_files_mode(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"old")
        path.chmod(0o600)
        checks.revert(path, b"older")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_only_config_has_a_starter_text(self, home):
        (settings.CONFIG_PATH.parent / "config.example.yaml").write_text("x: 1\n")
        entries = {e["key"]: e for e in checks.managed_files()}
        assert checks.starter_text(entries["config"]) == "x: 1\n"
        assert checks.starter_text(entries["env"]) is None and checks.starter_text(entries["agents"]) is None


class FakeSystemctl:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        for needle, text in self.outputs.items():
            if needle in " ".join(args):
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestService:
    def test_override_wins_and_loses_the_suffix(self):
        assert service.detect_base("jabs-dashboard-v2.service") == "jabs-dashboard-v2"

    def test_detects_the_unit_whose_working_directory_is_this_install(self, home, monkeypatch):
        fake = FakeSystemctl({
            "list-unit-files": "jabs-dashboard.service enabled\njabs-dashboard-v2.service enabled\njabs-dashboard-v2-tick.service static\n",
            "show jabs-dashboard.service": "WorkingDirectory=/elsewhere\n",
            f"show jabs-dashboard-v2.service": f"WorkingDirectory={home}\n",
        })
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "_run", fake)
        assert service.detect_base() == "jabs-dashboard-v2"

    def test_a_single_candidate_is_used_when_nothing_matches(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "_run", FakeSystemctl({"list-unit-files": "jabs-dashboard.service enabled\n"}))
        assert service.detect_base() == "jabs-dashboard"

    def test_several_unmatched_candidates_are_not_guessed(self, home, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: True)
        monkeypatch.setattr(service, "_run", FakeSystemctl(
            {"list-unit-files": "jabs-dashboard.service enabled\njabs-dashboard-v2.service enabled\n"}))
        assert service.detect_base() is None

    def test_not_on_systemd(self, monkeypatch):
        monkeypatch.setattr(service, "available", lambda: False)
        assert service.detect_base() is None

    def test_unit_status_text(self, monkeypatch):
        monkeypatch.setattr(service, "_run", FakeSystemctl({"show": (
            "ActiveState=active\nSubState=running\nUnitFileState=enabled\nActiveEnterTimestamp=Fri 2026-11-13\n"
            "NextElapseUSecRealtime=\nResult=success\n")}))
        status = service.unit_status("x.service")
        assert status["active"] == "active (running)" and status["enabled"] == "enabled" and status["next"] == "-"

    def test_a_failed_unit_shows_its_result(self, monkeypatch):
        monkeypatch.setattr(service, "_run", FakeSystemctl({"show": "ActiveState=failed\nSubState=failed\nResult=exit-code\n"}))
        assert "last result exit-code" in service.unit_status("x.service")["active"]

    def test_unknown_unit(self, monkeypatch):
        monkeypatch.setattr(service, "_run", FakeSystemctl({}))
        assert service.unit_status("x.service")["active"] == "unknown"

    def test_sudo_is_added_for_normal_users_only(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert service.control_command("restart", "u.service") == ["sudo", "systemctl", "restart", "u.service"]
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert service.control_command("stop", "u.service") == ["systemctl", "stop", "u.service"]

    def test_only_known_actions_are_allowed(self):
        with pytest.raises(ValueError):
            service.control_command("mask", "u.service")

    def test_journal_command_follows_the_unit(self):
        command = service.journal_command("u.service")
        assert command[:3] == ["journalctl", "-u", "u.service"] and "-f" in command


class TestSupport:
    def test_ago(self):
        assert util.ago(None) == "-"
        assert util.ago(990, now=1000) == "10 s ago"
        assert util.ago(1000 - 600, now=1000) == "10 min ago"
        assert util.ago(1000 - 7200, now=1000) == "2 h ago"
        assert util.ago(1000 - 5 * 86400, now=1000) == "5 d ago"

    def test_capture_keeps_records_off_the_terminal(self, capsys):
        root = logging.getLogger()
        before = list(root.handlers)
        root.addHandler(logging.StreamHandler(sys.stderr))
        try:
            capture = logs.install()
            logging.getLogger("x").warning("careful")
            assert capture.drain() == ["WARNING x: careful"] and capture.drain() == []
            assert "careful" not in capsys.readouterr().err
        finally:
            root.handlers[:] = before

    def test_audit_lines_name_the_user_and_the_details(self, caplog):
        with caplog.at_level(logging.INFO, logger=logs.AUDIT_LOGGER):
            logs.audit("agent added", name="nas1")
        assert "agent added by" in caplog.text and "name=nas1" in caplog.text
