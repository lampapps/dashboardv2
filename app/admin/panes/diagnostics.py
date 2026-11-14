"""Diagnostics tab: run the tick, test email, preview the digest and alerts, check an API key, view internal state."""

import asyncio
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, RichLog

from ... import config, db, mailer, maintenance, notify, settings, tick
from ..app import Pane
from ..logs import audit
from ..util import ago, run_db

DAY_S = 86400


def _config_and_agents() -> tuple:
    return config.load_config(), config.load_agents()


def _run_dev_server() -> None:
    try:
        subprocess.run([sys.executable, "-m", "app", "web", "--dev"], check=False)
    except KeyboardInterrupt:
        pass


def find_key_owner(key: str):
    """The registered agent whose key this is, or None; the key itself is never kept."""
    if not key:
        raise ValueError("paste an API key")
    digest = config.hash_key(key)
    return next((a for a in config.load_agents() if a.key_hash == digest), None)


class DiagnosticsPane(Pane):
    TAB_ID = "diagnostics"
    TAB_TITLE = "4 Diagnostics"
    ACTIONS = {
        "tick": "do_tick", "testmail": "do_testmail", "web": "do_dev_server", "meta": "do_meta",
        "diagnostics.digest": "do_digest", "diagnostics.alert": "do_alert", "diagnostics.key": "do_key",
    }
    BINDINGS = [
        Binding("t", "tick", "Run tick"), Binding("m", "testmail", "Test mail"), Binding("p", "digest", "Digest"),
        Binding("a", "alert", "Alert mail"), Binding("k", "key", "Check key"), Binding("e", "meta", "State"),
        Binding("w", "dev_server", "Dev server"), Binding("c", "clear", "Clear"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(classes="buttons"):
            yield Button("Run tick", id="tick", variant="primary")
            yield Button("Test mail", id="testmail")
            yield Button("Digest", id="digest")
            yield Button("Alert mail", id="alert")
            yield Button("Check key", id="key")
            yield Button("State", id="meta")
            yield Button("Dev server", id="dev_server")
            yield Button("Clear", id="clear")
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)

    async def reload(self) -> None:
        return None

    # ---------------------------------------------------------------- output

    def say(self, text: str = "") -> None:
        self.query_one(RichLog).write(text)

    def heading(self, text: str) -> None:
        self.say(f"\n=== {datetime.now():%H:%M:%S}  {text}")

    def flush_logs(self) -> None:
        for line in self.admin.capture.drain():
            self.say(f"  log: {line}")

    # ---------------------------------------------------------------- actions

    async def do_tick(self) -> None:
        cfg = await asyncio.to_thread(config.load_config)
        ok, _ = await self.admin.confirm(
            "Run the periodic tasks", "Runs every task now, exactly like the timer: rollups, status changes, offline "
            "alerts, the digest and sampling. Emails that are due will really be sent.", confirm="Run now")
        if not ok:
            return
        self.heading("tick")

        def work() -> list:
            db.init_db()
            return run_db(tick.run_once, cfg)

        results = await asyncio.to_thread(work)
        for result in results:
            self.say(f"  {result['name']:<16} {result['seconds'] * 1000:6.0f} ms  {result['error'] or 'ok'}")
        self.flush_logs()
        audit("tick run manually", failures=sum(1 for r in results if r["error"]))

    async def do_testmail(self) -> None:
        cfg = await asyncio.to_thread(config.load_config)
        self.heading("test mail")
        if not mailer.is_configured(cfg.email):
            self.say("  email is not configured: set email.smtp_server and email.to_addrs in config.yaml")
            return
        hint = mailer.smtp_login_hint(cfg.email)
        if hint:
            self.say(f"  note: {hint}")
        text, html = mailer.render("test", server=cfg.email.smtp_server, port=cfg.email.smtp_port,
                                   recipients=", ".join(cfg.email.to_addrs),
                                   generated=datetime.now().strftime("%Y-%m-%d %H:%M"))
        sent = await asyncio.to_thread(mailer.send, cfg.email, "JABS test message", text, html)
        self.say(f"  sent to {', '.join(cfg.email.to_addrs)}" if sent else "  sending FAILED")
        self.flush_logs()
        audit("test mail", sent=sent)

    def _digest(self) -> tuple:
        cfg, agents = _config_and_agents()
        now = time.time()

        def build(conn):
            data = notify.digest_data(conn, cfg, agents, now - DAY_S, now)
            text, _html = mailer.render("digest", **data)
            return notify.digest_subject(data, now, ZoneInfo(cfg.timezone)), text

        subject, text = run_db(build)
        return cfg, subject, text

    async def do_digest(self) -> None:
        cfg, subject, text = await asyncio.to_thread(self._digest)
        self.heading("digest preview (last 24 hours)")
        self.say(f"Subject: {subject}\n")
        self.say(text)
        if not mailer.is_configured(cfg.email):
            return
        ok, _ = await self.admin.confirm("Send the digest", f"Send this digest now to {', '.join(cfg.email.to_addrs)}? "
                                         "It does not change when the next scheduled digest goes out.", confirm="Send")
        if not ok:
            return
        now = time.time()
        result = await asyncio.to_thread(run_db, notify.send_digest, cfg, now - DAY_S, now)
        self.say("  nothing to report, not sent" if result is None else ("  sent" if result else "  sending FAILED"))
        self.flush_logs()
        audit("digest sent manually", result=result)

    async def do_alert(self) -> None:
        cfg, agents = await asyncio.to_thread(_config_and_agents)
        if not agents:
            await self.admin.show("Alert mail", "No agents are registered.")
            return
        name = await self.admin.pick("Preview the offline alert for...", [(a.name, a.name) for a in agents])
        if not name:
            return
        agent = next(a for a in agents if a.name == name)
        subject, text, html = await asyncio.to_thread(run_db, notify.preview_offline_alert, cfg, agent, time.time())
        self.heading(f"offline alert preview for {name}")
        self.say(f"Subject: {subject}\n")
        self.say(text)
        if not mailer.is_configured(cfg.email):
            return
        ok, _ = await self.admin.confirm("Send as a test", f"Send this to {', '.join(cfg.email.to_addrs)} marked [TEST]? "
                                         "It does not change the agent's status or alert counters.", confirm="Send")
        if ok:
            sent = await asyncio.to_thread(mailer.send, cfg.email, f"[TEST] {subject}", text, html)
            self.say("  sent" if sent else "  sending FAILED")
            self.flush_logs()
            audit("test alert mail", agent=name, sent=sent)

    async def do_key(self) -> None:
        fields = [{"key": "key", "label": "API key to look up", "kind": "password",
                   "help": "Checked against the stored hashes and then discarded."}]

        def submit(values: dict) -> dict:
            return {"agent": find_key_owner(values["key"])}

        result = await self.admin.form("Which agent has this key?", fields, submit, "Check")
        if result is None:
            return
        agent = result["agent"]
        self.heading("key check")
        if agent is None:
            self.say("  no registered agent has this key (it is wrong, was replaced by a new key, or the agent was removed)")
            return
        problems = []
        if not agent.enabled:
            problems.append("the agent is DISABLED, so the key is rejected")
        if agent.allowed_ip:
            problems.append(f"it is only accepted from {agent.allowed_ip}")
        self.say(f"  belongs to agent '{agent.name}'" + ("; " + "; ".join(problems) if problems else "; it will be accepted"))

    async def do_meta(self) -> None:
        items = await asyncio.to_thread(run_db, maintenance.meta_items)
        self.heading("internal state")
        for key, value in items:
            extra = ""
            if key.endswith("_ts") or key == notify.DIGEST_LAST_SENT:
                try:
                    extra = f"  ({ago(float(value))})"
                except ValueError:
                    pass
            self.say(f"  {key} = {value}{extra}")
        if not items:
            self.say("  (empty)")

    async def do_dev_server(self) -> None:
        ok, _ = await self.admin.confirm(
            "Run the development server", f"Starts `python -m app web --dev` in this terminal using {settings.BASE_DIR}. "
            "The console is paused until you press Ctrl+C.", confirm="Start")
        if ok:
            self.admin.run_external(_run_dev_server)

    # ---------------------------------------------------------------- bindings and buttons

    def action_tick(self) -> None:
        self.admin.spawn(self.do_tick)

    def action_testmail(self) -> None:
        self.admin.spawn(self.do_testmail)

    def action_digest(self) -> None:
        self.admin.spawn(self.do_digest)

    def action_alert(self) -> None:
        self.admin.spawn(self.do_alert)

    def action_key(self) -> None:
        self.admin.spawn(self.do_key)

    def action_meta(self) -> None:
        self.admin.spawn(self.do_meta)

    def action_dev_server(self) -> None:
        self.admin.spawn(self.do_dev_server)

    def action_clear(self) -> None:
        self.query_one(RichLog).clear()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        getattr(self, f"action_{event.button.id}")()
