---
name: horloger-manager
description: >
  Reference skill for the horloger-manager subagent. Provides the complete
  job YAML schema, cron expression cheat-sheet, step-by-step operation guides,
  file path conventions, and troubleshooting checklist for managing HORLOGER
  scheduled tasks. Activate when creating, editing, listing, deleting, enabling,
  or disabling HORLOGER cron jobs.
metadata:
  author: RELAIS
  version: "1.0"
allowed-tools:
  - read_file
  - write_file
  - list_directory
---

# horloger-manager

## Overview

HORLOGER is the RELAIS scheduling brick. It reads job files from a jobs
directory and, at the scheduled time, injects a synthetic message into the
pipeline exactly as if the `owner_id` user had typed `prompt` on `channel`.

This skill documents the job YAML schema, file path conventions, supported
operations, and troubleshooting steps for the horloger-manager subagent.

---

## File Paths

| Purpose | Path |
|---------|------|
| Jobs directory | `$RELAIS_HOME/config/horloger/jobs/` |
| Default (no RELAIS_HOME) | `~/.relais/config/horloger/jobs/` |
| Job file naming | `{id}.yaml` — filename must equal the `id` field |

HORLOGER watches the jobs directory with `watchfiles` and hot-reloads within
~30 seconds of any file change. No brick restart is needed.

---

## Job YAML Reference

### Schema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `id` | string | yes | — | Unique kebab-case identifier. Must match the filename (without `.yaml`). Pattern: `[a-z0-9][a-z0-9-]*`. |
| `owner_id` | string | yes | — | User key from `portail.yaml` (e.g. `usr_alice`). The synthetic message is injected as this user. |
| `schedule` | string | yes | — | Standard 5-field cron expression (see cheat-sheet below). |
| `channel` | string | yes | — | Channel name where the message is injected (e.g. `discord`, `telegram`, `rest`). |
| `prompt` | string | yes | — | The message text HORLOGER injects as if the user sent it. |
| `enabled` | boolean | yes | `true` | When `false`, HORLOGER skips this job entirely. |
| `created_at` | string | yes | — | ISO-8601 UTC timestamp of job creation (e.g. `2026-04-20T08:00:00Z`). |
| `description` | string | yes | — | Human-readable purpose shown in listings. |
| `timezone` | string | no | `"UTC"` | IANA timezone for schedule interpretation (e.g. `Europe/Paris`, `America/New_York`). |

### Complete example

```yaml
id: weather-morning
owner_id: usr_alice
schedule: "0 8 * * *"
channel: discord
prompt: "Give me the morning weather forecast for Paris"
enabled: true
created_at: "2026-04-20T08:00:00Z"
description: "Daily morning weather briefing"
timezone: "Europe/Paris"
```

---

## Cron Expression Cheat-Sheet

A cron expression has **exactly 5 space-separated fields**:

```
┌───────────── minute        (0–59)
│ ┌─────────── hour          (0–23)
│ │ ┌───────── day of month  (1–31)
│ │ │ ┌─────── month         (1–12 or JAN–DEC)
│ │ │ │ ┌───── day of week   (0–7, 0 and 7 = Sunday, or SUN–SAT)
│ │ │ │ │
* * * * *
```

| Natural language | Cron expression |
|-----------------|-----------------|
| Every minute | `* * * * *` |
| Every hour (at :00) | `0 * * * *` |
| Every day at 8:00 AM | `0 8 * * *` |
| Every day at midnight | `0 0 * * *` |
| Every weekday at 9:00 AM | `0 9 * * 1-5` |
| Every Monday at 7:30 AM | `30 7 * * 1` |
| Every Sunday at 6:00 PM | `0 18 * * 0` |
| Every 15 minutes | `*/15 * * * *` |
| Every 2 hours | `0 */2 * * *` |
| First day of each month at noon | `0 12 1 * *` |
| Every day at 8:00 AM and 6:00 PM | `0 8,18 * * *` |

Special strings (when HORLOGER supports them):

| String | Equivalent |
|--------|-----------|
| `@hourly` | `0 * * * *` |
| `@daily` | `0 0 * * *` |
| `@midnight` | `0 0 * * *` |
| `@weekly` | `0 0 * * 0` |
| `@monthly` | `0 0 1 * *` |

---

## Operations Guide

### List all jobs

1. `list_directory("$RELAIS_HOME/config/horloger/jobs/")`
2. For each `.yaml` file: `read_file` the content.
3. Present a summary table: `id | schedule | channel | owner_id | enabled | description`.
4. If empty, inform the user and offer to create a first job.

### Create a job

1. Collect required fields (prompt user one question at a time if missing).
2. Convert natural-language schedule to a 5-field cron expression; show it.
3. Validate: expression must have exactly 5 whitespace-separated fields.
4. Set `created_at` to current UTC time (`YYYY-MM-DDTHH:MM:SSZ`).
5. Build the full YAML string.
6. Show it and wait for explicit confirmation before writing.
7. `write_file("$RELAIS_HOME/config/horloger/jobs/{id}.yaml", content)`.
8. Confirm: "Job `{id}` created. HORLOGER will pick it up within ~30 seconds."

### Edit a job

1. `read_file("$RELAIS_HOME/config/horloger/jobs/{id}.yaml")`.
2. Apply only the requested changes (leave all other fields untouched).
3. If changing `schedule`, validate the new expression.
4. Show before/after diff; wait for explicit confirmation.
5. `write_file` the updated content.

### Disable a job (soft)

1. `read_file` the job file.
2. Set `enabled: false`.
3. `write_file` the updated content (no confirmation needed for unambiguous requests).
4. Confirm: "Job `{id}` disabled. HORLOGER will skip it until re-enabled."

### Enable a job

1. `read_file` the job file.
2. Set `enabled: true`.
3. `write_file` the updated content.
4. Confirm: "Job `{id}` enabled. HORLOGER will run it on schedule."

### Delete a job

**Soft delete (default — recommended)**
1. Read the file to confirm it exists.
2. Set `enabled: false`; rewrite.
3. Inform the user the job is disabled (can be re-enabled).

**Hard delete (permanent — only on explicit user request)**
1. Read the file to show the user exactly what will be deleted.
2. Ask: "This will permanently delete job `{id}`. Are you sure? (yes/no)"
3. Wait for explicit `yes`.
4. Inform user the file will be deleted (use appropriate tool).
5. Confirm: "Job `{id}` permanently deleted."

---

## Security Rules

| Rule | Detail |
|------|--------|
| `owner_id` is immutable | Never change `owner_id` unless the caller has `admin` role. Read `<relais_execution_context>` to verify. |
| Confirm hard deletes | Always ask twice before removing a file permanently. |
| Validate cron | Reject any expression with ≠ 5 fields. Explain the correct format. |
| Strict file scope | Write only to `$RELAIS_HOME/config/horloger/jobs/*.yaml`. |
| No secrets in prompts | Refuse if the user tries to embed API keys or passwords in the `prompt` field. |

---

## Troubleshooting

### Job doesn't fire

| Check | How |
|-------|-----|
| `enabled: false`? | `read_file` the job; verify `enabled: true`. |
| Invalid cron expression? | Count the fields — must be exactly 5. Check for typos or extra spaces. |
| `owner_id` not in portail.yaml? | Read `config/portail.yaml`; confirm the key exists and `blocked: false`. |
| Channel not active? | Read `config/aiguilleur.yaml`; confirm the channel is `enabled: true`. |
| HORLOGER brick not running? | Check `supervisorctl status horloger`. Restart if stopped. |
| Wrong timezone? | HORLOGER interprets `schedule` in the `timezone` field; default is UTC. Verify the user's intent. |

### File not found

HORLOGER ignores files that are not valid YAML or whose `id` field does not
match the filename. If a job disappears from the list after creation:
1. Verify the filename is `{id}.yaml` (exact match, no spaces or uppercase).
2. `read_file` the file and check YAML syntax (no tabs, correct indentation).

### Hot-reload delay

HORLOGER watches the jobs directory with `watchfiles`. Changes are detected
within ~5 seconds and applied within ~30 seconds. If a job still does not
appear after 60 seconds, restart HORLOGER: `supervisorctl restart horloger`.

---

## References

- HORLOGER brick source: `horloger/`
- Jobs directory: `$RELAIS_HOME/config/horloger/jobs/`
- RELAIS architecture: `docs/ARCHITECTURE.md`
- Cron syntax reference: https://crontab.guru/
