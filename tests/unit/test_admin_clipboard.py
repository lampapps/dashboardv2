import contextlib
import subprocess

import pytest
from textual.widgets import Button

from app.admin import clipboard, dialogs
from app.admin.app import AdminApp

from tests.unit.test_admin_console import dialog_text, dialog_title, drive, press, wait_dialog


class TestCopyWithTool:
    def test_uses_the_first_program_that_matches_the_session(self, monkeypatch):
        calls = []
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}" if name != "wl-copy" else None)
        monkeypatch.setattr(clipboard.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
        assert clipboard.copy_with_tool("secret") == "xclip"
        command, kwargs = calls[0]
        assert command == ["xclip", "-selection", "clipboard"] and kwargs["input"] == "secret"
        assert "secret" not in " ".join(command)

    def test_wayland_wins_when_both_are_present(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(clipboard.subprocess, "run", lambda command, **kwargs: None)
        assert clipboard.copy_with_tool("x") == "wl-copy"

    def test_nothing_without_a_desktop_session(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert clipboard.copy_with_tool("x") is None

    def test_nothing_without_a_program(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
        assert clipboard.copy_with_tool("x") is None

    def test_a_failing_program_falls_through_to_the_next(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
        seen = []

        def run(command, **kwargs):
            seen.append(command[0])
            if command[0] == "xclip":
                raise subprocess.CalledProcessError(1, command)

        monkeypatch.setattr(clipboard.subprocess, "run", run)
        assert clipboard.copy_with_tool("x") == "xsel" and seen == ["xclip", "xsel"]


class TestPasteWithTool:
    def test_reads_the_clipboard_through_the_session_program(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(clipboard.subprocess, "run",
                            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="abc"))
        assert clipboard.paste_with_tool() == "abc"

    def test_nothing_without_a_session(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert clipboard.paste_with_tool() is None


class TestMessageDialog:
    @pytest.fixture
    def shown(self, monkeypatch):
        """Record what the copy actions do instead of touching a clipboard or the terminal."""
        record = {"osc52": [], "tool": None, "plain": [], "suspended": 0, "notes": []}
        monkeypatch.setattr(dialogs.clipboard, "copy_with_tool", lambda text: record["tool"])
        monkeypatch.setattr(AdminApp, "copy_to_clipboard", lambda self, text: record["osc52"].append(text))
        monkeypatch.setattr(dialogs, "_print_plain", lambda text: record["plain"].append(text))
        monkeypatch.setattr(dialogs.MessageDialog, "notify",
                            lambda self, message, **kwargs: record["notes"].append((message, kwargs.get("severity"))))

        @contextlib.contextmanager
        def suspend(self):
            record["suspended"] += 1
            yield

        monkeypatch.setattr(AdminApp, "suspend", suspend)
        return record

    def open(self, app):
        app.run_worker(app.show("Key", "body", copy_text="JABS_AGENT_KEY=abc"), exit_on_error=True)

    def test_copy_uses_a_clipboard_program_when_there_is_one_and_the_terminal_too(self, shown):
        shown["tool"] = "xclip"

        async def scenario(app, pilot):
            self.open(app)
            await wait_dialog(pilot, app, "Key")
            await press(pilot, app, "copy")
            assert shown["osc52"] == ["JABS_AGENT_KEY=abc"]
            assert shown["notes"] == [("Copied to the clipboard (xclip)", None)]

        drive(scenario)

    def test_copy_without_a_program_warns_and_points_at_show_in_terminal(self, shown):
        async def scenario(app, pilot):
            self.open(app)
            await wait_dialog(pilot, app, "Key")
            await press(pilot, app, "copy")
            assert shown["osc52"] == ["JABS_AGENT_KEY=abc"]
            (message, severity), = shown["notes"]
            assert "Show in terminal" in message and severity == "warning"

        drive(scenario)

    def test_show_in_terminal_prints_the_text_with_the_console_suspended(self, shown):
        async def scenario(app, pilot):
            self.open(app)
            await wait_dialog(pilot, app, "Key")
            await press(pilot, app, "plain")
            assert shown["plain"] == ["JABS_AGENT_KEY=abc"] and shown["suspended"] == 1
            assert dialog_title(app) == "Key"  # back in the dialog afterwards

        drive(scenario)

    def test_a_dialog_without_copy_text_has_neither_button(self, shown):
        async def scenario(app, pilot):
            app.run_worker(app.show("Plain", "body"), exit_on_error=True)
            await wait_dialog(pilot, app, "Plain")
            assert not app.screen.query("#copy") and not app.screen.query("#plain")
            assert app.screen.query_one("#close", Button)
            assert "body" in dialog_text(app)

        drive(scenario)

    def test_print_plain_survives_a_closed_stdin(self, monkeypatch, capsys):
        def no_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", no_input)
        dialogs._print_plain("the key")
        assert "the key" in capsys.readouterr().out
