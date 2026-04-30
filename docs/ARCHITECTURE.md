# RELAIS — Technical Architecture

**Last updated:** 2026-04-21

This document describes the architecture as actually implemented in the repository.

---

## Active bricks

| Brick | Role | Main entry point |
|-------|------|-----------------|
| `aiguilleur` | Inbound/outbound channel adapters | `aiguilleur/main.py` |
| `portail` | Envelope validation and identity enrichment | `portail/main.py` |
| `sentinelle` | ACL, inbound and outbound routing | `sentinelle/main.py` |
| `atelier` | LLM execution via DeepAgents/LangGraph | `atelier/main.py` |
| `commandant` | Non-LLM slash commands | `commandant/main.py` |
| `souvenir` | Redis + SQLite memory | `souvenir/main.py` |
| `archiviste` | Logging and partial pipeline observation | `archiviste/main.py` |
| `forgeron` | Autonomous skill improvement via LLM trace analysis | `forgeron/main.py` |

---

## Data flow

```text
User
  -> Aiguilleur
  -> relais:messages:incoming
  -> Portail
  -> relais:security
  -> Sentinelle
     -> relais:tasks -> Atelier
     -> relais:commands -> Commandant

Atelier
  -> relais:skill:trace -> Forgeron
     -> relais:events:system (patch_applied / patch_rolled_back)
  -> relais:messages:streaming:{channel}:{correlation_id}
  -> relais:messages:outgoing_pending -> Sentinelle outgoing -> relais:messages:outgoing:{channel}
  -> relais:messages:outgoing:{channel} for some progress events
  -> relais:tasks:failed on unrecoverable failure
  (conversation history managed by LangGraph checkpointer AsyncSqliteSaver — checkpoints.db)

Commandant
  -> relais:messages:outgoing:{channel} for /help
  -> relais:memory:request for /clear

Souvenir
  observes relais:messages:outgoing:{channel}

Aiguilleur
  consumes relais:messages:outgoing:{channel}
  -> external channel
```

---

## Redis Streams

### Main pipeline

| Stream | Producer | Consumer |
|--------|----------|---------|
| `relais:messages:incoming` | Aiguilleur | Portail |
| `relais:messages:incoming:horloger` | Horloger | Portail |
| `relais:security` | Portail | Sentinelle |
| `relais:tasks` | Sentinelle | Atelier |
| `relais:commands` | Sentinelle | Commandant |
| `relais:messages:outgoing_pending` | Atelier | Sentinelle |
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant | Aiguilleur |

### Memory

| Stream / key | Producer | Consumer |
|--------------|----------|---------|
| `relais:memory:request` | Atelier, Commandant, Forgeron | Souvenir (`souvenir_group`), Forgeron (`forgeron_archive_group`) |
| `relais:memory:response` | Souvenir | agents (via SouvenirBackend) |
| `relais:memory:response:{correlation_id}` (Redis List) | Souvenir (HistoryReadHandler) | Forgeron (synchronous BRPOP) |

### Autonomous improvement (Forgeron)

| Stream / key | Producer | Consumer |
|--------------|----------|---------|
| `relais:skill:trace` | Atelier | Forgeron (`forgeron_group`) — trace analysis pipeline |
| `relais:memory:request` | Atelier | Forgeron (`forgeron_archive_group`) — auto-skill creation pipeline, Souvenir (`souvenir_group`) |
| `relais:events:system` | Forgeron | Archiviste |
| `relais:messages:outgoing_pending` | Forgeron (notifications) | Sentinelle |
| `relais:skill:annotation_cooldown:{skill_name}` (Redis String) | Forgeron | Forgeron (Phase 1 changelog cooldown) |
| `relais:skill:consolidation_cooldown:{skill_name}` (Redis String) | Forgeron | Forgeron (Phase 2 consolidation cooldown) |
| `relais:skill:creation_cooldown:{intent_label}` (Redis String) | Forgeron | Forgeron (auto-creation cooldown) |

### Streaming and errors

| Stream | Producer | Consumer |
|--------|----------|---------|
| `relais:messages:streaming:{channel}:{correlation_id}` | Atelier | streaming channel adapter |
| `relais:tasks:failed` | Atelier | observation / diagnostics |
| `relais:messages:outgoing:failed` | Aiguilleur adapters | observation / diagnostics — DLQ for undeliverable outgoing messages (`STREAM_OUTGOING_FAILED`) |
| `relais:admin:pending_users` | Portail | manual review |
| `relais:logs` | all bricks | Archiviste |
| `relais:events:messages` | various | Archiviste |

### Channel-specific Redis keys

| Key | Producer | Consumer | Description |
|-----|----------|---------|-------------|
| `relais:whatsapp:pairing` (Redis String JSON, TTL 300s) | `whatsapp_configure` tool / `python -m aiguilleur.channels.whatsapp configure --action pair` | WhatsApp adapter / operator | Active QR pairing context (`KEY_WHATSAPP_PAIRING`) |

---

## BrickBase — shared infrastructure

All pipeline bricks (`portail`, `sentinelle`, `atelier`, `souvenir`, `commandant`, `forgeron`) inherit from `common.brick_base.BrickBase`. This abstract class provides:

| Mechanism | Description |
|-----------|-------------|
| `start()` | Unified entry point: Redis connection → `on_startup()` → concurrent stream loops → `on_shutdown()` |
| `_run_stream_loop(spec, redis, shutdown_event)` | XREADGROUP loop with conditional XACK (`ack_mode="always"\|"on_success"`) |
| `reload_config()` | Atomic reload via `safe_reload` (parse → lock → swap) |
| `_start_file_watcher()` | Watches `_config_watch_paths()` via `watchfiles` |
| `_config_reload_listener()` | Listens to `relais:config:reload:{brick}` over Pub/Sub |
| `_create_shutdown()` | Instantiates `GracefulShutdown` — subclasses override for test patchability |
| `_extra_lifespan(stack)` | Hook for entering additional context managers (e.g. `AsyncSqliteSaver` in Atelier) |
| `configure_logging_once()` | Module-level function: configures `logging.basicConfig` once. Priority: env `LOG_LEVEL` > `config.yaml` `logging.level` (via `get_log_level()`) > `"INFO"` |

Each brick declares its streams via `stream_specs() -> list[StreamSpec]` and its handler `async (envelope, redis) -> bool`.

---

## Per-brick behaviour

### Aiguilleur

- Loads channels via `load_channels_config()`. When `config/aiguilleur.yaml` is absent (manually deleted or directory not initialised), a WARNING is logged and the code falls back to a minimal `discord` configuration.
- Starts one adapter per enabled channel.
- Full native Python adapters present in the repository:
  - **Discord** (`aiguilleur/channels/discord/adapter.py`) — inbound `relais:messages:incoming`, outbound `relais:messages:outgoing:discord`.
  - **WhatsApp** (`aiguilleur/channels/whatsapp/adapter.py`) — aiohttp webhook server listening to the external gateway [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) (Node.js, launched by `scripts/run_baileys.py` under supervisord, program `baileys-api` in the `optional` group). The adapter transcribes incoming WhatsApp events into Envelope → `relais:messages:incoming`, and sends outgoing replies via the gateway REST API after Markdown→WhatsApp conversion (`common/markdown_converter.convert_md_to_whatsapp()` for native `*bold*`, `_italic_`, `~strike~`). Install, configure and pair via `python -m aiguilleur.channels.whatsapp` CLI or the LangChain tools `whatsapp_install`, `whatsapp_configure`, `whatsapp_uninstall`.
  - **REST** (`aiguilleur/channels/rest/adapter.py`) — HTTP/JSON and SSE adapter for programmatic clients (CLI, CI, TUI). Exposes:
    - `POST /v1/messages` — Send a message and receive the LLM reply (JSON or SSE streaming)
    - `GET /v1/history?session_id=...&limit=...` — Retrieve a session's history (ownership enforcement via user_id)
    - `GET /v1/events` — Persistent SSE push stream: fan-out outgoing messages to concurrent subscribers (same user ID, different clients). Powered by `PushRegistry` (per-user XREAD reader tasks) and `relais:messages:outgoing:rest:{user_id}` per-user streams.
    - `GET /docs/sse` — Interactive SSE playground

    Bearer authentication via API keys in `portail.yaml`. API keys are resolved via `UserRegistry.resolve_rest_api_key()` (HMAC-SHA256 hash, never stored in plaintext). The adapter filters incoming `ACTION_MESSAGE_PROGRESS` events to resolve only the final response. The adapter also mirrors outgoing envelopes to `relais:messages:outgoing:rest:{user_id}` streams so that subscribed SSE clients can read them independently.
- Each adapter stamps `context.aiguilleur["channel_profile"]` from `ChannelConfig.profile` (aiguilleur.yaml).
- Each adapter stamps `context.aiguilleur["channel_prompt_path"]` from `ChannelConfig.prompt_path` (aiguilleur.yaml). `None` if not configured — no channel overlay is loaded.
- Each adapter stamps `context.aiguilleur["streaming"]` (`bool`) from `ChannelConfig.streaming` (read by Atelier per message, not at startup).

### Portail

- Consumes `relais:messages:incoming`.
- Validates the envelope.
- Resolves the user with `UserRegistry`.
- Writes to `context.portail`: `user_record`, `user_id` and `llm_profile` (from `context.aiguilleur["channel_profile"]` or `"default"`).
- Applies `unknown_user_policy`:
  - `deny`: silent drop
  - `guest`: stamp guest then forward
  - `pending`: writes to `relais:admin:pending_users` then drop
- Publishes to `relais:security`.

### Sentinelle

- Inbound:
  - consumes `relais:security`
  - applies ACL
  - routes to `relais:tasks` or `relais:commands`
  - replies inline for unknown or unauthorized command
- Outbound:
  - consumes `relais:messages:outgoing_pending`
  - currently passes through to `relais:messages:outgoing:{channel}`

### Atelier

- Consumes `relais:tasks`.
- Manages conversation history via a persistent LangGraph checkpointer (`AsyncSqliteSaver`, `checkpoints.db`). Thread ID is `f"{user_id}:{session_id}"` (per-session isolation for Phase 4b).
- Assembles the system prompt with `assemble_system_prompt()` (`atelier/soul_assembler.py`), which returns an `AssemblyResult(prompt, issues, is_degraded)`. If `is_degraded=True` (at least one prompt layer missing or unreadable), a WARNING is logged with the issue list, and the degraded prompt is still used (fail-soft).
- For each request, `AgentExecutor` prepends a `<relais_execution_context>` block to the first user message containing `sender_id`, `channel`, `session_id`, `correlation_id` and `reply_to` extracted from the envelope. This block is strictly technical metadata — skills (notably `channel-setup` for WhatsApp pairing) can read it for correct routing, and the system prompt instructs the model **not** to echo it back to the user.
- Executes `AgentExecutor` — returns `AgentResult(reply_text, messages_raw, tool_call_count, tool_error_count, subagent_traces)`. `subagent_traces` is a tuple of `SubagentTrace` (one per subagent that used tools) built from `SubagentMessageCapture` callbacks; empty tuple when no subagent is invoked.
- Publishes:
  - streaming text/progress to `relais:messages:streaming:{channel}:{correlation_id}`
  - some progress events to `relais:messages:outgoing:{channel}`
  - an `archive` action to `relais:memory:request` with the full reply and `messages_raw` for Souvenir archiving
  - an execution trace to `relais:skill:trace` for Forgeron (fire-and-forget; only when `skills_used` is non-empty); `context[CTX_SKILL_TRACE]` contains `skill_names`, `tool_call_count`, `tool_error_count`, `messages_raw`, `skill_paths`. Published in two cases: (a) after a successful turn when `tool_call_count > 0`, (b) on the DLQ path (`AgentExecutionError`) with `tool_error_count = -1` (sentinel: aborted turn) and `messages_raw = exc.messages_raw` (partial conversation captured from graph state). A separate trace is also published **per subagent** that used tools (`SubagentTrace`, step 7b) — messages captured by `SubagentMessageCapture` (LangChain callback injected into `RunnableConfig`) are scoped to the subagent's LangGraph namespace
  - the final reply to `relais:messages:outgoing_pending` (without `messages_raw`); `context["atelier"]["skills_used"]` stamped if skills were used
  - on agent failure (`AgentExecutionError`): a synthesized error reply from `ErrorSynthesizer` (lightweight LLM call) published to `relais:messages:outgoing_pending` so the user receives an empathetic message instead of silence
  - final errors to `relais:tasks:failed`
- **Extracted modules**:
  - `atelier/streaming.py` — `StreamBuffer`, `_extract_thinking`, `_has_tool_use_block`, `_extract_block_type`, `TaskArgsTracker`, `ChunkPayload`, `decode_chunk`; token buffering and content block extraction helpers; `TaskArgsTracker` accumulates JSON argument fragments from the `task` tool (streaming token-by-token) to extract the subagent name and maintain a namespace-ID → name mapping over the duration of a `_stream()` call; `ChunkPayload` (NamedTuple) models a validated DeepAgents astream v2 chunk (`chunk_type`, `ns`, `data`, `source` property); `decode_chunk` validates and decodes a raw dict chunk into a `ChunkPayload` or returns `None` for unknown shapes; also `REPLY_PLACEHOLDER`, `_EXECUTE_FAILURE_MARKER`, `_normalise_content` (sentinel constants and content normalisation); all extracted from `agent_executor.py` to keep each module under 800 lines.
  - `atelier/subagents_resolver.py` — pure functions for resolving `tools:` and `skills:` tokens (`_load_tools_from_import`, `_resolve_skill_token`, `_add_tool`, …); uses `common.pattern_matcher.matches` for fnmatch filtering; extracted from `atelier/subagents.py`.
  - `atelier/display_config.py` — `DisplayConfig` (frozen dataclass) loaded from `atelier.yaml` (`display:` section) via `load_display_config()`, replaces the former `progress_config.py`. Validation is per-field: each invalid key emits a WARNING and falls back to its default value (other fields remain applied) via `_validate_bool()` and `_validate_int()`.
  - `atelier/profile_model.py` — `_resolve_profile_model()`; builds the `BaseChatModel` or returns the `model` string from a `ProfileConfig`; extracted from `agent_executor.py`. Uses a `ModelHandler` `Protocol` and `_HANDLER_REGISTRY` (factory registry pattern): `DeepSeekModelHandler` (handles `deepseek:` prefix, `always_instantiate = True`, requires `langchain_deepseek`) → `DefaultModelHandler` (catch-all, delegates to `init_chat_model`, `always_instantiate = False`). The `always_instantiate` flag on each handler controls whether model instantiation is forced regardless of profile fields; it is checked first in `needs_init` before `base_url`, `api_key_env`, etc. When a provider-specific library is absent the matching handler raises `ImportError` immediately — there is no silent fallback.
  - `atelier/prompts.py` — prompt builders: `DIAGNOSTIC_MARKER` (runtime constant for diagnostic injection), `SUBAGENT_OPERATIONAL_RULES` (injected into subagent system prompts), `build_project_context_prompt`, `_build_execution_context`, `_build_core_system_prompt` (reads `atelier/SYSTEM_PROMPT.md` once, cached); extracted from `agent_executor.py`.
  - `atelier/SYSTEM_PROMPT.md` — non-user-editable core identity for the RELAIS agent: defines identity, long-term memory instructions, self-diagnosis on tool errors, diagnostic awareness, execution context handling, and operational constraints. Read by `_build_core_system_prompt()` and passed as `system_prompt=` to `create_deep_agent()`. Distinct from user-editable SOUL.md layers passed via `memory=`.
  - `atelier/transient_errors.py` — provider-agnostic transient-error detection: `_TRANSIENT_ERROR_NAMES`, `_TRANSIENT_VALUE_ERROR_PATTERNS`, `_is_transient_provider_error`; extracted from `agent_executor.py`.
  - `atelier/diagnostic_trace.py` — post-error diagnostic trace formatting: `_DIAGNOSTIC_MAX_CHARS`, `format_diagnostic_trace`, `_render_diagnostic_trace`; extracted from `agent_executor.py`. Re-exported from `agent_executor.py` for backward compatibility. Imports `DiagnosticTrace` from `atelier/errors.py` (dataclass structuring error counters and details for an aborted turn: `messages_count`, `tool_count`, `tool_errors`, `last_tool`, `last_error`, `tool_error_details`).
  - `atelier/stream_loop.py` — streaming loop state and pure helpers: `StreamLoopState` (accumulator dataclass for a `_stream()` call: `full_reply`, `last_tool_result`, `pending_tool_name`, `current_section`); `compute_reply_text` (computes the final reply text — fallback to `last_tool_result` for models without a final AI token such as nemotron-mini, then `REPLY_PLACEHOLDER`); `build_subagent_traces` (builds `SubagentTrace` objects from LangChain callback data); `handle_updates_chunk` (processes `updates`-type chunks: step transitions and subagent launch detection); `handle_tool_call_chunks` (processes tool call chunks from a messages token — primary detection via `tool_call_chunks`, fallback via `_has_tool_use_block` — returns a new immutable `StreamLoopState`); `handle_tool_result` (processes a `ToolMessage`: normalises content and updates `last_tool_result` in state); `emit_text` (emits a text token into the `StreamBuffer` or accumulates in `current_section` according to `final_only` mode); `emit_thinking` (emits a thinking block if the `thinking` event is enabled in `DisplayConfig`); extracted from `agent_executor.py`. Re-exported from `agent_executor.py` for backward compatibility.
- **Note**: inline skill annotation (formerly `SkillAnnotator` in Atelier) was migrated to Forgeron (S3 — `ChangelogWriter`). Atelier publishes traces to `relais:skill:trace`; Forgeron manages the changelog → consolidation cycle autonomously.

### Commandant

- Inherits from `BrickBase`; `stream_specs()` declares a single stream: `relais:commands` (`commandant_group`, `ack_mode="always"`).
- `/help` writes directly to `relais:messages:outgoing:{channel}`.
- `/clear` writes a `clear` action to `relais:memory:request`.
- `/sessions` writes a `sessions` action to `relais:memory:request` to list the user's recent sessions.
- `/resume <session_id>` writes a `resume` action to `relais:memory:request` to resume a previous session. Validates that the session_id belongs to the user (ownership enforcement).

### Souvenir

- Consumes `relais:memory:request` (actions: `archive`, `clear`, `file_write`, `file_read`, `file_list`, `sessions`, `resume`, `history_read`).
- `archive` action: published by Atelier after each completed LLM turn, contains the reply envelope + `messages_raw` (serialised LangChain history for that turn).
- Archives each turn in `storage/memory.db` via `LongTermStore`.
- `LongTermStore`: one row per turn in `archived_messages` (upsert on `correlation_id`) with `messages_raw` JSON, `user_content` and `assistant_content` as denormalised fields.
- The `clear` action deletes SQLite rows for the session and removes the thread from the LangGraph checkpointer (thread_id `user_id:session_id`).
- `sessions` action: returns a formatted list of the user's recent sessions (with ownership enforcement via `user_id`), publishes the reply to `relais:messages:outgoing:{channel}` via `SessionsHandler`.
- `resume` action: retrieves the full history of a previous session (ownership enforcement via `user_id`), publishes the reply to `relais:messages:outgoing:{channel}` via `ResumeHandler`.
- `history_read` action: published by Forgeron to read the full raw message history of a session; the handler reads from SQLite, truncates to a token budget (~4 chars/token), and publishes the JSON result to `relais:memory:response:{correlation_id}` (Redis List with 60s TTL) for Forgeron to retrieve via `BRPOP` (synchronous handshake).
- File actions (`file_*`) serve agent requests via `SouvenirBackend`, replying on `relais:memory:response`.
- Handlers: `ArchiveHandler`, `ClearHandler`, `FileWriteHandler`, `FileReadHandler`, `FileListHandler`, `SessionsHandler`, `ResumeHandler`, `HistoryReadHandler` — no LLM calls inside Souvenir.

### Horloger

- **Producer only**: `stream_specs()` returns `[]`; BrickBase waits on `shutdown_event` while the `_tick_loop` runs as a background task.
- Reads job YAML specs from `$RELAIS_HOME/config/horloger/jobs/*.yaml` (one file per job); automatic hot-reload via watchfiles.
- On each tick (`tick_interval_seconds`, default 30s):
  1. `JobRegistry.reload()` + `Scheduler.sync_jobs()` — reload jobs and purge history of deleted jobs.
  2. `Scheduler.get_due_jobs()` — classifies jobs into `to_trigger` / `to_skip` using four guards: future guard, catch-up guard, disabled guard, double-fire guard.
  3. Publishes a trigger envelope to `relais:messages:incoming:horloger` for each job to fire.
  4. Records each outcome in `storage/horloger.db` (SQLite via SQLModel + aiosqlite): statuses `triggered`, `publish_failed`, `skipped_catchup`, `skipped_disabled`, `skipped_double_fire`.
- **Virtual channel pattern**: the envelope traverses the full pipeline (Portail → Sentinelle → Atelier) like a real user message.
  - `sender_id = f"horloger:{job.owner_id}"` so Sentinelle applies the correct ACL.
  - `context["portail"]` pre-stamped (`user_id`, `llm_profile`) to bypass UserRegistry lookup (`"horloger"` is not a real channel in `portail.yaml`).
  - `context["aiguilleur"]["reply_to"] = job.channel` so Sentinelle routes the reply to the correct output channel.
- Storm-protection guard: jobs whose last scheduled time is older than `catch_up_window_seconds` (default 120s) are skipped, not re-triggered, after a restart.
- **`horloger-manager` native subagent**: handles CRUD of job YAML files via `/horloger` or `/schedule` commands.

| Stream | Direction |
|--------|-----------|
| `relais:messages:incoming:horloger` | Produced by Horloger, consumed by `portail_group` |
| `relais:logs` | Produced by Horloger (BrickBase) |

### Archiviste

- Observes `relais:logs`, `relais:events:system`, `relais:events:messages`.
- Observes an explicit subset of the pipeline, not all streams.
- Writes `logs/events.jsonl` and relays some logs to the Python logging subsystem.

### Forgeron

Forgeron is the skill self-improvement brick. It has two independent pipelines and inherits from `BrickBase`:

**Infrastructure**:
- `BaseAsyncStore` — Base class centralizing async SQLAlchemy engine setup (`create_async_engine`, `async_sessionmaker`) with lifecycle management (`close()`, async context manager protocol `__aenter__`/`__aexit__`). Both `SessionStore` and `SkillTraceStore` inherit from this to eliminate duplicated engine initialization.
- `on_shutdown()` — Hook called by BrickBase shutdown to cleanly close both stores' async engines.

#### Direct edit pipeline — Progressive skill improvement

- Consumes `relais:skill:trace` (group `forgeron_group`, `ack_mode="always"` — traces are advisory).
- Atelier publishes to this stream after each agent turn: skill names used, tool call and error counts, serialised LangChain raw messages (`CTX_SKILL_TRACE`), and `skill_paths` (dict `{skill_name: absolute_path}` for bundle skills). Atelier also publishes a separate trace **per subagent** that used tools (step 7b, `SubagentTrace`), with messages scoped to the subagent's LangGraph namespace via `SubagentMessageCapture`.
- Forgeron accumulates one row per trace per skill in SQLite via `SkillTraceStore` (inherits from `BaseAsyncStore`).

**Direct edit (`SkillEditor`, LLM precise)**:
- `SkillEditor` receives the current SKILL.md + the conversation trace scoped to the target skill (via `scope_messages_to_skill`). It calls the LLM once with `with_structured_output` to produce a rewritten SKILL.md and a `changed` flag.
- The SKILL.md is written only if `changed=True` and the content differs from the existing file.
- Every edit attempt (success or failure) is appended to `edit_history.jsonl` next to SKILL.md: Unix timestamp, trigger reason, LLM reason, `changed` flag, and `correlation_id`. The file is capped at 50 entries (oldest pruned) via an atomic tmp-replace write.
- Triggered by four conditions (as soon as at least one is true):
  1. **Tool errors**: `tool_error_count >= edit_min_tool_errors` (default 1)
  2. **Aborted turn**: `tool_error_count == -1` (DLQ sentinel — turn aborted by `ToolErrorGuard`)
  3. **Success after failure**: current turn has 0 errors but the previous turn for the same skill had some errors (the "correction turn")
  4. **Usage threshold**: `edit_call_threshold` cumulative calls (default 10)
- The four trigger conditions are evaluated by extracted methods: `_check_error_trigger()`, `_check_threshold_trigger()`, `_check_success_after_failure_trigger()` to reduce cyclomatic complexity.
- Rate-limited by Redis cooldown `relais:skill:edit_cooldown:{skill_name}` (TTL `edit_cooldown_seconds`).
- For bundle skills, `skill_paths` provides the absolute directory path; `SkillEditor` uses this path in priority over standard resolution.

**LLM profile**: `edit_profile` (default `"precise"`) — single LLM call per trigger (no fast phase, no periodic consolidation).

#### Auto-creation pipeline — Automatic skill creation from session archives

- Consumes `relais:memory:request` (group `forgeron_archive_group`, independent from `souvenir_group` — full fan-out via two consumer groups on the same stream).
- For each `archive` action, Forgeron extracts user messages from `CTX_SOUVENIR_REQUEST["messages_raw"]` and calls `IntentLabeler` (Haiku profile — lightweight) to obtain a normalised label (e.g. `"send_email"`).
- `SessionStore` (inherits from `BaseAsyncStore`) accumulates labelled sessions in SQLite (`session_summaries`) and maintains a counter per label in `skill_proposals`.
- When `min_sessions_for_creation` sessions share the same label (and no Redis cooldown `relais:skill:creation_cooldown:{label}` is active), `SkillCreator` generates a complete SKILL.md via LLM (profile `precise`) and writes it to `skills_dir/{skill_name}/SKILL.md`.
- Creation is idempotent: if the file already exists, `SkillCreator` returns `None` without overwriting.
- The `skill.created` event (`ACTION_SKILL_CREATED`) is published to `relais:events:system` with `context["forgeron"]` containing `skill_created`, `skill_path`, `intent_label`, `contributing_sessions`.
- If `notify_user_on_creation` is enabled, a notification is published to `relais:messages:outgoing_pending` to inform the user of the skill creation.

#### Correction pipeline — Skill redesign via trace analysis

- Triggered by `IntentLabeler` detecting a correction in a session pattern (`is_correction` field of `IntentLabelResult`).
- `_trigger_skill_design()` orchestrates a synchronous handshake with exponential backoff retry:
  1. Publishes a `history_read` request to `relais:memory:request` so Souvenir serves the full history.
  2. Sends a user notification to `relais:messages:outgoing_pending` (before BRPOP to avoid any blocking).
  3. Waits for the reply via `_fetch_history_with_retry()` — BRPOP with exponential backoff (max 2 retries, 1s and 2s delays) to handle Souvenir being slow. On timeout, processing is skipped gracefully.
  4. If history arrives, publishes an `ACTION_MESSAGE_TASK` to `relais:tasks` with `force_subagent="skill-designer"` and correction data in `context["forgeron"]` (`corrected_behavior`, `history_turns`, optional `skill_name_hint`).
- The `skill-designer` native subagent (Atelier) receives this data and generates a revised SKILL.md via the `WriteSkillTool`. `WriteSkillTool` enforces a flat-layout constraint: skills must be direct children of `$RELAIS_HOME/skills/` or of a bundle's `skills/` directory — nested subdirectories are rejected because `ToolPolicy` only scans one level deep.

**SQLite files** (in `~/.relais/storage/forgeron.db`):

| Table | Contents |
|-------|---------|
| `skill_traces` | Execution traces per skill (direct edit pipeline) |
| `session_summaries` | Archived sessions with their intent label (auto-creation pipeline) |
| `skill_proposals` | Skill proposals aggregated by label (auto-creation pipeline) |

**Configuration validation** (`forgeron/config.py`):
- `ForgeonConfig.__post_init__()` validates `llm_profile` and `edit_profile` against a whitelist of valid profiles (`default`, `fast`, `free`, `precise`, `coder`). Unknown profile values silently fall back to `"precise"` to prevent broken configurations from blocking the service.

---

## Configuration in use

### Cascade

Resolution follows:

1. `RELAIS_HOME` (default `~/.relais`)

### Main files

| File | Actual usage |
|------|-------------|
| `config/config.yaml` | reads mainly `llm.default_profile` |
| `config/portail.yaml` | users, roles, `unknown_user_policy`, `guest_role` |
| `config/sentinelle.yaml` | ACL and groups |
| `config/atelier.yaml` | display event configuration (`display:` section) |
| `config/atelier/profiles.yaml` | LLM profiles |
| `config/atelier/mcp_servers.yaml` | MCP servers |
| `config/aiguilleur.yaml` | Aiguilleur channels; copied by `initialize_user_dir()`; fallback to Discord-only if manually deleted |
| `config/forgeron.yaml` | LLM profiles (`edit_profile`, `llm_profile`), thresholds (`edit_call_threshold`, `edit_min_tool_errors`), cooldowns (`edit_cooldown_seconds`), `skills_dir`, `edit_mode`, `creation_mode`, `correction_mode` |
| `config/horloger.yaml` | `tick_interval_seconds`, `catch_up_window_seconds`, `jobs_dir`, `db_path` |

`initialize_user_dir()` copies the full set of templates declared in `common/init.DEFAULT_FILES`, including `config/aiguilleur.yaml`. Native subagents (`relais-config`, …) are **excluded** from this copy: they are shipped directly in `atelier/subagents/` (source tree) and loaded as the 2nd tier by `SubagentRegistry`. Only the `config/atelier/subagents/` directory is created empty in `RELAIS_HOME` to hold operator-supplied custom subagents.

### 2-tier subagent architecture

Atelier uses a 2-tier architecture for subagents:

| Tier | Location | Priority | Initialisation | Modification |
|------|----------|----------|----------------|-------------|
| **User** | `$RELAIS_HOME/config/atelier/subagents/{name}/` | 1st (highest) | Created manually by the operator | Editable without restart (hot-reload) |
| **Native** | `atelier/subagents/{name}/` (source) | 2nd (fallback) | Shipped with the repository | Editable via source code; hot-reload supported |

**Loading** (`SubagentRegistry.load()`):
1. Scans `$RELAIS_HOME/config/atelier/subagents/` first
2. Then scans `atelier/subagents/` (native)
3. First match by name wins (user overrides native)
4. Each directory must contain `subagent.yaml`

**Shipped native subagents**:
- `relais-config` (`atelier/subagents/relais-config/`) — configuration CRUD, WhatsApp tools, etc.
- `horloger-manager` (`atelier/subagents/horloger-manager/`) — CRUD of Horloger job YAML files; accessible via `/horloger` or `/schedule`.
- `general-purpose` (`atelier/subagents/general-purpose/`) — overrides deepagents' built-in to enforce a strict worker contract (no user-facing closings/sign-offs leaked into the orchestrator as tool results); inherits all parent MCP tools via `tool_tokens: [inherit]`.

**Usage**:
- Role access is controlled via `allowed_subagents` in `portail.yaml` (fnmatch patterns, e.g. `["relais-config"]`, `["my-*"]`)
- No code changes required to add/modify subagents — Atelier discovers them automatically

**Hot-reload**:
- Atelier watches `$RELAIS_HOME/config/atelier/subagents/` and `atelier/subagents/` via `watchfiles`
- A change in either directory triggers an atomic registry reload
- Subagents currently executing are not interrupted

**Tool token validation and degraded state**:
- At `load()`, `module:<dotted.path>` tokens and static bare-name references are validated at startup (`mcp:`, `inherit`, and `local:` forms are dynamic and skipped at this stage)
- A subagent is considered **degraded** if at least one of its tool tokens could not be resolved:
  - **At startup** (static validation): the invalid token is recorded in the `degraded_tokens` field of `SubagentSpec`
  - **At runtime** (runtime resolution): the token fails during request processing and is added to the registry's `_runtime_degraded`
- The `degraded_names` property returns the set of degraded subagent names (startup + runtime)
- Each degraded subagent is logged with a WARNING indicating the problematic token and reason (non-importable module, static tool not found, etc.)
- Degraded subagents are not excluded from the pipeline — they remain accessible but execute only with valid tools (fail-closed, never fail-silent)

### Hot configuration reload

All bricks support hot configuration reload without restart:

**Base mechanism** (implemented in `BrickBase`, inherited by all bricks):
- `_config_watch_paths()` — returns the list of YAML files to watch
- `_start_file_watcher()` — creates an asyncio task via `watch_and_reload()` to detect filesystem changes
- `reload_config()` — reloads and validates configuration (returns True/False)
- `_config_reload_listener()` — subscribes to the `relais:config:reload:{brick}` Pub/Sub channel for operator-triggered reloads

**Files watched per brick**:
- **Portail**: `config/portail.yaml` (users, roles, policies)
- **Sentinelle**: `config/sentinelle.yaml` (ACL, groups)
- **Atelier**: `config/atelier.yaml`, `config/atelier/profiles.yaml`, `config/atelier/mcp_servers.yaml`, `config/atelier/subagents/` (user), `atelier/subagents/` (native)
- **Souvenir**: no files watched — no reloadable config (Souvenir makes no LLM calls)
- **Forgeron**: `config/forgeron.yaml` (LLM profiles, `skills_dir`, `edit_call_threshold`, `edit_mode`, `creation_mode`)
- **Horloger**: no files watched — the `tick_loop` reloads via `watchfiles` on `jobs_dir` (one YAML file per job)
- **Aiguilleur**: `config/aiguilleur.yaml` (channel definitions) — see below for the soft/hard field distinction

**Reload flow**:
1. Filesystem watch via `watchfiles` (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows)
2. Change detected → atomic `safe_reload()` call: parse new YAML → acquire `_config_lock` → swap in place
3. YAML validation failure → previous configuration preserved (safe fallback)
4. External trigger: operator sends `"reload"` to `relais:config:reload:{brick}` (Pub/Sub) → manual trigger without file change

**Fail-closed guard** (Portail and Sentinelle):
Once a valid non-permissive configuration has been loaded (`_config_loaded_once = True`), any reload that would result in an empty `UserRegistry` (Portail) or empty `ACLManager` (Sentinelle) is rejected — the previous configuration is preserved. This prevents privilege escalation by deleting or emptying the configuration file in production.

**Configuration backups**:
- On each successful reload, the previous configuration is archived in `~/.relais/config/backups/{brick}_{timestamp}.yaml`
- Retention: max 5 versions per brick
- Enables audit and manual rollback if needed

**Aiguilleur hot-reload — soft vs hard fields**:
Reloading `aiguilleur.yaml` in Aiguilleur distinguishes two categories of fields:

| Category | Fields | Effect |
|----------|--------|--------|
| **Soft** | `profile`, `prompt_path`, `streaming` | Updated live without restarting the adapter. `profile` is updated via `ProfileRef.update()` (thread-safe); adapters read `adapter.config` on each incoming message. |
| **Hard** | `type`, `class_path`, `enabled`, `command` | Change detected → WARNING logged. Process restart required to apply. Adding/removing channels also requires a restart. |

The mechanism relies on an `aiguilleur-config-watcher` daemon thread that watches the file via `watchfiles` and calls `_reload_channel_profiles()` on each change. The thread is stopped cleanly by `_shutdown_event` on SIGTERM.

**Use cases**:
- Modifying ACLs (Sentinelle) without restart
- Adding/removing LLM profiles (Atelier) live
- Changing user policy (Portail)
- Changing LLM profile or prompt overlay path (Aiguilleur) live, without restarting the Discord/Telegram adapter

---

## Prompts

`assemble_system_prompt()` currently assembles 4 layers. All paths are explicit — no path is inferred by convention from the role name or channel:

1. `prompts/soul/SOUL.md` — base personality (always loaded)
2. `role_prompt_path` — relative path configured in `portail.yaml` (`roles[*].prompt_path`), stamped into `UserRecord.role_prompt_path` by Portail
3. `user_prompt_path` — relative path configured in `portail.yaml` (`users[*].prompt_path`), stamped into `UserRecord.prompt_path` by Portail. Independent from `role_prompt_path` — no fallback between the two.
4. `channel_prompt_path` — relative path configured in `aiguilleur.yaml` (`channels[*].prompt_path`), stamped into `context.aiguilleur["channel_prompt_path"]` by Aiguilleur

The files under `prompts/policies/*.md` exist in the templates but are not automatically injected into the main prompt by the current code.

---

## Envelope — `action` contract

Since 2026-04-10, `Envelope.to_json()` **raises `ValueError`** if `envelope.action` is empty. Each producing site must set `action` explicitly before publishing:

- replies derived via `Envelope.create_response_to()` or `Envelope.from_parent()` do not retain the source action — the calling code must assign the target action (`ACTION_MESSAGE_OUTGOING_PENDING`, `ACTION_MESSAGE_OUTGOING`, etc.) before calling `xadd`.
- Sites updated when this constraint was introduced: `atelier/main.py` (final reply), `sentinelle/main.py` (inline rejection), `commandant/commands.py` (`/help`), `souvenir/handlers/clear_handler.py` (`/clear` confirmation).
- Test fixtures now construct envelopes with an explicit `action=`.

This constraint prevents an envelope without a declared intent from traversing the pipeline.

---

## Storage

### Redis

- main pipeline transport
- default local socket: `<RELAIS_HOME>/redis.sock`
- TCP port `127.0.0.1:6379` open in addition to the Unix socket for external services (typically the `baileys-api` gateway)

### SQLite

- main file: `<RELAIS_HOME>/storage/memory.db`
- used by `LongTermStore` and `FileStore`
- schema initialised automatically at startup via `SQLModel.metadata.create_all`

There is no `audit.db` managed by Archiviste in the current implementation.

---

## Startup

### Supervised

The recommended path is:

```bash
./supervisor.sh start all              # Start the system
./supervisor.sh --verbose start all    # Start + follow all logs in real time
```

This starts local Redis then the Python bricks via `launcher.py`. The `--verbose` flag displays logs for all bricks after startup (Ctrl+C to detach without stopping supervisord).

> **Security**: `supervisor.sh stop <name>` and `restart <name>` validate the service name via `validate_service_name()` (allowed characters: alphanumeric, `_`, `:`, `.`, `-`). An invalid name causes immediate exit with an error code.

Supervisord groups:

| Group | Contents | Auto-start `supervisor.sh start all` |
|-------|---------|--------------------------------------|
| `infra` | `courier` (Redis) | yes |
| `core` | `portail`, `sentinelle`, `atelier`, `souvenir`, `commandant`, `archiviste`, `forgeron` | yes |
| `relays` | `aiguilleur` | yes |
| `optional` | `baileys-api` (WhatsApp Node.js gateway, launched via `scripts/run_baileys.py`) | **no** — started on demand by the `relais-config` subagent or manually via `supervisorctl start baileys-api` |

### Manual

```bash
redis-server config/redis.conf
uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python forgeron/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

---

## Bundle system (`common/bundles.py`)

Bundles are ZIP archives that distribute subagents, skills, and tools as a single installable unit.

### Bundle structure

```
my-bundle.zip
└── my-bundle/           # root folder = bundle name
    ├── bundle.yaml      # required manifest (name, description, version, author)
    ├── subagents/       # optional — one directory per subagent
    ├── skills/          # optional — one directory per skill
    └── tools/           # optional — .py files exporting BaseTool instances
```

### Installation and discovery

- Destination: `~/.relais/bundles/<bundle-name>/`
- **CLI**: `relais bundle install/uninstall/list`
- **Slash command**: `/bundle install|uninstall|list`

Security: ZIP bomb protection (> 50 MB rejected) + path traversal protection.

### Integration in Atelier

| Component | Behaviour |
|-----------|-----------|
| `ToolRegistry` | Scans `~/.relais/bundles/*/tools/*.py`; tags each tool with `_bundle_name` |
| `SubagentRegistry` | Tier 3 (after user config and native): `~/.relais/bundles/*/subagents/` |
| `ToolPolicy` | Merges `~/.relais/bundles/*/skills/*/` into skill resolution |
| Hot-reload | `watchfiles` watches `~/.relais/bundles/`; atomic reload |

Bundle subagents remain subject to the `allowed_subagents` access control in `portail.yaml`.

See `docs/BUNDLES.md` for the full format specification.

---

## Client tools (`tools/`)

### TypeScript TUI (`tools/tui-ts/`)

Alternative terminal client in TypeScript/Bun. Uses **@opentui/solid** (SolidJS terminal renderer) as the rendering engine and **solid-js** for reactivity. Compilable to a standalone binary (`bun build --compile`).

- **`src/main.tsx`** — entry point: loads config, instantiates `RelaisClient`, renders `<App>` with `@opentui/solid`, hydrates session history after the first render. Patches `TextBufferRenderable.prototype.onResize` to trigger `setWrapWidth` after Yoga has assigned the final width — required to enable dynamic word-wrap.
- **`src/app.tsx`** — root `App` component: layout (ChatHistory + InputArea + StatusBar), selection/copy management via `useSelectionHandler` and `useKeyHandler`, `/clear` command dispatch via `handleClear`.
- **`src/components/ChatHistory.tsx`** — `<scrollbox>` with sticky-scroll auto-follow, displays `<Banner>` + list of `<MessageBubble>`.
- **`src/components/InputArea.tsx`** — multi-line input area, Enter=submit, Shift+Enter=newline.
- **`src/components/StatusBar.tsx`** — status bar (session ID, send state, copy flash, error banner).
- **`src/components/MessageBubble.tsx`** — user/assistant message bubble with markdown rendering.
- **`src/lib/`** — `client.ts` (REST/SSE), `sse-parser.ts` (stateful SSE parser), `store.ts` (SolidJS reactive state), `config.ts` (YAML config + RELAIS_HOME resolution), `theme.ts` (reactive SolidJS store for the colour palette, initialised from `config.theme` at startup via `initTheme()`), `clipboard.ts`, `logger.ts`, `handle-clear.ts` (`/clear` logic: clears the UI immediately then sends `/clear` to the backend to purge Redis+SQLite history, resets `sessionId`, displays a confirmation flash or error banner).

Dependencies: `@opentui/core`, `@opentui/solid`, `solid-js`, `yaml`. Runtime: Bun ≥ 1.3.

---

## Useful references

- [README.md](../README.md)
- [docs/ENV.md](ENV.md)
- [tests/test_smoke_e2e.py](../tests/test_smoke_e2e.py)
