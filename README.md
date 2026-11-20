# JABS Dashboard v2

Read-only web dashboard and ingest API for the JABS agents on a home lab LAN. Agents post job events, heartbeats, storage samples; the dashboard shows them, emails a digest, and alerts when an agent goes quiet.

v2 replaces the legacy dashboard (`../dashboard`, "v1"). Version 0.5.0 also replaced the agent API: everything is under `/api/v2/` and every agent, job and target is identified by an id that never changes, so renaming one never splits its history.

- The UI has no login and cannot change anything. The only writes are agent posts, authenticated with an API key.
- All configuration is YAML plus `.env` for secrets. There are no settings pages.

## Requirements

- Linux with systemd
- Python 3.11 or newer with `python3-venv`

## Install

Do these steps as the user that will run the service, in the install directory.

0. **Clone**
   ```bash
   sudo mkdir /opt/jabs-dashboard
   sudo chown <user>:<user> /opt/jabs-dashboard
   git clone https://github.com/lampapps/dashboardv2.git /opt/jabs-dashboard

1. **Install.**

   ```bash
   ./setup.sh
   ```

   It creates the virtual environment, installs the dependencies, copies the example `config/config.yaml`, `config/agents.yaml` and `.env` (never overwriting existing files), and creates the empty database.

2. **Open the admin console.** The next steps use it; the [Admin console](#admin-console) section lists every tab.

   ```bash
   .venv/bin/python -m app admin
   ```

3. **Configure** (tab `2` Files). Select `config.yaml` and press `e` to edit it in your system editor: set the timezone, the drives you want charted and, if you want mail, the SMTP server and recipients. Do the same for `.env` (SMTP login, AWS keys). The files document themselves, so leave what you do not need. The console checks each file when you close the editor and offers to revert a mistake.

4. **Register your agents** (tab `1` Agents): press `a`, enter a name and the machine it runs on. The API key is shown once, with the two lines to put in that agent's `.env`:

   ```text
   JABS_DASHBOARD_URL=http://<this host>:5002
   JABS_AGENT_KEY=<the key>
   ```

5. **Install the services** (quit the console first, or use another terminal):

   ```bash
   sudo deploy/install.sh
   ```

   This runs a config check and then installs and starts a web service (gunicorn) and a timer that runs the periodic tasks every 5 minutes. The service runs as the invoking user, never root; use `--user NAME` if you are not running `sudo` from that user's session.

6. **Check it.** Open `http://<this host>:5002/` and `/healthz`. In the console, tab `5` Service shows the units and follows their journal, and tab `1` shows each agent's last contact once it reports.

To try the dashboard before installing services, run `.venv/bin/python -m app web --dev` (<http://localhost:5002/>).

### Services

`sudo deploy/install.sh` installs units named `jabs-dashboard-v2` (`--name` changes it, for example `jabs-dashboard` at cutover from v1) and `<name>-tick.service` / `.timer`. `deploy/install.sh --render-only DIR` previews the unit files without touching the system. Logs go to the journal; there are no log files.

```bash
systemctl status jabs-dashboard-v2 jabs-dashboard-v2-tick.timer
journalctl -u jabs-dashboard-v2 -f            # web
journalctl -u jabs-dashboard-v2-tick          # periodic tasks
sudo systemctl restart jabs-dashboard-v2      # after editing config.yaml or .env
```

After updating the code, run `./setup.sh` again (it only fills in what is missing) and restart the service.

## Configuration

| File | What it holds |
| --- | --- |
| `config/config.yaml` | Timezone, server, status badges, storage drives and S3 buckets, email, retention. Every key is documented in `config.example.yaml`. Read at start-up, so restart the service after editing |
| `config/agents.yaml` | The agent registry. Keys are stored only as SHA-256 hashes. Managed with the `agent` commands or the console; edits take effect without a restart |
| `.env` | Secrets: `JABS_SMTP_USERNAME`, `JABS_SMTP_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. `ENV_MODE=development` raises logging to DEBUG and prefixes email subjects with `[DEV]` |

Check the files with `.venv/bin/python -m app config` or the console's Files tab. Every agent in `agents.yaml` has an `id` (a UUID that `agent add` writes); the API key, the history and every link use it, so the name is only a label you can change by hand or with `agent rename`. Never edit or copy an `id`. After upgrading from a file without ids, run `agent adopt`. If `agents.yaml` becomes invalid the dashboard logs why and keeps the last good copy while running; with none it rejects every key.

## Usage

### Web pages

| Page | What it shows |
| --- | --- |
| Overview (`/`) | Metrics, Job Activity chart, jobs table (host, job, last, next and previous run; sort or group by host), storage over time (the drives and S3 buckets set in `config.yaml`), status badges |
| Agents (`/agents`) | One card per agent, grouped by host: up/down history, uptime, read-only configuration |
| Agent (`/agents/<agent id>`) | That agent's jobs, activity and history |
| Job (`/agents/<agent id>/jobs/<job id>`) | Run history with filters and paging, duration and size trend, a run's events |

Light and dark themes follow the toggle in the top bar. `/healthz` returns `{"status": "ok", ...}` for monitors.

### Command line

Run as `.venv/bin/python -m app <command>`. The [admin console](#admin-console) can do everything below; destructive commands here need `--yes`.

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
| `agent rename NAME NEW_NAME` | Change an agent's name; only the label changes, its id, key, settings and history stay and the agent needs no change. Refused when `NEW_NAME` is registered |
| `agent adopt` | Give an id to every agent in `agents.yaml` that lacks one (needed once after upgrading from 0.4) |
| `agent rekey NAME` | Issue a new API key for an agent (printed once); the old key stops working at once, the id, name and history stay |
| `agent check-key` | Find which agent an API key belongs to, and whether it would be accepted; the key is read from a hidden prompt, or from stdin when piped (`echo "$KEY" \| ... agent check-key`) |
| `agent remove NAME --yes [--purge-history]` | Delete an agent from the registry; `--purge-history` also deletes its runs, status and storage samples |
| `orphans` | List stored history that nothing configures any more: agents removed from `agents.yaml`, and jobs an agent no longer reports |
| `backup [--dest FILE]` | Consistent copy of the database, safe while running |
| `restore FILE --yes` | Replace the database with a backup; the current one is first saved as `pre-restore-<time>.sqlite` |
| `vacuum` | Reclaim free space in the database file |
| `check-db` | Integrity check, size and row counts (exit status 1 when problems are found) |
| `meta` | Show internal state such as the last tick and digest times |
| `runs list --agent NAME [--job JOB] [--limit N]` | List an agent's stored runs with their ids, newest first; `JOB` is a job id or a unique job name |
| `runs delete --id ID [--id ID ...] --yes` | Delete specific runs (and their events) by id |
| `runs delete --agent NAME [--job JOB] --yes` | Delete all of an agent's stored runs, or one job's |
| `storage purge [--source S] [--name N] --yes` | Delete stored storage samples, all or for one source or name |
| `reset --yes` | Delete the database and all history |

### Admin console

`.venv/bin/python -m app admin` opens a full-screen console for the person administering the dashboard: everything the command line does, and nothing the web UI already shows. Switch tabs with `1` to `6` or the mouse; the footer lists the keys for the current tab; `r` refreshes and `q` quits.

| Tab | What you can do |
| --- | --- |
| 1 Agents | See settings and last contact. Add, edit, rename, enable or disable, issue a new key, remove. A new key is shown once, with the lines for the agent's `.env` |
| 2 Files | Check `config.yaml`, `agents.yaml` and `.env` (valid, safe permissions). **Edit** opens the file in your system editor (`$VISUAL`, `$EDITOR`, else the system default such as nano) and validates it afterwards: edit again, revert or keep. Offers a service restart after `config.yaml` or `.env` changes |
| 3 Database | Version, size and row counts. Back up, restore, check integrity, vacuum, migrate, reset, purge storage samples, and delete runs for an agent or job, or mark individual runs (Space) and delete just those. Runs are listed with time, status, target, size, id and message |
| 4 Diagnostics | Run the tick now, send a test mail, preview and send the digest, preview and test-send an offline alert (marked `[TEST]`, changes no state), find which agent an API key belongs to, view internal state, run the development server |
| 5 Service | State of the web service, tick service and timer; start, stop, restart (uses `sudo`); follow the journal |
| 6 Docs | Read the install's Markdown files (this README, AGENTS and so on). Links between documents work and `b` goes back; web links are shown but never opened |

Run it as the user that owns the install. It refuses to start as root when the install belongs to someone else, because root-owned files would stop the dashboard writing its own database. Units are found by their working directory; if that fails, pass `--unit jabs-dashboard-v2`. Administrative changes are recorded in the journal as `jabs-admin` (never keys or passwords). It needs the `textual` package, installed by `./setup.sh`.

## Agent API

An agent sends its key in the `X-API-Key` header. The endpoints are documented in the [Agent API guide](docs/AGENTS_API_GUIDE.md):

```text
POST /api/v2/heartbeat {"hostname"?, "version"?, "agent_type"?}
POST /api/v2/events    {"job_id", "target_id", "job_name"?, "target_label"?, "run_id"?, "event_type"?, ...}
POST /api/v2/purge     {"job_id", "target_id", "external_ids": [...], "message"?}
POST /api/v2/jobs      {"hostname"?: "...", "jobs": [{"job_id", "job_name"?, "schedules"?: [{"cron", "enabled"}],
                         "targets"?: [{"target_id", "target_label"?, "source"?, "destination"?}]}]}
POST /api/v2/storage   {"hostname"?: "...", "samples": [{"name", "kind": "local|cloud|repo",
                         "used_bytes", "total_bytes"?, "series"?, "timestamp"?}]}
GET  /api/heartbeat    health check for monitors such as Uptime Kuma; no key needed
```

**Identity.** The agent's id lives in `agents.yaml`; a job is its `job_id` and a target its `target_id` (a sync pair, or a snapshot job's single target). Names (`job_name`, `target_label`, the agent name) are display text the agent or administrator may change at any time: the stored row is renamed and its history stays. A `jobs` report replaces what the agent reported before; a job or target it no longer lists is kept as inactive with its history (see `orphans`). Stored storage samples count as contact, but the Overview charts only the drives and S3 buckets listed under `storage:` in `config.yaml`. Agent-supplied text is untrusted and always rendered as text, never as HTML.

**Hosts.** A job name alone is not unique when agents run on several machines, so every job is shown with its host. An agent reports its machine name in `hostname` (always used when present); otherwise `host` in `agents.yaml` is used (`agent add NAME --host HOST`), otherwise the agent name. A reported name that is not a plausible host name is ignored, not rejected.

**Up or down.** Any authenticated post counts as contact, including a bare heartbeat. With no contact for its `grace_minutes` an agent is logged `down`; the next contact logs it `up`. If the dashboard itself was not running, the gap is shown as "no data" and agents get a fresh grace period.

## Email alerts

Set `email.smtp_server` and `email.to_addrs` in `config.yaml` and the login in `.env`, then send a test (console: Diagnostics, or `.venv/bin/python -m app testmail`). Port 465 uses implicit TLS; otherwise `use_tls` selects STARTTLS; without credentials mail is sent unauthenticated (a LAN relay).

- **Offline alert** when an agent is logged `down`, repeated per `max_alerts` and `repeat_interval_minutes`, with a recovery mail after the first contact. `offline_alerts: false` on an agent in `agents.yaml` opts out.
- **Digest** on the cron `email.digest.schedule`: finished runs, failed and stopped runs with their errors, and agent outages. Skipped when nothing happened.

### Periodic tasks

The timer runs `python -m app tick` every 5 minutes: daily rollup, agent status changes, alerts, digest, drive and S3 sampling, and deleting runs and events older than `retention.runs_days` (their per-day counts stay for `rollup_days`) and old status and storage data. A failing task is logged and does not stop the others.

## Development

```bash
./setup.sh --dev      # also installs the test dependencies
.venv/bin/python -m pytest -q
```

- `tests/unit`: isolated tests, including the console (about a minute).
- `tests/contract`: a black-box agent-API suite that starts the real app on a throwaway home and talks to it over HTTP like an agent.
- `tests/js`: `node --test` for the pure helpers in `static/js/ui.js`; pytest runs them when `node` is installed.

See [AGENTS.md](AGENTS.md) for the layout and conventions.

## License

MIT, see [LICENSE.md](LICENSE.md).
