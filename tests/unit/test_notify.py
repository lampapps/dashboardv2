import smtplib
from contextlib import closing

import pytest
from pydantic import ValidationError

from app import __main__ as cli
from app import activity, config, db, mailer, notify, settings, tick
from app.config import AgentConfig, Config

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
DAY = 86400.0


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def make_config(email=None, **kw) -> Config:
    base = {"smtp_server": "smtp.test", "to_addrs": ["admin@example.com"]}
    return Config(timezone="UTC", email={**base, **(email or {})}, **kw)


def agent(name="nas1", **kw) -> AgentConfig:
    return AgentConfig(name=name, key_hash="0" * 64, **kw)


def set_state(conn, name="nas1", status="down", since=NOW - 7200, heartbeat=NOW - 10800, count=0, last_alert=None):
    conn.execute(
        "INSERT OR REPLACE INTO agent_state (name, updated_at, status, status_since, last_heartbeat, alert_count, "
        "last_alert_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, NOW, status, since, heartbeat, count, last_alert))


def log_status(conn, name, ts, status):
    conn.execute("INSERT INTO agent_status_log (agent, ts, status, reason) VALUES (?, ?, ?, '')", (name, ts, status))


def add_run(conn, agent_name="nas1", job="job", status="success", started=NOW - 600, completed=NOW - 500,
            error=None, target=""):
    cursor = conn.execute(
        "INSERT INTO runs (agent, job_name, target_label, status, started_at, completed_at, error_message, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (agent_name, job, target, status, started, completed, error, NOW))
    return cursor.lastrowid


def state_of(conn, name="nas1"):
    return conn.execute("SELECT alert_count, last_alert_at FROM agent_state WHERE name = ?", (name,)).fetchone()


class Outbox:
    def __init__(self):
        self.sent = []
        self.ok = True

    def __call__(self, email, subject, text, html):
        self.sent.append({"subject": subject, "text": text, "html": html})
        return self.ok

    @property
    def subjects(self):
        return [m["subject"] for m in self.sent]


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(mailer, "send", box)
    return box


@pytest.fixture
def registry(monkeypatch):
    agents = [agent()]
    monkeypatch.setattr(notify, "load_agents", lambda: agents)
    return agents


class TestEmailConfig:
    def test_valid_settings(self):
        cfg = make_config({"from_addr": "jabs@example.com", "digest": {"schedule": "30 6 * * 1"}})
        assert cfg.email.to_addrs == ["admin@example.com"] and cfg.email.offline_alert.send_recovery is True

    @pytest.mark.parametrize("email", [
        {"to_addrs": ["not-an-address"]}, {"to_addrs": ["a@b.c\nBcc: x@y.z"]}, {"from_addr": "no at sign"},
        {"digest": {"schedule": "0 8 * *"}}, {"digest": {"schedule": "99 8 * * *"}}])
    def test_rejects_bad_values(self, email):
        with pytest.raises(ValidationError):
            make_config(email)

    def test_blank_sender_is_allowed(self):
        assert make_config({"from_addr": ""}).email.from_addr == ""


class FakeSMTP:
    def __init__(self, log, fail=None):
        self.log, self.fail = log, fail

    def __call__(self, host, port, timeout=None, context=None):
        if self.fail:
            raise self.fail
        self.log.append(("connect", type(self).__name__, host, port, timeout))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, message):
        self.log.append(("send", message))


@pytest.fixture
def smtp(monkeypatch):
    log: list = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP(log))
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP(log))
    for name in ("JABS_SMTP_USER", "JABS_SMTP_PASS", "ENV_MODE"):
        monkeypatch.delenv(name, raising=False)
    return log


class TestMailer:
    def send(self, email=None, subject="Hello"):
        return mailer.send(make_config(email).email, subject, "plain body", "<p>html body</p>")

    def test_sends_multipart_with_tls_and_login(self, smtp, monkeypatch):
        monkeypatch.setenv("JABS_SMTP_USER", "me@example.com")
        monkeypatch.setenv("JABS_SMTP_PASS", "secret")
        assert self.send({"smtp_port": 587}) is True
        kinds = [entry[0] for entry in smtp]
        assert kinds == ["connect", "starttls", "login", "send"]
        message = smtp[-1][1]
        assert (message["Subject"], message["From"], message["To"]) == ("Hello", "me@example.com", "admin@example.com")
        assert message.get_body(("plain",)).get_content().strip() == "plain body"
        assert "html body" in message.get_body(("html",)).get_content()

    def test_from_addr_wins_over_login_name(self, smtp, monkeypatch):
        monkeypatch.setenv("JABS_SMTP_USER", "login@example.com")
        monkeypatch.setenv("JABS_SMTP_PASS", "x")
        self.send({"from_addr": "jabs@example.com"})
        assert smtp[-1][1]["From"] == "jabs@example.com"

    def test_port_465_uses_implicit_tls(self, smtp):
        assert self.send({"smtp_port": 465, "from_addr": "j@example.com"}) is True
        assert smtp[0][:3] == ("connect", "FakeSMTP", "smtp.test") and "starttls" not in [e[0] for e in smtp]

    def test_no_credentials_means_no_login(self, smtp):
        assert self.send({"use_tls": False, "from_addr": "j@example.com"}) is True
        assert [e[0] for e in smtp] == ["connect", "send"]

    def test_not_configured_does_not_connect(self, smtp):
        assert self.send({"smtp_server": ""}) is False
        assert self.send({"to_addrs": []}) is False
        assert smtp == []

    def test_needs_a_sender(self, smtp):
        assert self.send() is False and smtp == []

    @pytest.mark.parametrize("error", [smtplib.SMTPAuthenticationError(535, b"bad"), OSError("unreachable"),
                                       TimeoutError("slow")])
    def test_failures_are_reported_not_raised(self, monkeypatch, error):
        monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP([], fail=error))
        monkeypatch.delenv("ENV_MODE", raising=False)
        assert self.send({"from_addr": "j@example.com"}) is False

    def test_development_mode_marks_the_subject(self, smtp, monkeypatch):
        monkeypatch.setenv("ENV_MODE", "development")
        self.send({"from_addr": "j@example.com"})
        assert smtp[-1][1]["Subject"] == "[DEV] Hello"

    def test_html_templates_escape_agent_text(self):
        text, html = mailer.render("offline", agent="<script>x</script>", notes="<b>n</b>", grace_minutes=60,
                                   last_contact="-", silent_for="1 h", down_since="-", alert_number=1,
                                   max_alerts=1, recovery=True, generated="-")
        assert "<script>" not in html and "&lt;script&gt;" in html and "<b>n</b>" not in html
        assert "<script>x</script>" in text  # the plain-text part is not HTML

    def test_login_hint(self, monkeypatch):
        monkeypatch.delenv("JABS_SMTP_USER", raising=False)
        assert "not set" in mailer.smtp_login_hint(make_config().email)
        monkeypatch.setenv("JABS_SMTP_USER", "u")
        monkeypatch.setenv("JABS_SMTP_PASS", "p")
        assert mailer.smtp_login_hint(make_config().email) is None


class TestOfflineAlerts:
    def run(self, conn, cfg=None, now=NOW):
        notify.offline_alerts(conn, cfg or make_config(), now)

    def test_down_agent_triggers_one_alert(self, conn, outbox, registry):
        set_state(conn)
        self.run(conn)
        assert outbox.subjects == ["JABS alert: agent nas1 is offline"]
        assert "Alert 1 of 1" in outbox.sent[0]["text"] and "Offline since" in outbox.sent[0]["text"]
        assert tuple(state_of(conn)) == (1, NOW)

    def test_max_alerts_stops_further_mail(self, conn, outbox, registry):
        set_state(conn, count=1, last_alert=NOW - 5 * DAY)
        self.run(conn)
        assert outbox.sent == []

    def test_repeats_only_after_the_interval(self, conn, outbox, registry):
        cfg = make_config({"offline_alert": {"max_alerts": 0, "repeat_interval_minutes": 60}})
        set_state(conn, count=1, last_alert=NOW - 30 * 60)
        self.run(conn, cfg)
        assert outbox.sent == []
        self.run(conn, cfg, now=NOW + 31 * 60)
        assert len(outbox.sent) == 1 and "Alert 2 for this outage" in outbox.sent[0]["text"]
        assert state_of(conn)["alert_count"] == 2

    def test_failed_send_is_retried_next_time(self, conn, outbox, registry):
        set_state(conn)
        outbox.ok = False
        self.run(conn)
        assert state_of(conn)["alert_count"] == 0
        outbox.ok = True
        self.run(conn)
        assert state_of(conn)["alert_count"] == 1 and len(outbox.sent) == 2

    @pytest.mark.parametrize("status", ["up", "unknown", "disabled", None])
    def test_only_down_agents_alert(self, conn, outbox, registry, status):
        set_state(conn, status=status)
        self.run(conn)
        assert outbox.sent == []

    def test_per_agent_opt_out(self, conn, outbox, monkeypatch):
        monkeypatch.setattr(notify, "load_agents", lambda: [agent(offline_alerts=False)])
        set_state(conn)
        self.run(conn)
        assert outbox.sent == []

    def test_unregistered_agent_is_ignored(self, conn, outbox, registry):
        set_state(conn, name="ghost")
        self.run(conn)
        assert outbox.sent == []

    @pytest.mark.parametrize("email", [{"offline_alert": {"enabled": False}}, {"smtp_server": ""}, {"to_addrs": []}])
    def test_disabled_or_unconfigured(self, conn, outbox, registry, email):
        set_state(conn)
        self.run(conn, make_config(email))
        assert outbox.sent == []

    def test_recovery_mail_and_reset(self, conn, outbox, registry):
        log_status(conn, "nas1", NOW - 7200, "down")
        set_state(conn, status="up", since=NOW - 600, count=1, last_alert=NOW - 3600)
        self.run(conn)
        assert outbox.subjects == ["JABS: agent nas1 is back"]
        assert "Offline for:    1 h 50 min" in outbox.sent[0]["text"]
        assert tuple(state_of(conn)) == (0, None)
        self.run(conn)
        assert len(outbox.sent) == 1  # once per outage

    def test_no_recovery_mail_without_a_prior_alert(self, conn, outbox, registry):
        set_state(conn, status="up", count=0)
        self.run(conn)
        assert outbox.sent == []

    def test_recovery_can_be_turned_off(self, conn, outbox, registry):
        set_state(conn, status="up", count=1)
        self.run(conn, make_config({"offline_alert": {"send_recovery": False}}))
        assert outbox.sent == [] and state_of(conn)["alert_count"] == 0

    def test_disabling_an_agent_resets_the_streak_silently(self, conn, outbox, registry):
        set_state(conn, status="disabled", count=1)
        self.run(conn)
        assert outbox.sent == [] and state_of(conn)["alert_count"] == 0

    def test_dashboard_outage_keeps_the_streak(self, conn, outbox, registry):
        set_state(conn, status="unknown", count=1)
        self.run(conn)
        assert outbox.sent == [] and state_of(conn)["alert_count"] == 1

    def test_broken_agents_file_is_skipped(self, conn, outbox, monkeypatch):
        def broken():
            raise config.ConfigError("bad")
        monkeypatch.setattr(notify, "load_agents", broken)
        set_state(conn)
        self.run(conn)
        assert outbox.sent == []


class TestDigestData:
    def test_counts_problems_and_outages(self, conn):
        add_run(conn, status="success")
        add_run(conn, status="completed", agent_name="nas2")
        add_run(conn, status="failed", job="etl", target="vol1", error="disk\nfull  badly", completed=NOW - 100)
        add_run(conn, status="stopped", completed=NOW - 300)
        add_run(conn, status="running", completed=None)
        add_run(conn, status="success", completed=NOW - 3 * DAY)  # before the period
        log_status(conn, "nas1", NOW - 4000, "up")
        log_status(conn, "nas1", NOW - 3000, "down")
        log_status(conn, "nas1", NOW - 1000, "up")
        data = notify.digest_data(conn, make_config(), ["nas1"], NOW - DAY, NOW)
        assert data["totals"] == {"success": 2, "failed": 1, "stopped": 1, "other": 0}
        assert data["per_agent"] == [
            {"agent": "nas1", "success": 1, "failed": 1, "stopped": 1, "other": 0},
            {"agent": "nas2", "success": 1, "failed": 0, "stopped": 0, "other": 0}]
        assert [(p["status"], p["job"], p["target"], p["error"]) for p in data["problems"]] == [
            ("failed", "etl", "vol1", "disk full badly"), ("stopped", "job", "", "")]
        (outage,) = data["outages"]
        assert (outage["agent"], outage["ongoing"], outage["began_before"], outage["duration"]) == (
            "nas1", False, False, "33 min")

    def test_ongoing_and_carried_over_outage(self, conn):
        log_status(conn, "nas1", NOW - 2 * DAY, "down")
        set_state(conn, status="down")
        (outage,) = notify.digest_data(conn, make_config(), ["nas1"], NOW - DAY, NOW)["outages"]
        assert outage["ongoing"] is True and outage["began_before"] is True

    def test_problem_list_is_capped(self, conn):
        for i in range(notify.MAX_DIGEST_PROBLEMS + 5):
            add_run(conn, status="failed", completed=NOW - 100 - i)
        data = notify.digest_data(conn, make_config(), [], NOW - DAY, NOW)
        assert len(data["problems"]) == notify.MAX_DIGEST_PROBLEMS and data["problems_more"] == 5
        assert data["totals"]["failed"] == notify.MAX_DIGEST_PROBLEMS + 5

    def test_long_errors_are_truncated(self, conn):
        add_run(conn, status="failed", error="x" * 1000)
        assert len(notify.digest_data(conn, make_config(), [], NOW - DAY, NOW)["problems"][0]["error"]) == 200


class TestDigestTask:
    def run(self, conn, cfg=None, now=NOW):
        notify.digest_task(conn, cfg or make_config(), now)

    def last_sent(self, conn):
        value = db.get_meta(conn, notify.DIGEST_LAST_SENT)
        return None if value is None else float(value)

    def test_first_run_only_starts_the_period(self, conn, outbox, registry):
        add_run(conn)
        self.run(conn)
        assert outbox.sent == [] and self.last_sent(conn) == NOW

    def test_not_due_before_the_schedule_fires(self, conn, outbox, registry):
        add_run(conn)
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - 2 * 3600))  # 06:00, next fire 08:00
        self.run(conn, now=NOW - 1800)
        assert outbox.sent == [] and self.last_sent(conn) == NOW - 2 * 3600

    def test_sends_when_due_and_advances(self, conn, outbox, registry):
        add_run(conn)
        add_run(conn, status="failed")
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - DAY))
        self.run(conn)
        assert outbox.subjects == ["JABS digest 2027-01-15: 1 ok, 1 failed, 0 stopped"]
        assert "Runs finished: 1 succeeded, 1 failed" in outbox.sent[0]["text"]
        assert self.last_sent(conn) == NOW
        self.run(conn, now=NOW + 60)
        assert len(outbox.sent) == 1

    def test_subject_mentions_offline_agents(self, conn, outbox, registry):
        set_state(conn, status="down")
        log_status(conn, "nas1", NOW - 3600, "down")
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - DAY))
        self.run(conn)
        assert outbox.subjects[0].endswith("1 agent offline")

    def test_nothing_to_report_skips_the_mail_but_advances(self, conn, outbox, registry):
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - DAY))
        self.run(conn)
        assert outbox.sent == [] and self.last_sent(conn) == NOW

    def test_failed_send_is_retried(self, conn, outbox, registry):
        add_run(conn)
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - DAY))
        outbox.ok = False
        self.run(conn)
        assert self.last_sent(conn) == NOW - DAY
        outbox.ok = True
        self.run(conn, now=NOW + 300)
        assert len(outbox.sent) == 2 and self.last_sent(conn) == NOW + 300

    def test_schedule_is_evaluated_in_the_configured_timezone(self, conn, outbox, registry):
        add_run(conn)
        cfg = Config(timezone="America/New_York", email={
            "smtp_server": "s", "to_addrs": ["a@b.c"], "digest": {"schedule": "0 8 * * *"}})
        # 08:00 UTC is 03:00 in New York, so the 08:00 local fire is five hours away.
        db.set_meta(conn, notify.DIGEST_LAST_SENT, repr(NOW - 3600))
        self.run(conn, cfg)
        assert outbox.sent == []
        self.run(conn, cfg, now=NOW + 5 * 3600)
        assert len(outbox.sent) == 1

    @pytest.mark.parametrize("email", [{"digest": {"enabled": False}}, {"smtp_server": ""}])
    def test_disabled_or_unconfigured(self, conn, outbox, registry, email):
        self.run(conn, make_config(email))
        assert outbox.sent == [] and self.last_sent(conn) is None


class TestRunsPurge:
    TZ = __import__("zoneinfo").ZoneInfo("UTC")

    def purge(self, conn, runs_days=30, rollup_days=365):
        return activity.purge_runs(conn, self.TZ, NOW, runs_days, rollup_days)

    def test_deletes_old_runs_and_their_events(self, conn):
        old = add_run(conn, started=NOW - 40 * DAY, completed=NOW - 40 * DAY + 60)
        new = add_run(conn, started=NOW - 5 * DAY)
        conn.execute("INSERT INTO events (run_pk, event_type, timestamp, created_at) VALUES (?, 'x', ?, ?)",
                     (old, NOW, NOW))
        conn.execute("INSERT INTO events (run_pk, event_type, timestamp, created_at) VALUES (?, 'x', ?, ?)",
                     (new, NOW, NOW))
        assert self.purge(conn) == 1
        assert [r["id"] for r in conn.execute("SELECT id FROM runs")] == [new]
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    def test_cutoff_is_a_day_boundary(self, conn):
        today = NOW - 8 * 3600  # 2027-01-15 00:00 UTC
        keep = add_run(conn, started=today - 29 * DAY)  # first retained day with runs_days=30
        drop = add_run(conn, started=today - 29 * DAY - 1)
        self.purge(conn)
        assert [r["id"] for r in conn.execute("SELECT id FROM runs")] == [keep] and drop

    def test_counts_survive_in_the_rollup(self, conn):
        add_run(conn, started=NOW - 31 * DAY, status="success")
        add_run(conn, started=NOW - 31 * DAY + 10, status="failed")
        add_run(conn, started=NOW - 31 * DAY + 20, status="failed")
        activity.rollup_daily(conn, self.TZ, NOW - 5 * DAY, 30)  # while the day was still inside the raw window
        self.purge(conn)
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        series = activity.activity(conn, self.TZ, NOW, 35, 30)["series"]
        assert series["failed"][-32] == 2 and series["success"][-32] == 1

    def test_the_leaving_day_is_rolled_up_before_it_is_deleted(self, conn):
        run = add_run(conn, started=NOW - 30 * DAY - 3600, status="running", completed=None)
        activity.rollup_daily(conn, self.TZ, NOW - DAY, 30)  # a stale earlier rollup
        conn.execute("UPDATE runs SET status = 'failed' WHERE id = ?", (run,))
        self.purge(conn)
        row = conn.execute("SELECT bucket, runs FROM daily_runs WHERE agent = 'nas1'").fetchall()
        assert [(r["bucket"], r["runs"]) for r in row] == [("failed", 1)]

    def test_old_rollups_are_dropped(self, conn):
        conn.execute("INSERT INTO daily_runs VALUES ('2025-01-01', 'nas1', 'success', 3)")
        conn.execute("INSERT INTO daily_runs VALUES ('2026-06-01', 'nas1', 'success', 4)")
        self.purge(conn, rollup_days=365)
        assert [r["day"] for r in conn.execute("SELECT day FROM daily_runs")] == ["2026-06-01"]

    def test_nothing_to_delete(self, conn):
        add_run(conn)
        assert self.purge(conn) == 0


class TestTickAndCli:
    def test_tick_runs_notifications_before_purging(self):
        names = [name for name, _ in tick.TASKS]
        assert names.index("daily rollup") < names.index("runs purge")
        assert names.index("agent status") < names.index("offline alerts") < names.index("digest")

    def test_tick_survives_a_failing_notification(self, conn, monkeypatch):
        def boom(*_args):
            raise RuntimeError("smtp exploded")
        monkeypatch.setattr(notify, "offline_alerts", boom)
        monkeypatch.setattr(tick, "TASKS", [("offline alerts", boom), ("digest", lambda c, cfg, now: ran.append(1))])
        ran: list = []
        tick.run_once(conn, make_config(), NOW)
        assert ran == [1]

    @pytest.fixture
    def installed(self, home):
        return home

    def write_email(self, home, body):
        (home / "config" / "config.yaml").write_text(f"timezone: UTC\nemail:\n{body}", encoding="utf-8")

    def test_testmail_sends_and_reports(self, installed, monkeypatch, capsys):
        self.write_email(installed, "  smtp_server: smtp.test\n  to_addrs: [a@b.c]\n")
        box = Outbox()
        monkeypatch.setattr(mailer, "send", box)
        assert cli.main(["testmail"]) == 0
        assert box.subjects == ["JABS test message"] and "smtp.test:587" in box.sent[0]["text"]
        assert "sent to a@b.c" in capsys.readouterr().out

    def test_testmail_failure_exit_code(self, installed, monkeypatch, capsys):
        self.write_email(installed, "  smtp_server: smtp.test\n  to_addrs: [a@b.c]\n")
        box = Outbox()
        box.ok = False
        monkeypatch.setattr(mailer, "send", box)
        assert cli.main(["testmail"]) == 1
        assert "sending failed" in capsys.readouterr().err

    def test_testmail_needs_configuration(self, installed, capsys):
        self.write_email(installed, "  smtp_server: ''\n")
        assert cli.main(["testmail"]) == 1
        assert "not configured" in capsys.readouterr().err

    def test_invalid_email_config_is_a_readable_error(self, installed, capsys):
        self.write_email(installed, "  to_addrs: [nope]\n")
        assert cli.main(["testmail"]) == 1
        assert "email.to_addrs" in capsys.readouterr().err
