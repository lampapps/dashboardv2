import json
import types

import pytest

from app import config, db, status
from app.config import AgentConfig
from app.web import api_ui, create_app

HOUR = 3600
GRACE = HOUR  # default grace_minutes=60
TICK = 300


def agent(name="a", **kwargs):
    return AgentConfig(name=name, key_hash=config.hash_key(name), **kwargs)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def contact(conn, name, at):
    """What ingest does for each authenticated request."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO agent_state (name, last_heartbeat, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_heartbeat = excluded.last_heartbeat",
            (name, at, at))
        status.record_contact(conn, name, at)


def log(conn, name="a"):
    return [(r["ts"], r["status"]) for r in conn.execute(
        "SELECT ts, status FROM agent_status_log WHERE agent = ? ORDER BY ts, id", (name,))]


def run_ticks(conn, agents, start, end):
    t = start
    while t <= end:
        status.update_statuses(conn, agents, t)
        t += TICK


class TestEvaluate:
    def test_disabled_agent(self):
        assert status.evaluate(agent(enabled=False), None, None, 100) == ("disabled", 100)
        assert status.evaluate(agent(enabled=False), "disabled", None, 100) is None

    def test_re_enabled_agent(self):
        a = agent()
        assert status.evaluate(a, "disabled", None, 100) == ("new", 100)
        assert status.evaluate(a, "disabled", 90, 100) == ("up", 100)
        assert status.evaluate(a, "disabled", 100, 100 + GRACE + 1) == ("down", 100 + GRACE + 1)

    def test_never_heard_from_is_not_judged(self):
        assert status.evaluate(agent(), None, None, 10_000) is None

    def test_fresh_contact(self):
        a = agent()
        assert status.evaluate(a, "up", 1000, 1000 + GRACE) is None
        assert status.evaluate(a, "down", 1000, 1000 + GRACE) == ("up", 1000)
        assert status.evaluate(a, None, 1000, 1000 + 5) == ("up", 1000)

    def test_down_is_dated_to_when_the_grace_period_ran_out(self):
        a = agent()
        assert status.evaluate(a, "up", 1000, 1000 + GRACE + 200) == ("down", 1000 + GRACE)
        assert status.evaluate(a, "down", 1000, 1000 + GRACE + 200) is None
        assert status.evaluate(a, None, 1000, 1000 + GRACE + 200) == ("down", 1000 + GRACE)

    def test_after_an_outage_the_agent_gets_a_fresh_grace_period(self):
        a = agent()
        gap_end = 10_000
        assert status.evaluate(a, "unknown", 100, gap_end + GRACE, gap_end) is None
        assert status.evaluate(a, "unknown", 100, gap_end + GRACE + 1, gap_end) == ("down", gap_end + GRACE)
        assert status.evaluate(a, "unknown", gap_end + 5, gap_end + 10, gap_end) == ("up", gap_end + 5)

    @pytest.mark.parametrize("enabled,stored,hb,now,expected", [
        (False, "up", 100, 200, "disabled"), (True, None, None, 200, "new"), (True, "up", 100, 200, "up"),
        (True, "up", 100, 100 + GRACE + 1, "down"), (True, "unknown", 100, 200, "unknown"),
        (True, "disabled", None, 200, "new"),
    ])
    def test_effective_status(self, enabled, stored, hb, now, expected):
        assert status.effective_status(agent(enabled=enabled), stored, hb, now, gap_end=150) == expected


class TestUpdates:
    def test_contact_then_silence_goes_down_at_the_end_of_the_grace_period(self, conn):
        a = agent()
        contact(conn, "a", 1000)
        run_ticks(conn, [a], 1000 + TICK, 1000 + GRACE + 3 * TICK)
        assert log(conn) == [(1000, "up"), (1000 + GRACE, "down")]

    def test_ticks_are_idempotent_and_only_bump_the_revision_on_change(self, conn):
        a = agent()
        contact(conn, "a", 1000)
        run_ticks(conn, [a], 1000 + TICK, 1000 + GRACE + 2 * TICK)
        rev = db.get_meta(conn, "rev")
        run_ticks(conn, [a], 1000 + GRACE + 3 * TICK, 1000 + GRACE + 6 * TICK)
        assert len(log(conn)) == 2
        assert db.get_meta(conn, "rev") == rev

    def test_coming_back_is_logged_up_at_contact_time(self, conn):
        a = agent()
        contact(conn, "a", 1000)
        run_ticks(conn, [a], 1000 + TICK, 1000 + GRACE + 2 * TICK)
        contact(conn, "a", 9000)
        contact(conn, "a", 9100)
        assert log(conn) == [(1000, "up"), (1000 + GRACE, "down"), (9000, "up")]

    def test_a_dashboard_outage_is_unknown_not_down(self, conn):
        a = agent()
        contact(conn, "a", 100)
        run_ticks(conn, [a], 300, 900)             # ticks at 300, 600, 900
        status.update_statuses(conn, [a], 20_000)  # nothing ran in between
        assert log(conn) == [(100, "up"), (900, "unknown")]

        run_ticks(conn, [a], 20_300, 20_000 + GRACE)
        assert log(conn) == [(100, "up"), (900, "unknown")]
        run_ticks(conn, [a], 20_000 + GRACE + TICK, 20_000 + GRACE + 3 * TICK)
        assert log(conn)[-1] == (20_000 + GRACE, "down")

    def test_an_agent_reporting_after_the_outage_is_up_again(self, conn):
        a = agent()
        contact(conn, "a", 100)
        run_ticks(conn, [a], 300, 900)
        status.update_statuses(conn, [a], 20_000)
        contact(conn, "a", 20_100)
        status.update_statuses(conn, [a], 20_300)
        assert [s for _, s in log(conn)] == ["up", "unknown", "up"]

    def test_disabling_and_re_enabling(self, conn):
        contact(conn, "a", 1000)
        status.update_statuses(conn, [agent(enabled=False)], 1100)
        status.update_statuses(conn, [agent()], 1400)
        assert log(conn) == [(1000, "up"), (1100, "disabled"), (1400, "up")]

    def test_never_seen_agents_leave_no_history_but_a_disabled_one_is_noted(self, conn):
        status.update_statuses(conn, [agent("x"), agent("y", enabled=False)], 1000)
        assert log(conn, "x") == []
        assert log(conn, "y") == [(1000, "disabled")]

    def test_status_history_is_purged_by_retention(self, conn):
        contact(conn, "a", 1000)
        status.purge_old(conn, config.Config(), 1000 + 400 * 86400)
        assert log(conn) == []


class TestTimeline:
    def add(self, conn, rows):
        for ts, st in rows:
            conn.execute("INSERT INTO agent_status_log (agent, ts, status) VALUES ('a', ?, ?)", (ts, st))

    def test_spans_are_clipped_to_the_window_and_extend_to_now(self, conn):
        self.add(conn, [(100, "up"), (200, "down"), (300, "up")])
        spans = status.timeline(conn, "a", 150, 400)
        assert spans == [[150, 200, "up"], [200, 300, "down"], [300, 400, "up"]]
        assert status.uptime(spans) == pytest.approx(150 / 250)

    def test_time_before_the_first_record_is_left_out(self, conn):
        self.add(conn, [(300, "up")])
        assert status.timeline(conn, "a", 100, 400) == [[300, 400, "up"]]

    def test_unknown_and_disabled_time_does_not_count_toward_uptime(self, conn):
        self.add(conn, [(0, "up"), (100, "unknown"), (200, "disabled"), (300, "up")])
        spans = status.timeline(conn, "a", 0, 400)
        assert status.uptime(spans) == 1.0
        assert status.uptime([[0, 10, "unknown"]]) is None

    def test_a_pending_change_is_shown_before_the_tick_logs_it(self, conn):
        self.add(conn, [(100, "up")])
        spans = status.timeline(conn, "a", 0, 300, pending=("down", 250))
        assert spans == [[100, 250, "up"], [250, 300, "down"]]

    def test_pending_new_is_not_drawn(self, conn):
        assert status.timeline(conn, "a", 0, 300, pending=("new", 250)) == []


class TestHttp:
    NOW = 1_800_000_000.0

    @pytest.fixture
    def client(self, home, monkeypatch):
        db.init_db()
        fake = types.SimpleNamespace(time=lambda: self.NOW)
        monkeypatch.setattr(api_ui, "time", fake)
        monkeypatch.setattr("app.web.api_ingest.time", fake)
        return create_app(config.load_config()).test_client()

    def test_ingest_logs_up_once_for_repeated_contact(self, client, home):
        key = config.add_agent("nas1")
        for _ in range(3):
            client.post("/api/monitoring/events", json={}, headers={"X-API-Key": key})
        conn = db.connect()
        assert log(conn, "nas1") == [(self.NOW, "up")]
        conn.close()

    def test_agents_payload(self, client, home):
        key = config.add_agent("nas1", grace_minutes=30, notes="the NAS", allowed_ip=None)
        config.add_agent("idle")
        config.set_agent_enabled("idle", False)
        client.post("/api/monitoring/events", headers={"X-API-Key": key}, json={
            "version": "2.0", "agent_type": "NAS Sync", "event_type": "backup_complete", "run_id": "r1",
            "job_name": "nightly", "target_id": "T", "duration_seconds": 5, "cron_schedule": "30 2 * * *"})

        body = client.get("/api/ui/agents?range=7d").get_json()
        by_name = {a["name"]: a for a in body["agents"]}

        nas = by_name["nas1"]
        assert (nas["status"], nas["version"], nas["agent_type"]) == ("up", "2.0", "NAS Sync")
        assert nas["config"] == {"enabled": True, "grace_minutes": 30, "notes": "the NAS", "allowed_ip": None,
                                 "offline_alerts": True}
        assert nas["jobs"] == {"total": 1, "failing": 0}
        assert nas["last_run"]["job_name"] == "nightly"
        assert nas["schedules"] == [{"job_name": "nightly", "cron_schedule": "30 2 * * *"}]
        assert nas["spans"] == []  # contact at "now" has no duration yet
        assert by_name["idle"]["status"] == "disabled"
        assert body["range"] == "7d" and body["end"] - body["start"] == 7 * 86400

    def test_problem_agents_sort_first(self, client, home):
        config.add_agent("a-up")
        config.add_agent("z-down")
        conn = db.connect()
        conn.execute("INSERT INTO agent_state (name, last_heartbeat, updated_at) VALUES ('z-down', ?, ?)",
                     (self.NOW - 2 * HOUR, self.NOW))
        conn.close()
        names = [a["name"] for a in client.get("/api/ui/agents").get_json()["agents"]]
        assert names == ["z-down", "a-up"]

    def test_no_keys_or_hashes_are_ever_returned(self, client, home):
        key = config.add_agent("nas1")
        client.post("/api/monitoring/events", json={}, headers={"X-API-Key": key})
        text = client.get("/api/ui/agents").get_data(as_text=True)
        assert key not in text and config.hash_key(key) not in text and "key_hash" not in text
        assert "key" not in json.loads(text)["agents"][0]["config"]

    def test_etag_and_range_handling(self, client, home):
        config.add_agent("nas1")
        first = client.get("/api/ui/agents?range=30d")
        assert client.get("/api/ui/agents?range=30d", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
        assert client.get("/api/ui/agents?range=nonsense").get_json()["range"] == "30d"

    def test_page_renders_with_navigation(self, client):
        html = client.get("/agents").get_data(as_text=True)
        assert 'aria-current="page">Agents' in html
        assert "js/agents.js" in html
