# JABS Dashboard v2

Read-only web dashboard and ingest API for the JABS agents on a home lab LAN. Agents post job events, heartbeats, storage samples and log tails; the dashboard shows them, emails a digest, and alerts when an agent goes quiet.

v2 replaces the legacy dashboard (`../dashboard`, "v1"). The v1 agent endpoints behave the same, so existing agents work unchanged.

- The UI has no login and cannot change anything. The only writes are agent posts, authenticated with an API key.
- All configuration is YAML plus `.env` for secrets. There are no settings pages.

## Pages

| Page | What it shows |
| --- | --- |
| Overview (`/`) | Metrics, Job Activity chart, jobs table (host, job, last, next and previous run; sort or group by host), storage over time (local and cloud), status badges |
| Agents (`/agents`) | One card per agent, grouped by host: up/down history, uptime, read-only configuration |
| Agent (`/agents/<name>`) | That agent's jobs, activity and history |
| Job (`/agents/<name>/job?name=<job>`) | Run history with filters and paging, duration and size trend, a run's events |
| Logs (`/logs`) | Log tails shipped by agents: follow, level filters, search, download |

Light and dark themes follow the toggle in the top bar. `/healthz` returns `{"status": "ok", ...}` for monitors.

## Install

Needs Python 3.11 or newer and `python3-venv`.

```bash
./setup.sh            # venv, dependencies, config files from the examples, empty database
$EDITOR config/config.yaml .env
.venv/bin/python -m app agent add NAME      # prints the agent's API key once
.venv/bin/python -m app web --dev           # try it at http://localhost:5002/
```

`./setup.sh --dev` also installs the test dependencies. It never overwrites existing files.

### Run as a service

```bash
sudo deploy/install.sh                      # units named jabs-dashboard-v2
sudo deploy/install.sh --name jabs-dashboard --user NAME   # at cutover from v1
```

This installs a web service (gunicorn) and a timer that runs `python -m app tick` every 5 minutes, runs the config check as the service user first, and starts both. The service runs as the invoking user, never root. After editing `config.yaml` or `.env`, run `sudo systemctl restart <unit>`.

```bash
systemctl status jabs-dashboard-v2 jabs-dashboard-v2-tick.timer
journalctl -u jabs-dashboard-v2 -f            # web
journalctl -u jabs-dashboard-v2-tick          # periodic tasks
deploy/install.sh --render-only DIR           # preview the units without touching the system
```

Logs go to the journal; there are no log files.

## Configuration

`config/config.yaml` (copy of `config.example.yaml`; every key is documented there):

- `timezone`: used to group days and show times.
- `server`: host, port, gunicorn workers and threads.
- `badges`: status images across the top, for example Uptime Kuma badge URLs.
- `storage`: local drives to sample and S3 buckets (CloudWatch daily sizes, or a bucket listing).
- `email`: SMTP server, recipients, digest schedule, offline alerts.
- `retention`: how long runs, rollups, status history, storage samples and log lines are kept.

`config/agents.yaml` is the agent registry. Keys are stored only as SHA-256 hashes. Edits take effect without a restart.

`.env` holds secrets: `JABS_SMTP_USER`, `JABS_SMTP_PASS`, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for S3. `ENV_MODE=development` raises the log level to DEBUG and prefixes email subjects with `[DEV]`.

Check the files with `python -m app config`.

## Command line

Run as `.venv/bin/python -m app <command>`.

| Command | Purpose |
| --- | --- |
| `initdb` | Create or upgrade the database schema (safe to repeat) |
| `config` | Validate `config.yaml` and `agents.yaml` |
| `web [--dev]` | Serve the UI and API (gunicorn, or Flask's server with `--dev`) |
| `tick` | Run the periodic tasks once (the timer does this) |
| `testmail` | Send a test message with the email settings |
| `agent add NAME [--host HOST] [--grace-minutes N] [--notes TEXT] [--allowed-ip IP]` | Register an agent and print its key once |
| `agent list`, `agent enable NAME`, `agent disable NAME` | Manage the registry; a disabled agent's key is rejected |
| `backup [--dest FILE]` | Consistent copy of the database, safe while running |
| `reset --yes` | Delete the database and all history |

## Agents

An agent sends its key in the `X-API-Key` header.

v1-compatible endpoints, documented in [`../dashboard/AGENTS_API_GUIDE.md`](../dashboard/AGENTS_API_GUIDE.md):

- `POST /api/monitoring/events` (job events, and heartbeats when `target_id` is omitted)
- `POST /api/monitoring/target-purged`
- `GET /api/heartbeat` (health check for monitors such as Uptime Kuma; no key needed)

New endpoints under `/api/v2/`, which an agent opts into:

```text
POST /api/v2/storage   {"samples": [{"name", "kind": "local|cloud|repo", "used_bytes",
                                      "total_bytes"?, "series"?, "timestamp"?}]}
POST /api/v2/logs      {"log": "name", "batch_id"?: "...", "lines": ["text" | {"message", "level"?, "ts"?}]}
```

- Logs: up to 1000 lines per post, 2000 characters per line, 25 logs per agent. Each log keeps its newest 5000 lines. A repeated `batch_id` is acknowledged without storing the lines again, so retries are safe. The level is taken from the entry or detected in the text.
- Storage: any post counts as hearing from the agent.

Agent-supplied text is untrusted and is always rendered as text, never as HTML.

### Hosts

Several agents can run on one machine, and the same agent type can run on several machines, so a job name alone is not unique. Each registered agent has a `host`, set with `agent add NAME --host HOST` or by editing `agents.yaml`. Every page that shows a job also shows its host (and the agent name when that differs), and the jobs table and the Agents page can be sorted or grouped by host. Agents do not report their own host. An agent registered without one is shown under its agent name.

### How agents are judged up or down

Any authenticated post from an agent counts as contact, including a bare heartbeat event. An agent with no contact for its `grace_minutes` is logged as `down`; the next contact logs it `up`. If the dashboard itself was not running, the gap is shown as "no data" and the agent gets a fresh grace period when the dashboard comes back.

## Email

Set `email.smtp_server` and `email.to_addrs`, put the login in `.env`, then run `python -m app testmail`. Port 465 uses implicit TLS; otherwise `use_tls` selects STARTTLS. Without credentials the mail is sent without logging in (a LAN relay).

- Offline alert: sent when an agent is logged `down`, repeated per `max_alerts` and `repeat_interval_minutes`. A per-agent `offline_alerts: false` in `agents.yaml` opts out. A recovery mail follows the first contact after an alert unless `send_recovery` is false.
- Digest: sent on the cron `email.digest.schedule`, in the configured timezone. It lists finished runs, failed and stopped runs with their errors, and agent outages. It is skipped when nothing happened.

## What the periodic tick does

Every 5 minutes: daily rollup of run counts, agent status changes, offline alerts, digest, local drive sampling, deleting runs and events older than `retention.runs_days` (their per-day counts stay in the rollup for `rollup_days`), purging old status, storage and log data, and S3 sampling when due. A failing task is logged and does not stop the others.

## Development

```bash
./setup.sh --dev
.venv/bin/python -m pytest -q
```

- `tests/unit`: fast, isolated tests.
- `tests/contract`: a black-box agent-API suite run against throwaway copies of v1 and v2 (`JABS_CONTRACT_BACKENDS=v1,v2`, default both). A test marked `v1_bug` is an expected failure on v1 and must pass on v2.
- `tests/js`: `node --test` for the pure helpers in `static/js/ui.js`; pytest runs them when `node` is installed.

See [AGENTS.md](AGENTS.md) for the layout and conventions.

## License

MIT, see [LICENSE.md](LICENSE.md).
