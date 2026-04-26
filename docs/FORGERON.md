# Forgeron — Autonomous Skill Improvement Brick

## Functional Overview

Forgeron is the self-improvement engine of the RELAIS pipeline. Its mission is to make the system smarter over time, without human intervention, by observing every agent turn, detecting recurring failures and user corrections, and rewriting skill documentation accordingly.

It operates through three independent pipelines:

1. **Skill editing** — when an agent turn produces errors or recovers from a previous failure, Forgeron rewrites the relevant `SKILL.md` files to embed lessons learned.
2. **Skill auto-creation** — when the same type of user request recurs across sessions (e.g. "send a plain-text email"), Forgeron generates a brand-new `SKILL.md` from scratch.
3. **Correction pipeline** — when the user explicitly criticises or corrects the agent's behaviour in a conversation, Forgeron dispatches a `skill-designer` subagent to redesign the skill responsible for the faulty behaviour.

All three pipelines run asynchronously, in the background, and never block the main message flow.

---

## Architecture Position in the Pipeline

```
Atelier
  │
  ├── relais:skill:trace  ──────────────► Forgeron (forgeron_group)
  │                                         │
  └── relais:memory:request ───────────►  Forgeron (forgeron_archive_group)
                                            │
                                            ├── SKILL.md (direct file write)
                                            ├── relais:events:system (skill.created)
                                            ├── relais:messages:outgoing_pending (user notifications)
                                            └── relais:tasks (skill-designer dispatch)
```

Forgeron consumes two Redis Streams and produces to three output channels. It never sits on the critical path: both streams use `ack_mode="always"`, meaning messages are acknowledged unconditionally regardless of processing outcome. Losing a trace is acceptable; blocking the pipeline is not.

---

## Pipeline 1 — Skill Editing

### Trigger

Atelier publishes an envelope to `relais:skill:trace` after every completed agent turn that used at least one skill and made at least one tool call. The envelope carries a `skill_trace` context block containing:

| Field | Type | Description |
|---|---|---|
| `skill_names` | `list[str]` | All skill directory names used in the turn |
| `tool_call_count` | `int` | Total number of tool calls made |
| `tool_error_count` | `int` | Number of tool errors; `-1` means the turn was aborted by `ToolErrorGuard` |
| `messages_raw` | `list[dict]` | Full serialised LangChain message list for the turn |
| `skill_paths` | `dict[str, str]` | Optional absolute directory path per skill (used for bundle skills) |

### Trigger Conditions

For each skill in `skill_names`, Forgeron evaluates four independent conditions. An edit is triggered when **any one** is true (and `edit_mode` is enabled):

| Condition | Logic |
|---|---|
| **Tool errors** | `tool_error_count >= edit_min_tool_errors` (default: 1) |
| **Aborted turn** | `tool_error_count == -1` (sentinel set by `ToolErrorGuard`) |
| **Success after failure** | Previous turn for this skill had errors, current turn has 0 — the "recovery turn" contains the fix |
| **Usage threshold** | Cumulative call count for this skill reaches `edit_call_threshold` (default: 5); counter resets after trigger |

The "success after failure" flag (`_last_had_errors`) is tracked in-memory per skill and resets after being consumed, preventing double-triggers on consecutive successful turns.

### Cooldown Guard

A per-skill Redis TTL key (`relais:skill:edit_cooldown:{skill_name}`) prevents edit spam. Default: **300 seconds (5 minutes)**. The cooldown is checked before any LLM call; if active, the edit is silently skipped. The key is set only after a successful write, so failed or no-op edits do not consume cooldown budget.

Usage-threshold triggers bypass the cooldown (`force=True`) since they represent accumulated evidence rather than a single-turn signal.

### Scope Filtering

Before calling the LLM, Forgeron applies `scope_messages_to_skill()` to reduce the conversation to messages relevant to the target skill:

- All `HumanMessage` and `AIMessage` entries are kept (intent context).
- `ToolMessage` entries are kept only when the immediately preceding `AIMessage` contains a `read_skill` tool call that references the target skill name.
- Fallback: if the filtered result has fewer than 3 messages, the full list is used.

This prevents cross-skill contamination when a conversation uses multiple skills simultaneously.

### LLM Edit Call

`SkillEditor._call_llm()` sends two messages to the configured edit LLM profile:

- **System**: strict scope rule — only incorporate observations directly and specifically about the target skill; `changed=false` if nothing new.
- **User**: current `SKILL.md` content + scoped conversation trace.

The LLM returns a `SkillEditResult` with three fields:

| Field | Type | Meaning |
|---|---|---|
| `updated_skill` | `str` | Full rewritten `SKILL.md` content |
| `changed` | `bool` | True if the file was meaningfully modified |
| `reason` | `str` | Short explanation of the edit or why nothing changed |

### Write Guard

Even when `changed=True`, Forgeron performs a final content comparison (`updated.strip() == current.strip()`). No write occurs if the content is identical. Writes are atomic via `.tmp` + `os.replace()` (POSIX-atomic rename).

---

## Pipeline 2 — Skill Auto-Creation

### Trigger

Forgeron also consumes `relais:memory:request` via a separate consumer group (`forgeron_archive_group`), independent of Souvenir. It processes only envelopes with `action=ACTION_MEMORY_ARCHIVE`.

### Intent Labeling

`IntentLabeler` makes a single cheap LLM call (typically `default` / Gemini Flash) to classify the session's primary task type into a normalized `snake_case` label:

- Input: the first 5 human messages of the conversation (max 300 chars each).
- Output: a label like `send_email`, `summarize_pdf`, `search_web`, or `none`.
- Excluded labels: `none`, `unknown`, `general`, `chat`, `conversation`, `question`.
- Label validation: must match `^[a-z][a-z0-9_]{1,39}$`.

The labeler also detects **correction signals** (see Pipeline 3): `is_correction=True` when the user is criticising or redirecting the agent's behaviour.

### Session Accumulation

Every labeled session is recorded in SQLite (`SessionSummary` table). The `SkillProposal` aggregate table tracks how many sessions share the same label, and holds up to 10 representative session IDs (sliding window).

### Creation Threshold

Skill creation is triggered when:

1. `session_count >= min_sessions_for_creation` (default: 3)
2. The proposal status is `"pending"` (not already created)
3. No creation cooldown key is active in Redis (`relais:skill:creation_cooldown:{intent_label}`, default: 86400 s / 24 h)

### Skill Generation

`SkillCreator` calls the `precise` LLM profile with:

- The intent label as task type
- Up to `max_sessions_for_labeling` (default: 5) representative user message previews

The LLM produces a complete `SKILL.md` with mandatory YAML frontmatter (`name`, `description`) and a structured body (numbered steps, examples, edge cases). The file is written to `skills_dir/{skill_name}/SKILL.md`. Creation is idempotent: if the file already exists, `SkillCreator` returns `None`.

After creation:

- `relais:events:system` receives a `skill.created` event.
- If `notify_user_on_creation` is true, a notification is published to `relais:messages:outgoing_pending`.
- The `SkillProposal` status transitions from `"pending"` to `"created"`.

---

## Pipeline 3 — Correction Pipeline

### Trigger

When `IntentLabeler` returns `is_correction=True` and `correction_mode` is enabled, Forgeron bypasses the normal creation path and enters the correction pipeline.

### Flow

```
1. Publish history-read request → relais:memory:request (ACTION_MEMORY_HISTORY_READ)
2. Publish user notification    → relais:messages:outgoing_pending (non-blocking, fires before BRPOP)
3. BRPOP on relais:memory:response:{corr_id}  (timeout: history_read_timeout_seconds, default 30s)
4. Parse history turns
5. Publish task envelope        → relais:tasks (ACTION_MESSAGE_TASK)
   with context["forgeron"]["force_subagent"] = "skill-designer"
        context["forgeron"]["corrected_behavior"] = <description from IntentLabeler>
        context["forgeron"]["history_turns"] = <full session history>
```

The `skill-designer` subagent in Atelier receives the task and rewrites the skill based on the full correction context.

If the BRPOP times out (Souvenir did not respond), the pipeline aborts silently after publishing the user notification (which was already sent).

---

## Data Model (SQLite — `~/.relais/storage/forgeron.db`)

### `skill_traces` table

One row per completed agent turn that used skills and made at least one tool call.

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `skill_name` | `str` | Skill directory name |
| `correlation_id` | `str` | Correlation ID of the Atelier turn |
| `tool_call_count` | `int` | Number of tool calls in the turn |
| `tool_error_count` | `int` | Number of tool errors; -1 = aborted |
| `messages_raw` | `str` | JSON blob — full LangChain message list |
| `skill_path` | `str?` | Absolute skill dir path (set for bundle skills) |
| `created_at` | `float` | Unix timestamp |

### `session_summaries` table

One row per archived turn processed by Forgeron's archive consumer.

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `session_id` | `str` | Session ID from the archive envelope |
| `correlation_id` | `str` | Correlation ID of the archived turn |
| `channel` | `str` | Origin channel (`discord`, `telegram`, …) |
| `sender_id` | `str` | Origin sender_id |
| `intent_label` | `str?` | Normalized snake_case intent, or `None` |
| `user_content_preview` | `str` | First 200 chars of the user message |
| `created_at` | `float` | Unix timestamp |

### `skill_proposals` table

One row per unique intent label; tracks the path from detection to creation.

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `intent_label` | `str` | Grouping key (unique) |
| `candidate_name` | `str` | Proposed skill directory name (underscores → hyphens) |
| `session_count` | `int` | Number of sessions with this label |
| `representative_session_ids` | `str` | JSON list of up to 10 session IDs (sliding window) |
| `draft_content` | `str?` | Generated `SKILL.md` content (None before creation) |
| `status` | `str` | `pending` → `created` or `skipped` |
| `created_at` | `float` | Unix timestamp |
| `created_skill_name` | `str?` | Final skill name after creation |

---

## Redis Keys

| Key | TTL | Purpose |
|---|---|---|
| `relais:skill:edit_cooldown:{skill_name}` | `edit_cooldown_seconds` (default: 300 s) | Per-skill edit rate limiter |
| `relais:skill:creation_cooldown:{intent_label}` | `creation_cooldown_seconds` (default: 86400 s) | Per-intent creation rate limiter |
| `relais:memory:response:{corr_id}` | 60 s (set by Souvenir) | BRPOP channel for history payloads in the correction pipeline |

---

## Configuration Reference (`forgeron.yaml`)

```yaml
forgeron:
  # LLM profile for intent labeling (fast model recommended).
  llm_profile: "precise"

  # LLM profile for SKILL.md direct editing.
  edit_profile: "precise"

  # Enable direct SKILL.md editing.
  edit_mode: true

  # Minimum tool errors per turn to trigger an edit.
  edit_min_tool_errors: 1

  # Per-skill cooldown between consecutive edits (seconds).
  # Prevents the same lesson from being re-applied on every turn.
  edit_cooldown_seconds: 300

  # Trigger an edit after N cumulative calls, even without errors.
  edit_call_threshold: 5

  # Skill directory root. Null = resolved from config cascade.
  skills_dir: null

  # Enable automatic skill creation from recurring session patterns.
  creation_mode: true

  # Minimum sessions with the same intent label before creating a skill.
  min_sessions_for_creation: 3

  # Cooldown between creation attempts per intent label (seconds).
  creation_cooldown_seconds: 86400

  # Maximum representative sessions passed to SkillCreator.
  max_sessions_for_labeling: 5

  # Notify the user when a new skill is created.
  notify_user_on_creation: true

  # Enable the correction pipeline (user feedback → skill-designer).
  correction_mode: true

  # Timeout waiting for Souvenir BRPOP in the correction pipeline.
  history_read_timeout_seconds: 30
```

---

## Key Classes

| Class | File | Responsibility |
|---|---|---|
| `Forgeron` | `forgeron/main.py` | `BrickBase` subclass; owns both consumer loops and orchestrates all three pipelines |
| `SkillEditor` | `forgeron/skill_editor.py` | Scopes conversation to a skill, calls the edit LLM, applies the cooldown, writes `SKILL.md` atomically |
| `SkillCreator` | `forgeron/skill_creator.py` | Generates a complete `SKILL.md` from representative session examples |
| `IntentLabeler` | `forgeron/intent_labeler.py` | Cheap LLM call to classify a session's primary task type; also detects correction signals |
| `SkillTraceStore` | `forgeron/trace_store.py` | Async SQLite writer for the `skill_traces` table |
| `SessionStore` | `forgeron/session_store.py` | Async SQLite writer/reader for `session_summaries` and `skill_proposals`; owns creation-threshold logic |
| `ForgeonConfig` | `forgeron/config.py` | Dataclass loaded from `forgeron.yaml` via the config cascade |

---

## Important Design Decisions

### Why `ack_mode="always"`?

Forgeron is an advisory consumer. If it crashes mid-analysis or a trace is lost, the pipeline is unaffected — the agent has already responded to the user. Leaving messages in the PEL would accumulate indefinitely with no benefit. All traces are therefore acknowledged unconditionally.

### Why scope messages before sending to the LLM?

A single Atelier turn can invoke 5+ skills simultaneously. Sending the full 200-message conversation to the LLM for each skill would multiply token costs and risk cross-skill contamination (e.g. lessons from `search-web` leaking into `mail-summary`). The scope filter keeps only tool results relevant to the target skill while preserving all human and AI messages for intent context.

### Why a 5-minute edit cooldown?

A very short cooldown risks repeated edits of the same skill within a single long conversation, since the same errors appear throughout the full `messages_raw`. The 5-minute cooldown prevents rapid-fire edits on consecutive turns while still allowing the skill to be updated again within the same session if genuinely new behaviour is observed. Across turns, the LLM's `changed=False` guard provides a second layer of protection against redundant writes.

### Why "success after failure" is captured?

The turn where the agent recovers from an error is the most valuable one: it contains both the failed attempt and the correct approach. Capturing it allows Forgeron to encode the actual fix rather than just the symptom.

### Correction vs. Creation pipelines

Both pipelines start from the same `relais:memory:request` stream. The distinction is made by `IntentLabeler`'s `is_correction` flag:

- `is_correction=False` → normal creation path (accumulate sessions, create skill when threshold reached).
- `is_correction=True` → immediate correction path (fetch history, dispatch `skill-designer` subagent).

The correction path bypasses all session counting and cooldowns because user-explicit feedback is high-signal and should be acted on immediately.
