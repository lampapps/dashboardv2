# JABS Dashboard v2

Read-only web dashboard and ingest API for the JABS agents on a home lab LAN. Agents post job events, heartbeats, storage samples; the dashboard shows them, emails a digest, and alerts when an agent goes quiet.

v2 replaces the legacy dashboard (`../dashboard`, "v1"). The v1 agent endpoints behave the same, so existing agents work unchanged.

- The UI has no login and cannot change anything. The only writes are agent posts, authenticated with an API key.
- All configuration is YAML plus `.env` for secrets. There are no settings pages.

## Pages

| Page | What it shows |
| --- | --- |
| Overview (`/`) | Metrics, Job Activity chart, jobs table (host, job, last, next and previous run; sort or group by host), storage over time (the drives and S3 buckets set in `config.yaml`), status badges |
| Agents (`/agents`) | One card per agent, grouped by host: up/down history, uptime, read-only configuration |
| Agent (`/agents/<name>`) | That agent's jobs, activity and history |
| Job (`/agents/<name>/job?name=<job>`) | Run history with filters and paging, duration and size trend, a run's events |

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
- `retention`: how long runs, rollups, status history, storage samples are kept.

`config/agents.yaml` is the agent registry. Keys are stored only as SHA-256 hashes. Edits take effect without a restart. An agent is identified by its key, so renaming an entry in the file keeps its key working, but its history (stored under the name) starts again under the new name. Names use letters, digits, `.`, `_` and `-`. If the file is invalid the dashboard logs why, keeps the last good copy while running, and rejects every key if it starts with none; `python -m app config` shows the error.

`.env` holds secrets: `JABS_SMTP_USER`, `JABS_SMTP_PASS`, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` for S3. `ENV_MODE=development` raises the log level to DEBUG and prefixes email subjects with `[DEV]`.

Check the files with `python -m app config`.

## Command line

Run as `.venv/bin/python -m app <command>`.

| Command | Purpose |
| --- | --- |
| `initdb` | Create or upgrade the database schema (safe to repeat) |
| `config` | Validate `config.yaml` and `agents.yaml` |
| `admin [--unit NAME]` | Open the interactive [administration console](#admin-console) |
| `web [--dev]` | Serve the UI and API (gunicorn, or Flask's server with `--dev`) |
| `tick [-v]` | Run the periodic tasks once (the timer does this); `-v` prints each task's time and result |
| `testmail` | Send a test message with the email settings |
| `agent add NAME [--host HOST] [--grace-minutes N] [--notes TEXT] [--allowed-ip IP]` | Register an agent and print its key once |
| `agent list`, `agent enable NAME`, `agent disable NAME` | Manage the registry; a disabled agent's key is rejected |
| `agent set NAME [--host H] [--grace-minutes N] [--notes T] [--allowed-ip IP] [--alerts/--no-alerts]` | Change an agent's settings (an empty `--allowed-ip ''` clears the restriction) |
| `agent rekey NAME` | Issue a new API key for an agent (printed once); the old key stops working at once, the name and history stay |
| `agent remove NAME --yes [--purge-history]` | Delete an agent from the registry; `--purge-history` also deletes its runs, status and storage samples |
| `backup [--dest FILE]` | Consistent copy of the database, safe while running |
| `restore FILE --yes` | Replace the database with a backup; the current one is first saved as `pre-restore-<time>.sqlite` |
| `vacuum` | Reclaim free space in the database file |
| `check-db` | Integrity check, size and row counts (exit status 1 when problems are found) |
| `meta` | Show internal state such as the last tick and digest times |
| `runs delete --agent NAME [--job JOB] --yes` | Delete an agent's stored runs and events, or one job's |
| `storage purge [--source S] [--name N] --yes` | Delete stored storage samples, all or for one source or name |
| `reset --yes` | Delete the database and all history |

## Admin console

`.venv/bin/python -m app admin` opens a full-screen console for the person administering the dashboard. It does everything the command line does, and nothing that the web UI shows (there is no job or run browsing). Switch tabs with `1` to `5` or the mouse; the footer lists the keys for the current tab. `r` refreshes, `q` quits.

| Tab | What you can do |
| --- | --- |
| 1 Agents | See each agent's settings and last contact. Add, edit, enable or disable, issue a new key and remove agents. A new key is shown once, with the lines to paste into the agent's `.env` |
| 2 Files | See whether `config.yaml`, `agents.yaml` and `.env` are valid and have safe permissions. **Edit** opens the file in your system editor (`$VISUAL`, `$EDITOR`, else the system default such as nano) and checks it when you leave; an invalid file can be edited again, reverted or kept. After `config.yaml` or `.env` changes it offers to restart the service |
| 3 Database | Version, size and row counts. Back up, restore (typed confirmation), check integrity, vacuum, migrate, delete runs for an agent or job, purge storage samples, reset |
| 4 Diagnostics | Run the tick now with per-task results, send a test mail, preview and optionally send the digest, preview and test-send an offline alert (marked `[TEST]`, no state is changed), find which agent a pasted API key belongs to, view the internal state, run the development server |
| 5 Service | State of the web service, the tick service and the tick timer; start, stop and restart (uses `sudo`); follow the journal live |

Run it as the user that owns the install (the user the service runs as). It refuses to start as root when the install belongs to someone else, because files created by root would stop the dashboard writing its own database. The units are found by their working directory; if that fails, give the name with `--unit jabs-dashboard-v2`. Administrative changes (agents, restores, resets, service actions) are recorded in the journal under the identifier `jabs-admin`; keys and passwords never are.

The console needs the `textual` package; after updating, run `.venv/bin/pip install -r requirements.txt`.

## Agents

An agent sends its key in the `X-API-Key` header.

v1-compatible endpoints, documented in [`../dashboard/AGENTS_API_GUIDE.md`](../dashboard/AGENTS_API_GUIDE.md):

- `POST /api/monitoring/events` (job events, and heartbeats when `target_id` is omitted)
- `POST /api/monitoring/target-purged`
- `GET /api/heartbeat` (health check for monitors such as Uptime Kuma; no key needed)

New endpoints under `/api/v2/`, which an agent opts into:

```text
POST /api/v2/storage   {"hostname"?: "...", "samples": [{"name", "kind": "local|cloud|repo",
                         "used_bytes", "total_bytes"?, "series"?, "timestamp"?}]}
```

`POST /api/monitoring/events` also accepts an optional `hostname`.

- Storage: any post counts as hearing from the agent. The samples are stored, but the Overview charts only the drives and S3 buckets listed under `storage:` in `config.yaml`.

Agent-supplied text is untrusted and is always rendered as text, never as HTML.

### Hosts

Several agents can run on one machine, and the same agent type can run on several machines, so a job name alone is not unique. An agent can report the name of its machine in a `hostname` field on `/api/monitoring/events` and `/api/v2/storage`; that name is always used when present. For agents that do not report one, set `host` in `agents.yaml` (`agent add NAME --host HOST`). An unusable reported name (not a plausible host name) is ignored rather than rejected. Every page that shows a job also shows its host (and the agent name when that differs), and the jobs table and the Agents page can be sorted or grouped by host. An agent with neither is shown under its agent name.

### How agents are judged up or down

Any authenticated post from an agent counts as contact, including a bare heartbeat event. An agent with no contact for its `grace_minutes` is logged as `down`; the next contact logs it `up`. If the dashboard itself was not running, the gap is shown as "no data" and the agent gets a fresh grace period when the dashboard comes back.

## Email

Set `email.smtp_server` and `email.to_addrs`, put the login in `.env`, then run `python -m app testmail`. Port 465 uses implicit TLS; otherwise `use_tls` selects STARTTLS. Without credentials the mail is sent without logging in (a LAN relay).

- Offline alert: sent when an agent is logged `down`, repeated per `max_alerts` and `repeat_interval_minutes`. A per-agent `offline_alerts: false` in `agents.yaml` opts out. A recovery mail follows the first contact after an alert unless `send_recovery` is false.
- Digest: sent on the cron `email.digest.schedule`, in the configured timezone. It lists finished runs, failed and stopped runs with their errors, and agent outages. It is skipped when nothing happened.

## What the periodic tick does

Every 5 minutes: daily rollup of run counts, agent status changes, offline alerts, digest, local drive sampling, deleting runs and events older than `retention.runs_days` (their per-day counts stay in the rollup for `rollup_days`), purging old status and storage data, and S3 sampling when due. A failing task is logged and does not stop the others.

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
