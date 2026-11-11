import pytest

from app import __main__ as cli
from app import config, db, notify
from app.agents import resolve_hosts
from app.config import AgentConfig, Config
from app.web import create_app


def event(job="Home-jim", status="backup_complete", run_id="r1"):
    return {"version": "1.0", "agent_type": "Snapshot", "event_type": status, "run_id": run_id, "job_name": job,
            "target_id": "restic-home", "duration_seconds": 5, "cron_schedule": "0 2 * * *"}


@pytest.fixture
def client(home):
    db.init_db()
    return create_app(config.load_config()).test_client()


@pytest.fixture
def fleet(home, client):
    """Two Snapshot agents with a job of the same name on different hosts, one agent without a host."""
    keys = {
        "p3-snap": config.add_agent("p3-snap", host="P3Tiny"),
        "nas-snap": config.add_agent("nas-snap", host="NAS1"),
        "p3-dns": config.add_agent("p3-dns", host="P3Tiny"),
        "loner": config.add_agent("loner"),
    }
    for name in ("p3-snap", "nas-snap"):
        assert client.post("/api/monitoring/events", json=event(), headers={"X-API-Key": keys[name]}).status_code == 201
    client.post("/api/monitoring/events", json=event("pihole", run_id="r2"), headers={"X-API-Key": keys["p3-dns"]})
    client.post("/api/monitoring/events", json=event("media", run_id="r3"), headers={"X-API-Key": keys["loner"]})
    return keys


class TestHostSetting:
    @pytest.mark.parametrize("host", ["P3Tiny", "nas-1.lan", "a", "x" * 64, ""])
    def test_valid(self, host):
        assert AgentConfig(name="a", host=host, key_hash="0" * 64).host == host

    @pytest.mark.parametrize("host", ["two words", "-leading", "x" * 65, "semi;colon", "new\nline", "<b>"])
    def test_invalid(self, host):
        with pytest.raises(ValueError):
            AgentConfig(name="a", host=host, key_hash="0" * 64)

    def test_stored_in_agents_yaml(self, home):
        config.add_agent("a1", host="P3Tiny")
        config.add_agent("a2")
        agents = {a.name: a for a in config.load_agents()}
        assert (agents["a1"].host, agents["a2"].host) == ("P3Tiny", "")
        assert "host: P3Tiny" in (home / "config" / "agents.yaml").read_text()

    def test_resolve_hosts_falls_back_to_the_agent_name(self, home):
        db.init_db()
        agents = [AgentConfig(name="a1", host="P3Tiny", key_hash="0" * 64), AgentConfig(name="a2", key_hash="1" * 64)]
        conn = db.connect()
        try:
            assert resolve_hosts(conn, agents) == {"a1": "P3Tiny", "a2": "a2"}
        finally:
            conn.close()

    def test_cli_add_and_list(self, home, capsys):
        assert cli.main(["agent", "add", "snap1", "--host", "P3Tiny"]) == 0
        assert cli.main(["agent", "add", "snap2"]) == 0
        capsys.readouterr()
        assert cli.main(["agent", "list"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].split("\t")[:2] == ["snap1", "P3Tiny"] and lines[1].split("\t")[:2] == ["snap2", "-"]

    def test_cli_rejects_a_bad_host(self, home, capsys):
        assert cli.main(["agent", "add", "snap1", "--host", "bad host"]) == 1
        assert "host" in capsys.readouterr().err

    def test_blank_yaml_values_use_defaults(self):
        cfg = Config.model_validate({"badges": None, "storage": {"s3_buckets": None}, "email": {"to_addrs": None}})
        assert (cfg.badges, cfg.storage.s3_buckets, cfg.email.to_addrs) == ([], [], [])


class TestPayloads:
    def test_same_job_name_on_two_hosts_is_two_rows(self, client, fleet):
        rows = client.get("/api/ui/overview").get_json()["jobs"]
        home = sorted((r["host"], r["agent"], r["job_name"]) for r in rows if r["job_name"] == "Home-jim")
        assert home == [("NAS1", "nas-snap", "Home-jim"), ("P3Tiny", "p3-snap", "Home-jim")]

    def test_host_defaults_to_the_agent_name(self, client, fleet):
        by_agent = {r["agent"]: r["host"] for r in client.get("/api/ui/overview").get_json()["jobs"]}
        assert by_agent == {"p3-snap": "P3Tiny", "nas-snap": "NAS1", "p3-dns": "P3Tiny", "loner": "loner"}

    def test_overview_agents_list(self, client, fleet):
        agents = {a["name"]: a["host"] for a in client.get("/api/ui/overview").get_json()["agents"]}
        assert agents == {"p3-snap": "P3Tiny", "nas-snap": "NAS1", "p3-dns": "P3Tiny", "loner": "loner"}

    def test_agents_page_cards(self, client, fleet):
        cards = {a["name"]: a for a in client.get("/api/ui/agents?range=7d").get_json()["agents"]}
        assert (cards["p3-snap"]["host"], cards["p3-snap"]["config"]["host"]) == ("P3Tiny", "P3Tiny")
        assert (cards["loner"]["host"], cards["loner"]["config"]["host"]) == ("loner", "")

    def test_agent_detail_rows(self, client, fleet):
        body = client.get("/api/ui/agents/nas-snap").get_json()
        assert body["host"] == "NAS1" and {r["host"] for r in body["job_rows"]} == {"NAS1"}

    def test_job_summary_and_run_detail(self, client, fleet):
        job = client.get("/api/ui/job?agent=nas-snap&name=Home-jim").get_json()
        assert (job["host"], job["rollup"]["host"]) == ("NAS1", "NAS1")
        runs = client.get("/api/ui/job/runs?agent=nas-snap&name=Home-jim").get_json()["runs"]
        run = client.get(f"/api/ui/runs/{runs[0]['id']}").get_json()["run"]
        assert (run["agent"], run["host"]) == ("nas-snap", "NAS1")

    def test_unregistered_agent_is_shown_under_its_own_name(self, client, fleet, home):
        (home / "config" / "agents.yaml").write_text("agents: []\n")
        rows = client.get("/api/ui/overview").get_json()["jobs"]
        assert {(r["agent"], r["host"]) for r in rows} == {(r["agent"], r["agent"]) for r in rows} and rows

    def test_logs_and_storage_carry_hosts(self, client, fleet):
        key = fleet["nas-snap"]
        assert client.post("/api/v2/logs", json={"log": "snap", "lines": ["x"]}, headers={"X-API-Key": key}).status_code == 201
        assert client.post("/api/v2/storage", json={"samples": [{"name": "repo", "kind": "repo", "used_bytes": 1}]},
                           headers={"X-API-Key": key}).status_code == 201
        logs = client.get("/api/ui/logs").get_json()["logs"]
        assert [(entry["agent"], entry["host"]) for entry in logs] == [("nas-snap", "NAS1")]
        assert client.get("/api/ui/storage?range=7d").get_json()["hosts"]["nas-snap"] == "NAS1"

    def test_host_changes_in_the_file_are_picked_up(self, client, fleet, home):
        path = home / "config" / "agents.yaml"
        path.write_text(path.read_text().replace("host: NAS1", "host: NAS2"))
        rows = client.get("/api/ui/overview").get_json()["jobs"]
        assert {r["host"] for r in rows if r["agent"] == "nas-snap"} == {"NAS2"}


class TestEmails:
    NOW = 1_800_000_000.0

    @pytest.fixture
    def conn(self, tmp_path):
        path = tmp_path / "t.sqlite"
        db.init_db(path)
        connection = db.connect(path)
        yield connection
        connection.close()

    def cfg(self):
        return Config(timezone="UTC", email={"smtp_server": "smtp.test", "to_addrs": ["a@example.com"]})

    def test_label(self):
        assert notify._label("P3Tiny", "snap") == "P3Tiny / snap"
        assert notify._label("snap", "snap") == "snap"

    def test_offline_alert_names_the_host(self, conn, monkeypatch):
        sent = []
        monkeypatch.setattr(notify.mailer, "send", lambda email, subject, text, html: sent.append((subject, text, html)) or True)
        agent = AgentConfig(name="snap", host="P3Tiny", key_hash="0" * 64)
        monkeypatch.setattr(notify, "load_agents", lambda: [agent])
        conn.execute("INSERT INTO agent_state (name, updated_at, status, status_since, last_heartbeat) "
                     "VALUES ('snap', ?, 'down', ?, ?)", (self.NOW, self.NOW - 7200, self.NOW - 10800))
        notify.offline_alerts(conn, self.cfg(), self.NOW)
        (subject, text, html), = sent
        assert subject == "JABS alert: agent P3Tiny / snap is offline"
        assert "Host:           P3Tiny" in text and "Agent:          snap" in text and ">P3Tiny<" in html

    def test_digest_lines_name_the_host(self, conn):
        agents = [AgentConfig(name="snap", host="P3Tiny", key_hash="0" * 64)]
        conn.execute("INSERT INTO runs (agent, job_name, status, started_at, completed_at, updated_at) "
                     "VALUES ('snap', 'Home-jim', 'failed', ?, ?, ?)", (self.NOW - 200, self.NOW - 100, self.NOW))
        data = notify.digest_data(conn, self.cfg(), agents, self.NOW - 86400, self.NOW)
        assert data["problems"][0]["label"] == "P3Tiny / snap" and data["per_agent"][0]["host"] == "P3Tiny"


class TestReportedHostname:
    @pytest.fixture
    def key(self, home, client):
        return config.add_agent("snap", host="configured-host")

    def post_event(self, client, key, **extra):
        return client.post("/api/monitoring/events", json={**event(), **extra}, headers={"X-API-Key": key})

    def hosts(self, client):
        return {r["agent"]: r["host"] for r in client.get("/api/ui/overview").get_json()["jobs"]}

    def stored(self):
        conn = db.connect()
        try:
            return conn.execute("SELECT hostname FROM agent_state WHERE name = 'snap'").fetchone()[0]
        finally:
            conn.close()

    def test_reported_hostname_wins_over_agents_yaml(self, client, key):
        assert self.post_event(client, key, hostname="P3Tiny").status_code == 201
        assert self.hosts(client) == {"snap": "P3Tiny"}
        assert self.stored() == "P3Tiny"

    def test_configured_host_is_the_fallback(self, client, key):
        self.post_event(client, key)
        assert self.hosts(client) == {"snap": "configured-host"}

    def test_a_later_event_without_hostname_keeps_the_reported_one(self, client, key):
        self.post_event(client, key, hostname="P3Tiny")
        self.post_event(client, key, run_id="r2")
        assert self.stored() == "P3Tiny"

    @pytest.mark.parametrize("value", ["two words", "<b>x</b>", "x" * 80, "", 123, ["a"], "new\nline", "-lead"])
    def test_bad_hostnames_are_ignored_not_rejected(self, client, key, value):
        assert self.post_event(client, key, hostname=value).status_code == 201
        assert self.stored() is None

    def test_hostname_is_trimmed(self, client, key):
        self.post_event(client, key, hostname="  P3Tiny  ")
        assert self.stored() == "P3Tiny"

    def test_logs_and_storage_report_the_hostname_too(self, client, key):
        headers = {"X-API-Key": key}
        assert client.post("/api/v2/logs", json={"log": "tick", "hostname": "NAS1", "lines": ["x"]},
                           headers=headers).status_code == 201
        assert self.stored() == "NAS1"
        assert client.post("/api/v2/storage", json={"hostname": "NAS2", "samples": [
            {"name": "repo", "kind": "repo", "used_bytes": 1}]}, headers=headers).status_code == 201
        assert self.stored() == "NAS2"
        assert client.get("/api/ui/storage?range=7d").get_json()["hosts"]["snap"] == "NAS2"

    def test_a_new_hostname_refreshes_the_ui_but_the_same_one_does_not(self, client, key):
        headers = {"X-API-Key": key}

        def rev():
            conn = db.connect()
            try:
                return db.get_meta(conn, "rev")
            finally:
                conn.close()

        def post_logs(host):
            assert client.post("/api/v2/logs", json={"log": "t", "hostname": host, "lines": ["x"]},
                               headers=headers).status_code == 201
        post_logs("A")
        before = rev()
        post_logs("A")
        assert rev() == before
        post_logs("B")
        assert rev() != before

    def test_agent_cards_say_where_the_host_comes_from(self, client, key):
        def card():
            return {a["name"]: a for a in client.get("/api/ui/agents?range=7d").get_json()["agents"]}["snap"]
        assert (card()["host"], card()["host_source"]) == ("configured-host", "configured")
        self.post_event(client, key, hostname="P3Tiny")
        assert (card()["host"], card()["host_source"]) == ("P3Tiny", "reported")
        assert card()["config"]["host"] == "configured-host"

    def test_agent_without_any_host_is_shown_under_its_name(self, client, home):
        key = config.add_agent("plain")
        client.post("/api/monitoring/events", json=event(), headers={"X-API-Key": key})
        cards = {a["name"]: a for a in client.get("/api/ui/agents?range=7d").get_json()["agents"]}
        assert (cards["plain"]["host"], cards["plain"]["host_source"]) == ("plain", "agent")
