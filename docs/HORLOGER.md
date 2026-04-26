# Horloger — Cron Scheduler Brick

## Functional Overview

Horloger is the scheduled-task engine of the RELAIS pipeline. It periodically reads job definitions from YAML files, evaluates which jobs are due to fire, and injects synthetic trigger envelopes into the pipeline as if a real user had sent a message. Those envelopes then traverse the full pipeline — Portail → Sentinelle → Atelier — and produce replies routed to the job's target channel.

Horloger is a **producer-only** brick: it writes to streams but never consumes from any. It holds no consumer group, has no PEL, and cannot block on incoming messages.

---

## Architecture Position in the Pipeline

```
YAML job files (~/.relais/config/horloger/jobs/*.yaml)
      │
      ▼
Horloger (tick loop — every tick_interval_seconds)
      │
      ├── relais:messages:incoming:horloger ──► Portail (→ Sentinelle → Atelier → reply)
      └── relais:logs
```

The synthetic messages published by Horloger enter the pipeline at the same point as a real channel message. Portail handles them via the pre-stamped context (see Portail bypass below), Sentinelle applies normal ACL, and Atelier executes the job's `prompt` as it would any user request. The reply is routed to `job.channel`, not back to the `horloger` virtual channel.

---

## Tick Loop

`Horloger.on_startup()` launches an `asyncio.create_task` that runs the tick loop independently of the BrickBase main loop. Because `stream_specs()` returns `[]`, BrickBase has no consumer loop to run — it blocks on `shutdown_event` instead, keeping the process alive so background tasks can execute.

Each tick:

```
1. Reload the job registry from disk (watchfiles or forced reload if files changed)
2. Call Scheduler.get_due_jobs(jobs, now) → (to_trigger: list[DueJob], to_skip: list[DueJob])
3. For each skipped DueJob:
   a. ExecutionStore.record(job.id, due.skip_reason)  # "skipped_catchup" | "skipped_disabled" | "skipped_double_fire"
4. For each due DueJob:
   a. build_trigger_envelope(due.spec)
   b. XADD relais:messages:incoming:horloger
   c. ExecutionStore.record(job.id, "triggered" | "publish_failed")
5. Sleep tick_interval_seconds
```

`DueJob` is a dataclass carrying `spec` (the `JobSpec`), `scheduled_for` (the cron occurrence time), and `skip_reason` (set only for skipped entries). If `XADD` raises, the execution is recorded as `publish_failed` — the job will be re-evaluated on the next tick. Because scheduler guards prevent double-firing within the same tick window, the next tick will fire it again if still due.

---

## Job Definition

Jobs are defined as YAML files, one file per job, stored in `jobs_dir` (default: `~/.relais/config/horloger/jobs/`). Hot-reload is supported: `watchfiles` monitors the directory and triggers `Scheduler.sync_jobs()` when files change.

### `JobSpec` fields

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique job identifier (used as the deduplication key in the scheduler) |
| `owner_id` | `str` | User ID to impersonate (must exist in portail.yaml) |
| `schedule` | `str` | Cron expression (e.g. `"0 9 * * 1-5"` = weekdays at 09:00) |
| `channel` | `str` | Target output channel for the reply (e.g. `discord`, `telegram`) |
| `prompt` | `str` | Message to inject as the user's request |
| `enabled` | `bool` | If false, the job is loaded but never fired |
| `timezone` | `str` | IANA timezone name (e.g. `"Europe/Paris"`); defaults to UTC |
| `description` | `str?` | Optional human-readable description |
| `created_at` | `float` | Unix timestamp of creation |

### Example job YAML

```yaml
id: daily-briefing
owner_id: usr_admin
schedule: "0 8 * * 1-5"
channel: telegram
prompt: "Give me today's briefing: calendar, tasks, and weather."
enabled: true
timezone: "Europe/Paris"
description: "Daily morning briefing, Monday to Friday at 08:00 Paris time"
```

---

## Scheduler Guards

`Scheduler.get_due_jobs()` evaluates four guards in order for each job. A job is skipped at the first failing guard:

### Guard 1 — Future protection

The previous scheduled time (`_get_prev(job, now)` via `croniter`) must be in the past relative to `now`. This prevents firing a job for a tick whose scheduled time is still in the future due to clock drift.

### Guard 2 — Catch-up window

```
if (now - prev_scheduled_time) > catch_up_window_seconds:
    record("skipped_catchup")
    skip
```

Default: 120 seconds. After a prolonged downtime, Horloger could detect many overdue jobs and fire them all at once (a "thundering herd"). The catch-up window discards jobs whose last scheduled time is too old. Only the most recently missed occurrence within the window is fired.

### Guard 3 — Enabled flag

`job.enabled = False` → `record("skipped_disabled")` → skip. Jobs can be disabled without deletion, for example during maintenance or vacation periods.

### Guard 4 — Double-fire prevention

```
if last is not None and (now - last) < min_interval_seconds:
    record("skipped_double_fire")
    skip
```

`_last_triggered` is an in-memory dict mapping `job.id` to the wall-clock time (`now`) when the job was last triggered. The guard fires if a trigger was recorded recently enough that a second fire within the same cron window is possible (default `min_interval_seconds=60`). This time-elapsed check is deliberately independent of the scheduled occurrence — it prevents duplicate fires regardless of DST shifts or clock jitter.

After passing all guards, `Scheduler.mark_triggered(job.id, now)` stores the current tick time in `_last_triggered` and the method returns the job as due.

---

## Portail Bypass (Virtual Channel Pattern)

Because `horloger` is not a real channel in `portail.yaml`, Portail cannot resolve the sender via its normal UserRegistry lookup. Horloger pre-stamps the envelope's context to bypass this:

### Envelope pre-stamping

`build_trigger_envelope(job)` sets:

```python
envelope.sender_id = f"horloger:{job.owner_id}"
envelope.channel   = "horloger"

# Pre-stamp Portail context (bypasses UserRegistry channel lookup)
context["portail"]["user_id"]     = job.owner_id
context["portail"]["llm_profile"] = "default"   # or job-specific profile

# Pre-stamp Aiguilleur context (controls streaming + reply routing)
context["aiguilleur"]["streaming"]  = False
context["aiguilleur"]["reply_to"]   = job.channel
```

Portail detects a pre-stamped `context["portail"]` and skips its normal lookup. Sentinelle applies the ACL for `job.owner_id`'s role. Atelier reads `context["portail"]["user_id"]` to select the correct role and prompt layers. The reply is routed by Sentinelle to `job.channel` using `context["aiguilleur"]["reply_to"]`.

---

## Execution Trace (SQLite)

Every scheduling decision is recorded in `ExecutionStore`, backed by SQLite via SQLModel + aiosqlite.

### `horloger_executions` table

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `job_id` | `str` | Job identifier |
| `status` | `str` | `triggered`, `publish_failed`, `skipped_catchup`, `skipped_disabled`, `skipped_double_fire` |
| `scheduled_at` | `float` | Unix timestamp of the cron occurrence that was evaluated |
| `fired_at` | `float` | Unix timestamp when the evaluation happened |

The execution store serves two purposes: operator observability (why did job X not fire?) and audit (what jobs ran, when?).

---

## `horloger-manager` Native Subagent

Horloger ships a native subagent (`atelier/subagents/horloger-manager/`) that handles job CRUD via natural language. It is accessible through the standard slash-command mechanism:

```
/horloger list
/schedule "send daily briefing at 8am Paris time"
```

The subagent uses `tool_tokens: [read_file, write_file, list_directory]` to read and write job YAML files directly in `jobs_dir`. Hot-reload in Horloger picks up the changes within one tick interval.

---

## Data Model (SQLite — `~/.relais/storage/horloger.db`)

### `horloger_executions` table

See the Execution Trace section above.

---

## Redis Keys

Horloger writes to streams only. It sets no standalone Redis keys.

| Stream | Direction | Purpose |
|---|---|---|
| `relais:messages:incoming:horloger` | Produce | Synthetic trigger envelopes injected into the pipeline |
| `relais:logs` | Produce | Operational log events |

---

## Configuration Reference (`horloger.yaml`)

```yaml
horloger:
  # How often to evaluate due jobs (seconds).
  tick_interval_seconds: 30

  # Maximum age of a missed occurrence to fire (seconds).
  # Older occurrences are skipped to prevent thundering herd after downtime.
  catch_up_window_seconds: 120

  # Directory containing job YAML files (one file per job).
  # Null = ~/.relais/config/horloger/jobs/
  jobs_dir: null

  # SQLite database path for execution traces.
  # Null = ~/.relais/storage/horloger.db
  db_path: null
```

---

## Key Classes

| Class | File | Responsibility |
|---|---|---|
| `Horloger` | `horloger/main.py` | `BrickBase` subclass; producer-only (empty `stream_specs`); owns the tick loop and coordinates scheduler + envelope builder + execution store |
| `JobSpec` | `horloger/job_model.py` | Frozen dataclass representing a single cron job loaded from YAML |
| `Scheduler` | `horloger/scheduler.py` | Evaluates which jobs are due (4 guards); maintains `_last_triggered` in-memory dict; provides `sync_jobs()` for hot-reload |
| `ExecutionStore` | `horloger/execution_store.py` | Async SQLite wrapper for `horloger_executions` table; records every scheduling decision |
| `HorlogerExecution` | `horloger/execution_store.py` | SQLModel ORM class for the `horloger_executions` table |
| `build_trigger_envelope` | `horloger/envelope_builder.py` | Constructs a pre-stamped envelope with virtual channel metadata and Portail/Aiguilleur context |

---

## Important Design Decisions

### Why producer-only (no consumer group)?

Horloger's trigger is time, not an upstream message. Adding a consumer loop would serve no purpose and introduce unnecessary complexity. The `BrickBase` pattern is preserved by returning `[]` from `stream_specs()` — the framework blocks on `shutdown_event`, keeping the process alive without requiring a dummy stream.

### Why pre-stamp Portail context?

The `horloger` virtual channel cannot appear in `portail.yaml` because its sender identities change per-job (one entry per `owner_id`). Pre-stamping the context allows Portail to skip its channel lookup while still applying the correct user identity and LLM profile. This is the same pattern used by any future brick that injects synthetic messages without a real channel adapter.

### Why `reply_to` instead of replying to `horloger`?

If the reply were routed back to the `horloger` virtual channel, there would be no adapter to deliver it — Horloger has no outgoing relay. Setting `reply_to` to `job.channel` routes the reply through the normal Sentinelle outgoing path to a real adapter (Discord, Telegram, etc.) that can deliver it to the user.

### Why a catch-up window instead of firing all missed occurrences?

After an extended downtime (e.g. server restart, maintenance window), a scheduler might detect dozens of overdue job occurrences and fire them all in a burst. For daily briefings, sending 8 hours of missed briefings at once is worse than sending none. The catch-up window is a pragmatic choice: fire the most recent occurrence if it's within a short grace period, otherwise skip and wait for the next scheduled time.

### Why record every skip decision?

Operators need to understand whether a job fired or not — and why. A "skipped_catchup" record answers "why didn't my job fire after the restart?" without requiring log archaeology. The execution store provides a structured audit trail that can be queried by the `horloger-manager` subagent or an external monitoring tool.

### Why croniter with timezone-aware `_get_prev()`?

Standard cron evaluation libraries often compute scheduled times in UTC, which produces incorrect DST behaviour for Europe/Paris or America/New_York jobs. `croniter` with a `ZoneInfo`-aware `datetime` object computes the previous occurrence in wall-clock time, so a job scheduled at `08:00 Europe/Paris` fires at 08:00 local time year-round, not at a UTC offset that shifts by ±1 hour across DST transitions.
