# JABS Dashboard v2

Replacement for the legacy dashboard (`../dashboard`, "v1"). Read-only web UI plus the agent ingest API, for a home lab LAN. Built in milestones; v1 and the current agents stay in service until v2 is verified and cut over.

## Rules specific to v2

- The UI never changes state and has no login. The only writes are agent ingest (API key in `X-API-Key`). All configuration is YAML (`config/config.yaml`, `config/agents.yaml`) plus `.env` for secrets; do not add UI settings or admin endpoints.
- Agent API contract: the v1 endpoints `/api/monitoring/events`, `/api/monitoring/target-purged` and `/api/heartbeat` must behave exactly as v1 so existing agents work unchanged (see `../dashboard/AGENTS_API_GUIDE.md`). New agent features are additive endpoints under `/api/v2/`, opt-in per agent (so far: `POST /api/v2/storage` with `{"samples": [{name, kind: local|cloud|repo, used_bytes, total_bytes?, series?, timestamp?}]}`).
- Agent API keys are stored only as SHA-256 hashes in `agents.yaml`; never log or return a key or hash.
- Treat agent-supplied text (messages, log lines, labels) as untrusted: render with `textContent`, cap sizes.

## Stack and layout

- Python 3.11+, Flask (`create_app()`), gunicorn (gthread), SQLite in WAL mode, pydantic for config and request payloads only. DB rows are plain dicts / `sqlite3.Row`; no ORM.
- Frontend: hand-written CSS variables and vanilla JS, Chart.js only (vendored, works offline). No Bootstrap, jQuery or build step.
- `app/__main__.py` is the management CLI (`python -m app <command>`); prefer adding a subcommand over a shell script.
- `app/db.py`: numbered `MIGRATIONS` tracked by `PRAGMA user_version`. Before the first production install migration 1 may be edited; afterwards only append.
- `app/tick.py`: periodic tasks, run every 5 minutes by a systemd timer; each task does its own due-check and keeps its state in the `meta` table.
- Email: `app/mailer.py` (SMTP send, templates in `app/mail_templates/`, `.txt` + `.html` pair per message) and `app/notify.py` (offline alerts and recovery from the logged `down`/`up` status, digest by cron schedule). Both run as tick tasks; `python -m app testmail` checks the settings.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures using `Optional[...]`.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`.
- Do not add dependencies without asking. Secrets only via `.env`.

## Testing

- pytest (`requirements-dev.txt`). `tests/unit` is fast and isolated (use the `home` fixture). `tests/contract` is a black-box agent-API suite that runs against a throwaway copy of v1 and v2: `JABS_CONTRACT_BACKENDS=v1,v2` (default both).
- `@pytest.mark.v1_bug(reason)` marks behavior v1 gets wrong; it is a strict xfail on v1 and must pass on v2.
- `tests/js` holds Node's built-in `node --test` tests for the pure helpers in `static/js/ui.js` (pytest runs them when `node` is installed). Keep DOM-free logic in `ui.js` so it stays testable.
- Add or update tests with every non-trivial change; run `.venv/bin/python -m pytest -q` before finishing.
