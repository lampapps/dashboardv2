# JABS Dashboard Agent API Guide

This document describes the HTTP API implemented in [app/web/api_ingest.py](../app/web/api_ingest.py) and [app/ingest.py](../app/ingest.py) that any agent (one of JABS's own, or a custom one) implements a client for in order to report activity to the dashboard.

It is meant to be a complete, standalone reference: a person or an AI coding assistant should be able to build a compatible agent client from this document alone, without reading the dashboard source.

The `/api/monitoring/*` and `/api/heartbeat` endpoints behave exactly as they did on the legacy v1 dashboard, so agents written for v1 work unchanged (the contract tests in `tests/contract` enforce this). New agent features are additive endpoints under `/api/v2/` and are opt-in per agent.

## Overview

- Base URL: the agent must know the dashboard's base URL (for example `http://jabs-dashboard:5002`). The JABS agents read it from `JABS_DASHBOARD_URL` (`JABS_SERVER_URL` is a deprecated alias in the older agents).
- Transport: HTTP or HTTPS, JSON request and response bodies, `Content-Type: application/json`.
- Auth: an **API key**, sent on every request in the `X-API-Key` header.
  1. An administrator registers each agent on the dashboard (`.venv/bin/python -m app agent add NAME`, or the admin console's Agents tab). The key is shown once. The dashboard stores only its SHA-256 hash.
  2. The agent keeps the key (for example `JABS_AGENT_KEY` in its `.env`) and sends it as `X-API-Key: <key>`.
  3. The dashboard identifies the agent by the key alone. Several agents can run on one machine as long as each has its own key.

  A missing header gets `401`. A key that matches no registered agent, a disabled agent, or a request from an address other than the agent's optional `allowed_ip` gets `403`. **A new agent must be registered before it can send anything.**
- Host name: an agent may send its machine name in `hostname` (events and storage). It is used when present. Otherwise the `host` set at registration is used, otherwise the agent's name. A value that is not a plausible host name is ignored, not rejected. `ip_address` and other unknown fields are ignored.
- Bodies must be JSON objects; otherwise the response is `400 {"error": "Request body must be a JSON object"}`. A field of the wrong type gives `400 {"error": "Invalid value for '<field>': <reason>"}`.
- Text fields are untrusted: long values are truncated (for example `message` at 2000 characters) and the UI shows them as plain text.
- Timestamps are Unix epoch seconds (integer or float).

## Endpoints

### 1. `POST /api/monitoring/events`

The primary endpoint. It serves three purposes, told apart by `event_type` and `target_id`:

1. **Heartbeat only** (no job in progress): omit `target_id`. Signals "I'm alive" and optionally reports the agent's version and type.
2. **Job progress**: `event_type` such as `"heartbeat"` with a `stage` describing what is happening (for example `"Starting backup"`, `"Compressing"`).
3. **Job completion**: `event_type` of `"backup_complete"` (success) or `"error"` (failure). This finalizes the run with its statistics.

#### Request body fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on. Used to tell same-named jobs on different machines apart. |
| `version` | string | no | Agent software version; stored on the agent. |
| `agent_type` | string | no | For example `"Snapshot"`, `"NAS Sync"`; stored on the agent. |
| `event_type` | string | no* | `"heartbeat"`, `"backup_complete"` or `"error"`. Needed for job and event tracking; not needed for a plain heartbeat with no `target_id`. |
| `message` | string | no | Human-readable description of the event. |
| `stage` | string | no | Short label for the current stage (progress events). |
| `timestamp` | number | no | Unix epoch seconds; defaults to the dashboard's time. |
| `run_id` | string | no | A UUID unique to one job *execution*. Correlates the start, progress and completion events of one run. Strongly recommended. |
| `job_run_id` | string | no | A UUID shared by every `target_id` reported during one overall script invocation (for example all source/destination pairs a sync agent processes in one run). Only needed by agents that report more than one `target_id` per invocation. |
| `target_id` | string | no** | A stable identifier for the logical, recurring job a run belongs to. **It must stay the same across every run of the same job** and must not embed a per-run timestamp. If omitted, the request is a pure heartbeat: the agent's contact time, version and type are updated and no run is created. |
| `target_label` | string | no | Human-readable label for the target. May change between runs. |
| `job_name` | string | no*** | The job's configured name. Required (with `target_id`) to create a run. |
| `backup_type` | string | no | For example `"full"`, `"incremental"`. A later `"full"` upgrades the stored type; it never downgrades. |
| `source` | string | no | Source being backed up (used when the run is created). |
| `destination` | string | no | Destination (used when the run is created). |
| `status` | string | no | For completion events: `"success"`, `"failed"` or `"stopped"`. Defaults from `event_type`. Send `event_type="backup_complete"` with `status="stopped"` when a job is deliberately cut short (a deadline, for example) and is expected to resume; the run is finalized without being counted as an error. |
| `duration_seconds` | number | no | Total runtime (completion events). |
| `files_backed_up` | number | no | Files processed. |
| `bytes_backed_up` | number | no | Bytes processed (uncompressed). |
| `bytes_compressed` | number | no | Bytes of the compressed output. |
| `percent_complete` | number | no | Mid-run progress, 0-100. Best effort; send on a plain `heartbeat` event while the job runs. |
| `bytes_per_second` | number | no | Current transfer rate, mid-run only. |
| `eta_seconds` | number | no | Estimated seconds remaining, mid-run only. |
| `current_item` | string | no | Short label for what is happening now, mid-run only. |
| `error_code` | number | no | Machine-readable error code (error events). |
| `error_message` | string | no | Human-readable error detail (error events). |
| `external_id` | string | no | An agent-chosen identifier for the artifact this run produced (a restic snapshot ID, a dated image file name). Meaningful on completion events. Pass the same value back in `/api/monitoring/target-purged` when the artifact is deleted. |
| `cron_schedule` | string | no | Comma-separated cron expression(s) for when `job_name` runs. Advisory: the dashboard uses it to show the next expected run and it never affects when the agent runs. May be sent with any event, including a bare heartbeat. |

\* Needed if you want the event stored; omit it (and `target_id`) for a bare heartbeat.
\** Omitting `target_id` makes the request a heartbeat.
\*** Needed only the first time a given `run_id` is reported; later events for the same `run_id` reuse the run.

#### Behavior

1. The key is validated (`401` or `403` on failure).
2. The agent's last-contact time is updated, together with `version`, `agent_type` and `hostname` when given. Any authenticated post counts as contact.
3. Without `target_id` the response is `201 {"success": true}` and nothing else happens.
4. Otherwise the run is looked up by `run_id`. If none exists it is created with status `running` from `job_name`, `backup_type`, `target_id`, `target_label`, `source`, `destination` and `job_run_id`. If one exists it is reused.
5. An event row is stored (`event_type`, `message`, `stage`, `error_code`, `timestamp`).
6. If `event_type` is `"backup_complete"` or `"error"` **and** `duration_seconds` is present, the run is finalized: status, completion time, runtime, file and byte counts, error fields, `external_id`, and `percent_complete` (100 on success, unchanged otherwise).
7. Otherwise, any of `percent_complete`, `bytes_per_second`, `eta_seconds`, `current_item`, `files_backed_up` or `bytes_backed_up` that are present are applied as a non-finalizing progress update. Omitted fields are left unchanged.

#### Responses

- `201 {"success": true}`: heartbeat only (no `target_id`).
- `201 {"success": true, "event_id": <int>, "backup_job_id": <int>}`: event recorded.
- `400 {"error": "..."}`: the body is not a JSON object, or a field has the wrong type.
- `401 {"error": "Missing API key (X-API-Key header)"}`
- `403 {"error": "Invalid API key"}`
- `403 {"error": "Agent '<name>' is disabled"}`
- `403 {"error": "API key not accepted from this address"}`
- `500 {"error": "Failed to process event"}`

#### Example: backup start (progress heartbeat)

```json
POST /api/monitoring/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "version": "1.4.0",
  "agent_type": "Snapshot",
  "event_type": "heartbeat",
  "message": "Starting backup for Jim-Home",
  "stage": "Starting backup",
  "run_id": "3f9a...uuid",
  "job_name": "Jim-Home",
  "backup_type": "full",
  "target_id": "Jim-Home",
  "target_label": "Jim-Home"
}
```

#### Example: mid-run progress update

Sent periodically (throttled, for example every 5 to 10 seconds) while a job runs. It omits `duration_seconds`, so it never finalizes the run.

```json
POST /api/monitoring/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "heartbeat",
  "message": "Backup in progress",
  "run_id": "3f9a...uuid",
  "job_name": "Jim-Home",
  "backup_type": "full",
  "target_id": "Jim-Home",
  "percent_complete": 42,
  "bytes_per_second": 125829120,
  "eta_seconds": 210,
  "current_item": "/home/jim/Documents/photos"
}
```

#### Example: backup completion (success)

```json
POST /api/monitoring/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "backup_complete",
  "message": "Backup Complete",
  "stage": "Completed",
  "status": "success",
  "run_id": "3f9a...uuid",
  "job_name": "Jim-Home",
  "backup_type": "full",
  "target_id": "Jim-Home",
  "duration_seconds": 842.3,
  "files_backed_up": 128933,
  "bytes_backed_up": 55834574848,
  "bytes_compressed": 21474836480
}
```

#### Example: backup completion (failure)

```json
POST /api/monitoring/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "error",
  "message": "Backup 'Jim-Home' failed: disk full",
  "stage": "Error",
  "status": "failed",
  "run_id": "3f9a...uuid",
  "job_name": "Jim-Home",
  "backup_type": "full",
  "target_id": "Jim-Home",
  "duration_seconds": 12.5,
  "error_code": 1,
  "error_message": "disk full"
}
```

#### Example: plain heartbeat (no active job)

```json
POST /api/monitoring/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "version": "1.4.0",
  "agent_type": "Snapshot"
}
```

## Data retention (dashboard side only)

Agents cannot tell the dashboard to delete anything. The dashboard deletes runs and events older than `retention.runs_days` in its own `config/config.yaml`, whatever the agent type or status. Per-day counts for the activity chart are kept longer (`retention.rollup_days`).

If your agent deletes specific artifacts locally (a restic snapshot removed by `forget --prune`, a dated image removed by a cleanup step), it **may** call `/api/monitoring/target-purged` right afterwards so the history shows that the data no longer exists on the agent. That only marks the matching runs as `purged`; it does not change when they are deleted. Purged runs still count as successes in the dashboard's metrics.

### 2. `POST /api/monitoring/target-purged`

Call this right after the agent has deleted one or more artifacts it reported earlier through `external_id` on a `backup_complete` event. Every run of the authenticated agent whose `target_id` matches **and** whose stored `external_id` is in `external_ids` is marked `purged`, and a `purged` event is logged on each. Nothing is deleted immediately.

`external_ids` is required and must not be empty. There is no "purge everything for this target" mode, so unrelated runs that share the `target_id` are never affected.

#### Request body fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `target_id` | string | **yes** | The `target_id` of the run(s) whose artifacts were deleted. |
| `external_ids` | array of strings | **yes** | The `external_id` values (as reported on each run's `backup_complete` event) of the deleted artifacts. |
| `message` | string | no | Human-readable note; a generic message is used if omitted. |
| `timestamp` | number | no | Unix epoch seconds; defaults to the dashboard's time. |

#### Responses

- `200 {"success": true, "purged_jobs": <int>}`
- `400 {"error": "Missing required field: target_id"}`
- `400 {"error": "Missing required field: external_ids (non-empty list)"}`
- `401 {"error": "Missing API key (X-API-Key header)"}`
- `403 {"error": "Invalid API key"}`
- `500 {"error": "Failed to record job target purge"}`

#### Example

```json
POST /api/monitoring/target-purged
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "target_id": "jim-home",
  "external_ids": ["a1b2c3d4e5f6...", "f6e5d4c3b2a1..."],
  "message": "restic forget --prune removed 2 snapshot(s)"
}
```

### 3. `POST /api/v2/storage`

Opt-in. Reports storage usage that the dashboard cannot measure itself, such as a NAS volume or a restic repository. Samples appear on the Overview's storage charts. Storage samples also count as contact for the agent.

#### Request body fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on; same rules as for events. |
| `samples` | array | **yes** | 1 to 200 samples. |

Each sample:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `name` | string | **yes** | 1 to 100 characters. What is measured (a volume or repository name). |
| `kind` | string | no | `"local"` (default), `"cloud"` or `"repo"`. |
| `series` | string | no | Up to 100 characters. Splits one `name` into stacked parts, for example one series per job. |
| `used_bytes` | integer | **yes** | Bytes used, 0 or more. |
| `total_bytes` | integer | no | Capacity in bytes, 0 or more. |
| `timestamp` | number | no | When it was measured; defaults to now. A time in the future is clamped to now. |

#### Responses

- `201 {"success": true, "stored": <int>}`
- `400 {"error": "Invalid value for '<field>': <reason>"}`
- `401`, `403`: as for events.
- `500 {"error": "Failed to record storage samples"}`

#### Example

```json
POST /api/v2/storage
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "samples": [
    {"name": "/srv/restic", "kind": "repo", "used_bytes": 412316860416, "total_bytes": 4000787030016},
    {"name": "/srv/restic", "kind": "repo", "series": "jim-home", "used_bytes": 214748364800}
  ]
}
```

### 4. `POST /api/v2/jobs`

Opt-in. Reports the jobs the agent has configured, with what each one reads and writes. The Agents page lists them in an agent's Configuration section, with the cron schedule that the agent sends in its heartbeats. Send it with every scheduler heartbeat: each report **replaces** the agent's previous one, so a job removed from the agent's config disappears. It also counts as contact for the agent.

Never put credentials in `source` or `destination`; both are stored and shown as text.

#### Request body fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on; same rules as for events. |
| `jobs` | array | **yes** | 0 to 200 jobs. An empty list clears what the agent reported. |

Each job:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `job_name` | string | **yes** | Same value as the `job_name` of its events; up to 200 characters. |
| `targets` | array | no | Up to 200 targets. A job that copies one thing has one target; a sync job has one per source/destination pair. |

Each target:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `name` | string | no | Up to 200 characters; defaults to the job name. Unique within the job (a repeated name replaces the earlier one). |
| `source` | string | no | Up to 1000 characters; several paths can be separated by newlines. |
| `destination` | string | no | Up to 1000 characters. |

Over-long text is cut, not rejected.

#### Responses

- `201 {"success": true, "stored": <int>}` (the number of targets stored)
- `400 {"error": "Invalid value for '<field>': <reason>"}`
- `401`, `403`: as for events.
- `500 {"error": "Failed to record jobs"}`

#### Example

```json
POST /api/v2/jobs
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "jobs": [
    {"job_name": "jim-home", "targets": [
      {"name": "jim-home", "source": "/home/jim", "destination": "s3:s3.amazonaws.com/bucket/jim"}]},
    {"job_name": "UNAS-KPF", "targets": [
      {"name": "backups", "source": "/mnt/nas-unas/backups", "destination": "nas2:jof"},
      {"name": "kpf", "source": "nas2:kpf", "destination": "/mnt/nas-unas/backups-kpf"}]}
  ]
}
```

### 5. `GET /api/heartbeat`

A health check for monitors such as Uptime Kuma. No key is needed. It returns `{"hostname", "version", "status": "ok", "error_event_count"}`. The dashboard's own `/healthz` is the lighter check for the web service.

## Building a compatible agent client

1. **Register the agent first** (`.venv/bin/python -m app agent add NAME` or the admin console). There is no self-registration. Keep the key secret (for example `JABS_AGENT_KEY` in the agent's `.env`).
2. Send the key as `X-API-Key: <key>` on every request, and the machine's name as `hostname`.
3. Generate a `run_id` (a UUID) once per job execution and put it on every event of that run. If the agent reports more than one `target_id` per invocation, also generate one `job_run_id` per invocation and put it on every target's events.
4. Send a start event with `event_type="heartbeat"`, `job_name`, `backup_type`, `target_id`, `target_label` and optionally `source`/`destination`. These are captured only when the run is created.
5. Optionally send progress events with the same `run_id` and `target_id`.
6. On completion, send exactly one event with `event_type="backup_complete"` (success) or `"error"` (failure), including `status`, `duration_seconds` and the counts (or `error_code`/`error_message`).
7. During idle periods send a bare heartbeat (`hostname`, `version`, `agent_type`, no `target_id`) so the agent stays "up". With no contact for its grace period the dashboard logs the agent `down` and alerts by email.
8. Treat every request as fire-and-forget: log a network failure and carry on, never let it fail or delay the job. Use short timeouts and catch `requests.exceptions.RequestException`.
9. If the agent deletes artifacts locally, report an `external_id` on each `backup_complete` event and call `/api/monitoring/target-purged` after each deletion. Do not try to delete dashboard records from the agent.

## Versioning and compatibility

The dashboard's `VERSION` (reported by `GET /api/heartbeat`) follows semantic versioning, and its MAJOR number is the API version:

- **MAJOR:** may change or remove endpoints and fields, so agents may need updating.
- **MINOR:** additive only: new endpoints, optional fields and opt-in features. Existing agents keep working.
- **PATCH:** fixes.

Agents are versioned independently, and each agent's README lists under Requirements the dashboard it works with. The dashboard is optional for agents, so an incompatible dashboard is a reason to update an agent, never a reason for a job to fail. Before dashboard 1.0.0 (the 0.x releases) nothing is promised.

## Reference implementation

[snapshot_agent/app/dashboard.py](../../snapshot_agent/app/dashboard.py) is the reference client: events, heartbeats, `target-purged` (with `external_id` and the list of snapshots a prune removed), `/api/v2/storage` and `/api/v2/jobs`. A smaller client for the v1 endpoints is [local_sync_agent/monitoring_client.py](../../local_sync_agent/monitoring_client.py), which provides `send_event()`, `send_sync_start()`, `send_sync_stage()`, `send_sync_complete()` and `send_scheduler_check()`.
