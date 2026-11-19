# JABS Dashboard v2

Replacement for the legacy dashboard (`../dashboard`, "v1"). Read-only web UI plus the agent ingest API, for a home lab LAN. v1 and the legacy agents are gone from the project: only this dashboard, `snapshot_agent`, `sync_agent` and `dns_backup_agent` remain.

## Rules specific to v2

- The UI never changes state and has no login. The only writes are agent ingest (API key in `X-API-Key`). All configuration is YAML (`config/config.yaml`, `config/agents.yaml`) plus `.env` for secrets; do not add UI settings or admin endpoints.
- Agent API contract (`docs/AGENTS_API_GUIDE.md`, the reference for every agent client): `POST /api/v2/heartbeat`, `/api/v2/events`, `/api/v2/purge`, `/api/v2/jobs`, `/api/v2/storage` and `GET /api/heartbeat`. There is no backward compatibility with the old `/api/monitoring/*` endpoints. Identity is by ids that never change: the agent's `id` (a UUID in `agents.yaml`, never hand-edited; `agent add` writes it, `agent adopt` fills a missing one), the agent-generated `job_id` and, within a job, `target_id`. Names (`agent.name`, `job_name`, `target_label`) are display text: a changed name renames the stored row and history stays. Never key, join or link anything by a name. Changing an endpoint breaks agents that may not be under development, so consider all of them (and bump MINOR only for additive changes).
- Agent API keys are stored only as SHA-256 hashes in `agents.yaml`; never log or return a key or hash.
- Treat agent-supplied text (messages, log lines, labels) as untrusted: render with `textContent`, cap sizes.

## Stack and layout

- Python 3.11+, Flask (`create_app()`), gunicorn (gthread), SQLite in WAL mode, pydantic for config and request payloads only. DB rows are plain dicts / `sqlite3.Row`; no ORM.
- Frontend: hand-written CSS variables and vanilla JS, Chart.js only (vendored, works offline). No Bootstrap, jQuery or build step.
- `app/__main__.py` is the management CLI (`python -m app <command>`); prefer adding a subcommand over a shell script.
- `app/admin/` is the Textual admin console (`python -m app admin`). Every CLI subcommand must also be doable there: add it to `admin.COVERAGE` and register the action id in a tab's `ACTIONS` (`TestParity` in `tests/unit/test_admin_support.py` fails otherwise). Keep logic in `app/maintenance.py`, `app/config.py` etc. so the CLI and console share it; the console only adds dialogs. Textual is imported lazily so `web` and `tick` never load it.
- `app/db.py`: numbered `MIGRATIONS` tracked by `PRAGMA user_version`. Before the first production install migration 1 may be edited; afterwards only append.
- `app/tick.py`: periodic tasks, run every 5 minutes by a systemd timer; each task does its own due-check and keeps its state in the `meta` table.
- Email: `app/mailer.py` (SMTP send, templates in `app/mail_templates/`, `.txt` + `.html` pair per message) and `app/notify.py` (offline alerts and recovery from the logged `down`/`up` status, digest by cron schedule). Both run as tick tasks; `python -m app testmail` checks the settings.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures using `Optional[...]`.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`.
- Do not add dependencies without asking. Secrets only via `.env`.
- The app's version is the `MAJOR.MINOR.PATCH` in the root `VERSION` file (read by `settings.py`); never write it anywhere else. Every JABS component follows this.

## Testing

- pytest (`requirements-dev.txt`). `tests/unit` is fast and isolated (use the `home` fixture; `tests/seed.py` writes agents, jobs, targets and runs straight into a test database). `tests/contract` is a black-box agent-API suite that starts the real app on a throwaway home and talks to it over HTTP.
- `tests/js` holds Node's built-in `node --test` tests for the pure helpers in `static/js/ui.js` (pytest runs them when `node` is installed). Keep DOM-free logic in `ui.js` so it stays testable.
- Add or update tests with every non-trivial change; run `.venv/bin/python -m pytest -q` before finishing.

## JABS-wide conventions

These apply to every JABS component: this dashboard, `snapshot_agent`, `local_sync_agent`, `nas_sync_agent` and `dns_backup_agent`. Each is its own repository next to this one, with its own README.md, LICENSE.md, `.gitignore` and `.env` (if needed). Agents that have no AGENTS.md of their own point here. The sections above are specific to the dashboard.

- **Purpose.** A home-lab LAN: unattended agents (backups, syncs, imaging) report to this dashboard, which shows them and emails a digest and offline alerts. Personal use, in active development.
- **API.** Agents talk to the dashboard only through [docs/AGENTS_API_GUIDE.md](docs/AGENTS_API_GUIDE.md); read it before writing or changing any client. Changing an endpoint can break agents that are not under development at the moment, so consider all of them first.
- **Dashboard is optional for agents.** An agent works without it; a dashboard or email failure is logged and never changes a job's result.
- **Secrets and config.** Secrets only in the component's own `.env`, never hardcoded. Non-secret settings (jobs, schedules, paths) in YAML under `config/` with an example template checked in beside the real file.
- **Version.** One `VERSION` file at the root of each component holds `MAJOR.MINOR.PATCH`; nothing else declares it. Every commit bumps it (PATCH for fixes, docs and tests; MINOR for a backward-compatible feature; MAJOR for a breaking change) and its message starts with the new number: `0.1.2 - <message>`.
- **Compatibility.** The dashboard's MAJOR number is the API version: a MAJOR change may break agents, MINOR only adds (endpoints, optional fields, opt-in features), PATCH fixes. Each agent's README lists the dashboard it works with under Requirements; see "Versioning and compatibility" in [docs/AGENTS_API_GUIDE.md](docs/AGENTS_API_GUIDE.md).
- **Python agents.** PEP8, type hints on signatures, plain dicts for config and rows (no ORM or dataclasses for them), narrow `except` clauses, no new dependencies without asking. Reuse each component's own logger setup; never `print()` for anything but one-off CLI output.
- **Bash agents** (`nas_sync_agent`, `dns_backup_agent`): `set -uo pipefail`, report to the dashboard and send email through their `jabs_client.py` and `emailer.py` (stdlib Python), and wrap those calls so a failure cannot abort the job.
- **Launcher scripts.** Components that have one (for example `local_sync_agent/jabs-agent.sh`) implement `setup`, `logs`, `reset` and `help`; `help` prints copy-paste commands built from the real paths of the current host. Use that script's prompts, colors and help layout as the style guide.
- **Tests.** `snapshot_agent` and this dashboard use pytest; `local_sync_agent`, `nas_sync_agent` and `dns_backup_agent` use stdlib `unittest` (`python3 -m unittest discover -s tests`). Add or update tests with every non-trivial change.

### README layout

Every component's README.md uses the same H2 headings in this order (loosely following the Standard Readme spec). Omit a section that does not apply; do not rename it. Component-specific topics go under `Usage` or `How it works` as H3.

1. `# Name`, then a one-paragraph description and short bullets (standalone or not, scheduling, no GUI, etc.). No heading for this part.
2. `## Requirements`
3. `## Install`: copy-paste steps; scheduling/service install as an H3.
4. `## Configuration`: `.env` keys and config files.
5. `## Usage`: commands, scheduling, admin console, restore (H3s).
6. `## How it works`: design, file layout, integrity checks.
7. `## Dashboard integration (optional)` for agents; `## Agent API` for the dashboard.
8. `## Email alerts`
9. `## Logs and troubleshooting`
10. `## Security`
11. `## Development`: tests and a link to AGENTS.md.
12. `## License`

Fenced blocks need a language, tables use `| --- |` separators, and the README must pass markdownlint with this repo's `.markdownlint.json` (`MD013` off, `MD024` siblings only).
