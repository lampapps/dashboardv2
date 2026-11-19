# JABS Dashboard Agent API Guide

This document describes the HTTP API implemented in [app/web/api_ingest.py](../app/web/api_ingest.py) and [app/ingest.py](../app/ingest.py) that any agent (one of JABS's own, or a custom one) implements a client for in order to report activity to the dashboard.

It is meant to be a complete, standalone reference: a person or an AI coding assistant should be able to build a compatible agent client from this document alone, without reading the dashboard source.

All agent endpoints live under `/api/v2/`. Version 0.5.0 replaced the earlier `/api/monitoring/*` endpoints; nothing of the old shape is accepted any more.

## Overview

- Base URL: the agent must know the dashboard's base URL (for example `http://jabs-dashboard:5002`). The JABS agents read it from `JABS_DASHBOARD_URL`.
- Transport: HTTP or HTTPS, JSON request and response bodies, `Content-Type: application/json`.
- Auth: an **API key**, sent on every request in the `X-API-Key` header.
  1. An administrator registers each agent on the dashboard (`.venv/bin/python -m app agent add NAME`, or the admin console's Agents tab). The key is shown once. The dashboard stores only its SHA-256 hash.
  2. The agent keeps the key (for example `JABS_AGENT_KEY` in its `.env`) and sends it as `X-API-Key: <key>`.
  3. The dashboard identifies the agent by the key alone. Several agents can run on one machine as long as each has its own key.

  A missing header gets `401`. A key that matches no registered agent, a disabled agent, or a request from an address other than the agent's optional `allowed_ip` gets `403`. **A new agent must be registered before it can send anything.**
- Host name: an agent may send its machine name in `hostname` (every endpoint except purge). It is used when present. Otherwise the `host` set at registration is used, otherwise the agent's name. A value that is not a plausible host name is ignored, not rejected. Other unknown fields are ignored.
- Bodies must be JSON objects; otherwise the response is `400 {"error": "Request body must be a JSON object"}`. A missing required field or a field of the wrong type gives `400 {"error": "Invalid value for '<field>': <reason>"}`.
- Text fields are untrusted: long values are truncated (for example `message` at 2000 characters) and the UI shows them as plain text.
- Timestamps are Unix epoch seconds (integer or float).

## Identity: ids never change, names are labels

The dashboard keys everything on ids that the agent sends and that never change, so renaming anything never splits its history.

| What | Identified by | Display text (may change freely) |
| --- | --- | --- |
| Agent | its registry id (assigned by the dashboard; the API key maps to it) | the agent name |
| Job | `job_id`, a string the agent generates once (a UUID) and keeps in the job's config | `job_name` |
| Target (one source and destination a job copies; a sync pair, or a snapshot job's single target) | `target_id`, unique within its job | `target_label` |

Rules for `job_id` and `target_id`:

- They must be the same on every run, in every request, for as long as the job or target exists.
- Never derive them from a name or path that a person might edit, and never embed a timestamp. (A job whose config has no natural stable key may use the pair name as `target_id`; renaming it then starts a new target.)
- Two agents may use the same `job_id`; they are told apart by the API key.
- Up to 200 characters.

When `job_name` or `target_label` changes, the dashboard renames the stored job or target and keeps its runs.

## Endpoints

### 1. `POST /api/v2/heartbeat`

"I am alive." Use it while idle so the agent stays up; with no contact for its grace period the dashboard logs the agent `down` and alerts by email. Every other authenticated request also counts as contact.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on. |
| `version` | string | no | Agent software version; kept until a later request replaces it. |
| `agent_type` | string | no | For example `"Snapshot"`, `"Sync"`; kept likewise. |

Response: `201 {"success": true}`.

### 2. `POST /api/v2/events`

One step of a job run: a start, a progress update, or the completion. Told apart by `event_type`:

1. **Progress**: `event_type` `"heartbeat"` with a `stage` describing what is happening (`"Starting backup"`, `"Compressing"`).
2. **Completion**: `event_type` of `"backup_complete"` (success) or `"error"` (failure), with `duration_seconds`. This finalizes the run with its statistics.

#### Request body fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `job_id` | string | **yes** | The job's stable id. |
| `target_id` | string | **yes** | The target's stable id within the job. |
| `job_name` | string | no | Display name of the job. Send it on every event; it keeps the stored name current. |
| `target_label` | string | no | Display label of the target. |
| `hostname` | string | no | The machine the agent runs on. |
| `version` | string | no | Agent software version. |
| `agent_type` | string | no | For example `"Snapshot"`, `"Sync"`. |
| `event_type` | string | no | `"heartbeat"`, `"backup_complete"` or `"error"`. |
| `message` | string | no | Human-readable description of the event. |
| `stage` | string | no | Short label for the current stage (progress events). |
| `timestamp` | number | no | Unix epoch seconds; defaults to the dashboard's time. |
| `run_id` | string | no | A UUID unique to one execution of one target. Correlates the start, progress and completion events of one run. Strongly recommended; without it every event creates its own run. |
| `job_run_id` | string | no | A UUID shared by every target reported during one overall invocation (all source/destination pairs a sync agent processes in one run). Only needed by agents that report more than one target per invocation. |
| `backup_type` | string | no | For example `"full"`, `"incremental"`. A later `"full"` upgrades the stored type; it never downgrades. |
| `source` | string | no | What this run read (captured when the run is created). |
| `destination` | string | no | What this run wrote (captured when the run is created). |
| `status` | string | no | For completion events: `"success"`, `"failed"` or `"stopped"`. Defaults from `event_type`. Send `event_type="backup_complete"` with `status="stopped"` when a job is deliberately cut short (a deadline, for example) and is expected to resume; the run is finalized without being counted as an error. |
| `duration_seconds` | number | no | Total runtime (completion events). |
| `files_backed_up` | number | no | Files processed. |
| `bytes_backed_up` | number | no | Bytes processed (uncompressed). |
| `bytes_compressed` | number | no | Bytes of the compressed output. |
| `percent_complete` | number | no | Mid-run progress, 0-100. Best effort; send on a `heartbeat` event while the job runs. |
| `bytes_per_second` | number | no | Current transfer rate, mid-run only. |
| `eta_seconds` | number | no | Estimated seconds remaining, mid-run only. |
| `current_item` | string | no | Short label for what is happening now, mid-run only. |
| `error_code` | number | no | Machine-readable error code (error events). |
| `error_message` | string | no | Human-readable error detail (error events). |
| `external_id` | string | no | An agent-chosen identifier for the artifact this run produced (a restic snapshot id, a dated image file name). Meaningful on completion events. Pass the same value back to `/api/v2/purge` when the artifact is deleted. |

A `null` for an optional field counts as absent.

#### Behavior

1. The key is validated (`401` or `403` on failure).
2. The agent's last-contact time is updated, together with `version`, `agent_type` and `hostname` when given.
3. The job (`job_id`) and target (`target_id`) are created when first seen, and renamed when `job_name` or `target_label` differ from what is stored.
4. The run is looked up by `run_id` within the target. If none exists it is created with status `running` from `backup_type`, `source`, `destination` and `job_run_id`. If one exists it is reused.
5. An event row is stored (`event_type`, `message`, `stage`, `error_code`, `timestamp`).
6. If `event_type` is `"backup_complete"` or `"error"` **and** `duration_seconds` is present, the run is finalized: status, completion time, runtime, file and byte counts, error fields, `external_id`, and `percent_complete` (100 on success, unchanged otherwise).
7. Otherwise, any of `percent_complete`, `bytes_per_second`, `eta_seconds`, `current_item`, `files_backed_up` or `bytes_backed_up` that are present are applied as a non-finalizing progress update. Omitted fields are left unchanged. A later verify-step event without `duration_seconds` never overwrites a finished run's outcome.

#### Responses

- `201 {"success": true, "event_id": <int>, "run_pk": <int>}`: event recorded.
- `400 {"error": "..."}`: not a JSON object, a required field is missing, or a field has the wrong type.
- `401 {"error": "Missing API key (X-API-Key header)"}`
- `403 {"error": "Invalid API key"}`, `{"error": "Agent '<name>' is disabled"}` or `{"error": "API key not accepted from this address"}`
- `500 {"error": "Failed to process event"}`

#### Example: backup start

```json
POST /api/v2/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "version": "1.4.0",
  "agent_type": "Snapshot",
  "event_type": "heartbeat",
  "message": "Starting backup for Jim-Home",
  "stage": "Starting backup",
  "run_id": "3f9a...uuid",
  "job_id": "b1c4...uuid",
  "job_name": "Jim-Home",
  "backup_type": "full",
  "target_id": "b1c4...uuid",
  "target_label": "Jim-Home"
}
```

#### Example: mid-run progress update

Sent periodically (throttled, for example every 5 to 10 seconds) while a job runs. It omits `duration_seconds`, so it never finalizes the run.

```json
POST /api/v2/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "heartbeat",
  "message": "Backup in progress",
  "run_id": "3f9a...uuid",
  "job_id": "b1c4...uuid",
  "job_name": "Jim-Home",
  "target_id": "b1c4...uuid",
  "percent_complete": 42,
  "bytes_per_second": 125829120,
  "eta_seconds": 210,
  "current_item": "/home/jim/Documents/photos"
}
```

#### Example: backup completion (success)

```json
POST /api/v2/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "backup_complete",
  "message": "Backup Complete",
  "stage": "Completed",
  "status": "success",
  "run_id": "3f9a...uuid",
  "job_id": "b1c4...uuid",
  "job_name": "Jim-Home",
  "target_id": "b1c4...uuid",
  "backup_type": "full",
  "duration_seconds": 842.3,
  "files_backed_up": 128933,
  "bytes_backed_up": 55834574848,
  "bytes_compressed": 21474836480,
  "external_id": "a1b2c3d4"
}
```

#### Example: backup completion (failure)

```json
POST /api/v2/events
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "event_type": "error",
  "message": "Backup 'Jim-Home' failed: disk full",
  "stage": "Error",
  "status": "failed",
  "run_id": "3f9a...uuid",
  "job_id": "b1c4...uuid",
  "job_name": "Jim-Home",
  "target_id": "b1c4...uuid",
  "duration_seconds": 12.5,
  "error_code": 1,
  "error_message": "disk full"
}
```

### 3. `POST /api/v2/purge`

Call this right after the agent has deleted one or more artifacts it reported earlier through `external_id` on a completion event (a restic snapshot removed by `forget --prune`, a dated image removed by a cleanup step). Every run of the authenticated agent's `job_id` and `target_id` whose stored `external_id` is in `external_ids` is marked `purged`, and a `purged` event is logged on each. Nothing is deleted from the dashboard; the runs are removed by retention like any other. Purged runs still count as successes in the dashboard's metrics.

`external_ids` is required and must not be empty. There is no "purge everything for this target" mode, so unrelated runs that share the target are never affected.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `job_id` | string | **yes** | The job whose artifacts were deleted. |
| `target_id` | string | **yes** | The target of the run(s) whose artifacts were deleted. |
| `external_ids` | array of strings | **yes** | The `external_id` values of the deleted artifacts. |
| `message` | string | no | Human-readable note; a generic message is used if omitted. |
| `timestamp` | number | no | Unix epoch seconds; defaults to the dashboard's time. |

Responses: `200 {"success": true, "purged_runs": <int>}`; `400` for a missing field; `401`, `403` as above; `500 {"error": "Failed to record job target purge"}`.

```json
POST /api/v2/purge
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "job_id": "b1c4...uuid",
  "target_id": "b1c4...uuid",
  "external_ids": ["a1b2c3d4e5f6...", "f6e5d4c3b2a1..."],
  "message": "restic forget --prune removed 2 snapshot(s)"
}
```

### 4. `POST /api/v2/jobs`

Reports the jobs the agent has configured, with what each one reads and writes. The Agents page lists them in an agent's Configuration section, with each job's schedules and whether each is enabled, and the Jobs table uses the enabled schedules to show the next expected run. Send it whenever the agent starts a scheduler check: each report **replaces** the agent's previous one. It also counts as contact for the agent.

A job or target missing from a report is marked inactive, not deleted: its run history stays (and an administrator can see it with `python -m app orphans` and delete it with `runs delete`). A job that returns in a later report is active again.

Never put credentials in `source` or `destination`; both are stored and shown as text.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on. |
| `jobs` | array | **yes** | 0 to 200 jobs. An empty list marks all the agent's jobs inactive. |

Each job:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `job_id` | string | **yes** | The job's stable id (same as on its events). |
| `job_name` | string | no | Display name; up to 200 characters. |
| `targets` | array | no | Up to 200 targets. A job that copies one thing has one target; a sync job has one per source/destination pair. |
| `schedules` | array | no | Up to 20 schedules the job has, **including disabled ones**, each `{"cron": "0 2 * * *", "enabled": true}` (`enabled` defaults to false). An empty list means the job has no schedules. |

Each target:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `target_id` | string | **yes** | The target's stable id within the job (same as on its events). |
| `target_label` | string | no | Display label; defaults to the id. |
| `source` | string | no | Up to 1000 characters; several paths can be separated by newlines. |
| `destination` | string | no | Up to 1000 characters. |

Over-long text is cut, not rejected.

Responses: `201 {"success": true, "stored": <int>}` (the number of targets stored); `400`; `401`, `403`; `500 {"error": "Failed to record jobs"}`.

```json
POST /api/v2/jobs
X-API-Key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
{
  "hostname": "backup-host",
  "jobs": [
    {"job_id": "b1c4...uuid", "job_name": "jim-home", "schedules": [{"cron": "0 0 * * *", "enabled": true}],
     "targets": [{"target_id": "b1c4...uuid", "target_label": "jim-home", "source": "/home/jim",
                  "destination": "s3:s3.amazonaws.com/bucket/jim"}]},
    {"job_id": "7d20...uuid", "job_name": "UNAS-KPF", "targets": [
      {"target_id": "backups", "source": "/mnt/nas-unas/backups", "destination": "nas2:jof"},
      {"target_id": "kpf", "source": "nas2:kpf", "destination": "/mnt/nas-unas/backups-kpf"}]}
  ]
}
```

### 5. `POST /api/v2/storage`

Opt-in. Reports storage usage that the dashboard cannot measure itself, such as a NAS volume or a restic repository. Samples appear on the Overview's storage charts. Storage samples also count as contact for the agent.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `hostname` | string | no | The machine the agent runs on. |
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

Responses: `201 {"success": true, "stored": <int>}`; `400 {"error": "Invalid value for '<field>': <reason>"}`; `401`, `403`; `500 {"error": "Failed to record storage samples"}`.

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

### 6. `GET /api/heartbeat`

A health check for monitors such as Uptime Kuma. No key is needed. It returns `{"hostname", "version", "status": "ok", "error_event_count"}`. The dashboard's own `/healthz` is the lighter check for the web service.

## Data retention (dashboard side only)

Agents cannot tell the dashboard to delete anything. The dashboard deletes runs and events older than `retention.runs_days` in its own `config/config.yaml`, whatever the agent type or status. Per-day counts for the activity chart are kept longer (`retention.rollup_days`).

## Building a compatible agent client

1. **Register the agent first** (`.venv/bin/python -m app agent add NAME` or the admin console). There is no self-registration. Keep the key secret (for example `JABS_AGENT_KEY` in the agent's `.env`).
2. Give every job a `job_id` (a UUID) once, store it in the job's config, and never change it. Choose a stable `target_id` for each thing the job copies.
3. Send the key as `X-API-Key: <key>` on every request, and the machine's name as `hostname`.
4. Generate a `run_id` (a UUID) once per execution of a target and put it on every event of that run. If the agent reports more than one target per invocation, also generate one `job_run_id` per invocation and put it on every target's events.
5. Send a start event with `event_type="heartbeat"`, `job_id`, `job_name`, `target_id`, `target_label`, `backup_type` and optionally `source`/`destination`. These are captured when the run is created.
6. Optionally send progress events with the same `run_id`, `job_id` and `target_id`.
7. On completion, send exactly one event with `event_type="backup_complete"` (success) or `"error"` (failure), including `status`, `duration_seconds` and the counts (or `error_code`/`error_message`).
8. Report the configured jobs with `/api/v2/jobs` and, during idle periods, `/api/v2/heartbeat`, so the agent stays "up".
9. Treat every request as fire-and-forget: log a network failure and carry on, never let it fail or delay the job. Use short timeouts and catch `requests.exceptions.RequestException`.
10. If the agent deletes artifacts locally, report an `external_id` on each completion event and call `/api/v2/purge` after each deletion. Do not try to delete dashboard records from the agent.

## Versioning and compatibility

The dashboard's `VERSION` (reported by `GET /api/heartbeat`) follows semantic versioning, and its MAJOR number is the API version:

- **MAJOR:** may change or remove endpoints and fields, so agents may need updating.
- **MINOR:** additive only: new endpoints, optional fields and opt-in features. Existing agents keep working.
- **PATCH:** fixes.

Agents are versioned independently, and each agent's README lists under Requirements the dashboard it works with. The dashboard is optional for agents, so an incompatible dashboard is a reason to update an agent, never a reason for a job to fail. Before dashboard 1.0.0 (the 0.x releases) nothing is promised: 0.5.0 changed the identity model and the endpoints together.

## Reference implementation

[snapshot_agent/app/dashboard.py](../../snapshot_agent/app/dashboard.py) is the reference client: heartbeats, events (with `external_id`), `purge` (with the list of snapshots a prune removed), `/api/v2/storage` and `/api/v2/jobs`.
