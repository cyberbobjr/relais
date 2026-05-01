# Atelier — LLM Agent Execution Brick

## Functional Overview

Atelier is the intelligence core of the RELAIS pipeline. It receives user tasks, assembles a personalised system prompt, selects tools and skills, and drives a multi-turn agentic loop via `deepagents.create_deep_agent()`. The resulting reply is streamed token-by-token to the user's channel and archived for memory and self-improvement.

Atelier is the only brick that calls LLMs. All other bricks are deterministic processors. It is also the most complex brick, coordinating eight subsystems: soul assembly, LLM profile resolution, tool policy, skill resolution, subagent registry, MCP session management, error synthesis, and memory archiving.

---

## Architecture Position in the Pipeline

```
Sentinelle
  │
  ├── relais:tasks ──────────────────────────────► Atelier (atelier_group, ack_mode="on_success")
  │                                                     │
  └── relais:atelier:control ──────────────────► Atelier (atelier_control_group, ack_mode="always")
                                                        │
                                                        ├── relais:messages:outgoing_pending     (reply → Sentinelle outgoing)
                                                        ├── relais:messages:streaming:{ch}:{id}  (token-by-token stream)
                                                        ├── relais:memory:request               (archive to Souvenir)
                                                        ├── relais:skill:trace                  (trace to Forgeron)
                                                        ├── relais:tasks:failed                 (DLQ — exhausted retries)
                                                        └── relais:logs
```

Both streams use `ack_mode="on_success"`: a message is acknowledged only after a successful reply has been published (or a final error routed to the DLQ). Transient failures leave the envelope in the PEL for automatic re-delivery.

---

## Message Handling Flow (`_handle_envelope`)

Every envelope on `relais:tasks` goes through a 9-step pipeline inside `_handle_envelope`:

```
Step 1   Resolve LLM profile
         └─ context["portail"]["llm_profile"] → load ProfileConfig from profiles.yaml

Step 2   Resolve memory paths (SoulAssembler)
         └─ Layer 1: soul/SOUL.md (core personality — always attempted)
            Layer 2: role_prompt_path from portail.yaml (role overlay)
            Layer 3: user_prompt_path from portail.yaml (per-user override)
            Layer 4: channel_prompt_path from aiguilleur.yaml (channel formatting)
            Returns list[str] of validated absolute paths → passed as memory= to create_deep_agent()

Step 3   Resolve skills (ToolPolicy)
         └─ ToolPolicy.resolve_skill_dirs(role) → list of absolute skill directory paths

Step 4   Filter MCP tools (ToolPolicy)
         └─ ToolPolicy.filter_mcp_tools(tools, role) → filtered list[BaseTool]

Step 5   Resolve subagents (SubagentRegistry)
         └─ SubagentRegistry.get_allowed(role, allowed_patterns) → list[SubagentDef]

Step 6   force_subagent override (optional)
         └─ If context["forgeron"]["force_subagent"] is set, override the subagent list

Step 7   Execute agent loop (AgentExecutor)
         └─ AgentExecutor.execute(envelope, ...) → AgentResult

Step 8   Publish skill trace → relais:skill:trace (fire-and-forget, for Forgeron)

Step 9   Publish reply → relais:messages:outgoing_pending
         └─ Then archive turn → relais:memory:request (ACTION_MEMORY_ARCHIVE)
```

If an `AgentExecutionError` is raised at Step 7, `ErrorSynthesizer` produces a user-facing error reply before routing the original envelope to the DLQ. If `ExhaustedRetriesError` is raised, the envelope goes directly to the DLQ without a user reply.

---

## Execution Context Block

On every agent turn, `AgentExecutor._build_execution_context()` prepends a `<relais_execution_context>` XML block to the first HumanMessage. The block carries:

```xml
<relais_execution_context>
  <sender_id>discord:123456</sender_id>
  <channel>discord</channel>
  <session_id>sess_abc123</session_id>
  <correlation_id>corr_xyz789</correlation_id>
  <reply_to>discord</reply_to>
</relais_execution_context>
```

This metadata is visible to the model and to skills (notably `channel-setup`). The system prompt instructs the model not to echo this block back to the user. The block is stripped from the final reply before publication.

---

## AgentExecutor

`AgentExecutor` (`atelier/agent_executor.py`) owns the LangGraph agent lifecycle and the retry logic. It is instantiated per-request by `Atelier._handle_envelope`.

### Initialisation

```python
AgentExecutor(
    profile=profile,           # ProfileConfig: model, max_turns, mcp_timeout, mcp_max_tools, resilience
    memory_paths=memory_paths, # list[str] of validated absolute paths from SoulAssembler → memory=
    mcp_servers=servers,       # dict from McpSessionManager.load_for_sdk()
    tools=tools,               # list[BaseTool] filtered by ToolPolicy
    skills=skill_dirs,         # list[str] absolute paths to skill directories
    subagents=subagents,       # list[SubagentDef] allowed for this role
    checkpointer=checkpointer, # AsyncSqliteSaver shared by all requests
    backend=backend,           # CompositeBackend (LocalShellBackend + SouvenirBackend)
)
```

`create_deep_agent()` is called during `__init__`. The `model` field uses the `provider:model-id` format (e.g. `anthropic:claude-sonnet-4-6`).

### Retry Loop (`execute`)

```
for attempt in range(profile.resilience.retry_attempts):
    try:
        result = await _run_once(envelope, context, stream_callback)
        return result
    except TRANSIENT_ERRORS:      # ConnectError, TimeoutException, etc.
        await asyncio.sleep(profile.resilience.retry_delays[attempt])
        continue
    except AgentExecutionError:
        raise                     # non-transient: surfaces to _handle_envelope
raise ExhaustedRetriesError(...)  # DLQ path
```

Transient errors leave the envelope in the PEL (no XACK). `AgentExecutionError` triggers `ErrorSynthesizer`. `ExhaustedRetriesError` routes to `relais:tasks:failed`.

### Single Run (`_run_once`)

1. Builds `RunnableConfig(thread_id="{user_id}:{session_id}", callbacks=[SubagentMessageCapture()])`.
2. Prepends the execution context block to the user message.
3. Calls `_stream(input, config, stream_callback)`.
4. Calls `agent.aget_state(config)` to retrieve the full `messages_raw` list from the LangGraph checkpoint.
5. Returns `AgentResult`.

### Streaming (`_stream`)

The streaming loop uses `agent.astream(stream_mode=["updates","messages"], subgraphs=True, version="v2")`:

- **"updates" chunks**: tool call starts, subagent delegations, tool results — used to update `ToolErrorGuard` counters and emit progress events.
- **"messages" chunks**: AIMessage token fragments — forwarded directly to `stream_callback` without intermediate buffering.

`ToolErrorGuard` tracks tool errors:
- `max_consecutive=5`: if the same tool fails 5 times in a row → `AgentExecutionError`
- `max_total=8`: if 8 total tool errors accumulate across any tools → `AgentExecutionError`

The higher total limit gives the agent diagnostic headroom: the system prompt instructs the model to re-read SKILL.md troubleshooting sections after repeated errors, which requires additional tool calls.

`compute_reply_text()` selects the final reply from the message list after streaming ends: it prefers the last AIMessage whose content is non-empty and not a tool-call request.

### Diagnostic Injection

When `AgentExecutionError` is caught by `_handle_envelope`, `AgentExecutor.inject_diagnostic_message()` appends an `AIMessage` with a diagnostic trace to the LangGraph state via `aupdate_state()`. This ensures the checkpoint contains a record of the failure for Forgeron's skill editor.

### Session Compaction (`compact_session`)

Triggered by `/compact` via `relais:atelier:control`:

1. Reads current checkpoint via `agent.aget_state(config)`.
2. Passes old messages to `_DeepAgentsSummarizationMiddleware` (LangGraph built-in).
3. Writes a `_summarization_event` state update, replacing old messages with a compact summary.
4. Returns `CompactResult(messages_before, messages_after, cutoff_index)`.

Compaction reduces checkpoint size without losing the session's semantic content.

---

## System Prompt Assembly (SoulAssembler)

### Fixed core identity — `atelier/SYSTEM_PROMPT.md`

`atelier/SYSTEM_PROMPT.md` is the non-user-editable RELAIS core identity prompt. It defines agent identity, long-term memory instructions, self-diagnosis behaviour on tool errors, diagnostic awareness, execution context block handling, and operational constraints. This file is read once at startup by `_build_core_system_prompt()` (cached) and passed as `system_prompt=` to `create_deep_agent()`.

### User-editable layers — SoulAssembler

`SoulAssembler` (`atelier/soul_assembler.py`) resolves up to 4 user-editable prompt file paths. It **does not read file contents** — file reading is delegated to DeepAgents via `memory=`. The validated paths are returned as `AssemblyResult.memory_paths: list[str]` and passed as `memory=` to `create_deep_agent()`.

| Layer | Source | Required |
|---|---|---|
| 1 — Core soul | `{prompts_dir}/soul/SOUL.md` (hardcoded path) | Yes — WARNING logged if missing |
| 2 — Role overlay | `role_prompt_path` field in `portail.yaml` → `roles[*].prompt_path` | No |
| 3 — Per-user override | `user_prompt_path` field in `portail.yaml` → `users[*].prompt_path` | No |
| 4 — Channel formatting | `channel_prompt_path` from `aiguilleur.yaml` per channel, stamped into `context["aiguilleur"]["channel_prompt_path"]` by Aiguilleur | No |

All paths for layers 2–4 are **explicit relative paths** configured in YAML — nothing is inferred from role names, channel names, or naming conventions. A path that is absolute or escapes `prompts_dir` is rejected with a WARNING and excluded. The `AssemblyResult.is_degraded` flag signals the caller when any layer could not be validated.

---

## Tool Policy (ToolPolicy)

`ToolPolicy` (`atelier/tool_policy.py`) enforces role-based access to skills and MCP tools.

### Skill Resolution

`resolve_skill_dirs(role)` returns the ordered list of skill directories available for the role, scanning in priority order:

1. Role-specific skill dirs from `atelier.yaml` (per-role `skills:` list)
2. Common skill dirs (available to all roles)
3. Bundle skill dirs (`~/.relais/bundles/*/skills/`)

Skill directories are passed as `skills=` to `create_deep_agent()`. DeepAgents exposes them via built-in `list_skills` and `read_skill` tools.

### MCP Tool Filtering

`filter_mcp_tools(tools, role)` applies fnmatch patterns from the role's `mcp_tools_allowed` list in `atelier.yaml`. Tools whose names do not match any pattern are excluded. An empty `mcp_tools_allowed` list means no MCP tools.

---

## Subagent Registry (SubagentRegistry)

`SubagentRegistry` implements a 3-tier discovery hierarchy with first-match-wins semantics:

| Tier | Location | Priority |
|---|---|---|
| 1 — User subagents | `~/.relais/config/atelier/subagents/{name}/` | Highest |
| 2 — Native subagents | `atelier/subagents/{name}/` | Medium |
| 3 — Bundle subagents | `~/.relais/bundles/{bundle}/subagents/{name}/` | Lowest |

Each subagent directory contains a `subagent.yaml` with:

```yaml
name: my-agent
description: "..."
system_prompt: "..."
tools:
  - read_file
  - write_file
  - mcp:git_*       # fnmatch filter on MCP tools
  - inherit         # all MCP tools the parent agent received
  - module:atelier.tools.my_module
```

`SubagentRegistry.get_allowed(role, allowed_patterns)` filters by the role's `allowed_subagents` fnmatch list from `portail.yaml`. The registry reloads atomically when any watched directory changes.

---

## MCP Session Manager (McpSessionManager)

`McpSessionManager` (`atelier/mcp_session_manager.py`) is a singleton started once during Atelier's lifespan and shared across all requests.

### Lifecycle

```
Startup: McpSessionManager.start()
  └─ For each enabled server in mcp_servers.yaml:
       spawn stdio subprocess OR connect SSE client
       store session handle

Per-request: make_mcp_tools(servers)
  └─ langchain-mcp-adapters wraps MCP sessions as BaseTool instances

Per-server lock: asyncio.Lock per server
  └─ Serializes concurrent stdio pipe calls to the same subprocess

Dead session eviction:
  └─ BrokenPipeError / ConnectionError / EOFError → evict session
     → re-establish on next call

Hot-reload: _restart_mcp_sessions() (atomic under _mcp_lock)
  └─ Triggered when mcp_servers.yaml changes
     On failure: degrades to empty tool list (logged, not raised)

Shutdown: McpSessionManager.close()
  └─ Terminates all subprocess sessions gracefully
```

### Per-profile constraints

| Field | Location | Description |
|---|---|---|
| `mcp_timeout` | `profiles.yaml` | Seconds before a single MCP tool call is cancelled (returns error string; loop continues) |
| `mcp_max_tools` | `profiles.yaml` | Maximum MCP tool definitions passed to the model (`0` = no MCP tools) |

Internal tools (file read/write, skill listing) are never capped by `mcp_max_tools`.

---

## Error Synthesis (ErrorSynthesizer)

When `AgentExecutionError` propagates from `AgentExecutor.execute()`, `ErrorSynthesizer` (`atelier/error_synthesizer.py`) makes a lightweight, non-retried LLM call to produce a user-facing error message. The call uses the `fast` profile and receives:

- The exception message
- The partial `messages_raw` from `exc.messages_raw` (the conversation state at the point of failure)

The synthesized reply is published to `relais:messages:outgoing_pending`. The original envelope is then routed to `relais:tasks:failed` (DLQ), and ACK is issued.

This ensures the user always receives a meaningful reply even when the agent loop aborts.

---

## Streaming Architecture

When `context["aiguilleur"]["streaming"]` is `True`, Atelier publishes tokens to a dedicated stream instead of (or in addition to) the final reply on `relais:messages:outgoing_pending`.

### Token stream

```
Key: relais:messages:streaming:{channel}:{correlation_id}
Format per entry: {"token": "...", "done": false}
Final entry:      {"token": "",    "done": true}
TTL: set by the adapter consumer (typically 30s after done=true)
```

Tokens are forwarded directly to `stream_callback` as they arrive, without intermediate buffering. Each token triggers one `XADD` call to the Redis stream.

`DisplayConfig` (populated from `aiguilleur.yaml`) controls whether progress events (tool call names, subagent starts) are emitted alongside tokens, or whether only the final reply is published (`final_only=True`).

---

## Skill Trace Publishing

After every completed agent turn that used at least one skill and made at least one tool call, Atelier publishes a `relais:skill:trace` envelope (fire-and-forget). The `context["skill_trace"]` block carries:

| Field | Type | Description |
|---|---|---|
| `skill_names` | `list[str]` | All skill directory names used in the turn |
| `tool_call_count` | `int` | Total number of tool calls made |
| `tool_error_count` | `int` | Number of tool errors; `-1` means the turn was aborted by `ToolErrorGuard` |
| `messages_raw` | `list[dict]` | Full serialised LangChain message list for the turn |
| `skill_paths` | `dict[str, str]` | Absolute directory path per skill (set for bundle skills) |

Forgeron consumes this trace for autonomous skill editing. The `-1` sentinel is set when `ToolErrorGuard` aborts the loop; it signals Forgeron that the full message list captures both the failure and the agent's (failed) recovery attempt.

---

## Hot-Reload

Atelier supports hot-reload for configuration and subagent files. Changes are detected via two mechanisms:

### File system (watchfiles)

`_config_watch_paths()` returns the list of watched paths:

- `profiles.yaml` — LLM profile configuration
- `mcp_servers.yaml` — MCP server definitions
- `atelier.yaml` — tool policy, subagent patterns
- `~/.relais/config/atelier/subagents/` — user subagents
- `atelier/subagents/` — native subagents
- `~/.relais/bundles/` — bundle subagents, skills, tools

When a change is detected, `safe_reload()` is called: it loads the new configuration, validates it, and replaces the live objects atomically. If MCP server configuration changed, `_restart_mcp_sessions()` replaces the singleton under `_mcp_lock`.

### Redis Pub/Sub (`relais:config:reload:atelier`)

Any brick can trigger a reload by publishing to `relais:config:reload:atelier`. The control consumer processes these alongside `relais:atelier:control` messages.

---

## Data Model

Atelier itself holds no SQLite database. Persistent state is distributed across:

- **AsyncSqliteSaver** (`~/.relais/storage/checkpoints.db`): LangGraph conversation checkpoints, keyed by `thread_id="{user_id}:{session_id}"`.
- **Souvenir** (`~/.relais/storage/memory.db`): Full turn archive via `relais:memory:request`.
- **Redis** (`relais:context:{session_id}`): Short-term context blobs (managed by Souvenir on archive action).

---

## Redis Keys

| Key / Stream | Direction | Purpose |
|---|---|---|
| `relais:tasks` | Consume | Incoming user task envelopes |
| `relais:atelier:control` | Consume | Control operations (compact, reload) |
| `relais:messages:outgoing_pending` | Produce | Final reply → Sentinelle outgoing path |
| `relais:messages:streaming:{ch}:{id}` | Produce | Token-by-token stream for streaming adapters |
| `relais:memory:request` | Produce | Archive turn to Souvenir (ACTION_MEMORY_ARCHIVE) |
| `relais:skill:trace` | Produce | Skill trace to Forgeron (fire-and-forget) |
| `relais:tasks:failed` | Produce | DLQ for exhausted-retry and error-synthesized envelopes |
| `relais:logs` | Produce | Operational log events |

---

## Configuration Reference (`profiles.yaml` and `atelier.yaml`)

### `profiles.yaml` — LLM profiles

```yaml
profiles:
  default:
    model: "anthropic:claude-haiku-4-5-20251001"
    temperature: 0.7
    max_tokens: 4096
    max_turns: 20
    mcp_timeout: 30        # seconds per MCP tool call
    mcp_max_tools: 20      # max MCP tool definitions passed to model (0 = none)
    resilience:
      retry_attempts: 3
      retry_delays: [5, 15, 30]

  precise:
    model: "anthropic:claude-sonnet-4-6"
    temperature: 0.3
    max_tokens: 8192
    max_turns: 30
    mcp_timeout: 60
    mcp_max_tools: 40
    resilience:
      retry_attempts: 2
      retry_delays: [10, 30]
```

### `atelier.yaml` — Tool policy and subagent access

```yaml
atelier:
  roles:
    admin:
      skills:
        - admin-tools
        - shell-scripts
      mcp_tools_allowed:
        - "*"              # all MCP tools
      allowed_subagents:
        - "*"              # all subagents

    user:
      skills:
        - web-search
        - calendar
      mcp_tools_allowed:
        - "brave_search"
        - "gcal_*"
      allowed_subagents:
        - "horloger-manager"
        - "relais-config"
        - "general-purpose"
```

---

## Key Classes

| Class | File | Responsibility |
|---|---|---|
| `Atelier` | `atelier/main.py` | `BrickBase` subclass; owns both consumer loops, coordinates all subsystems, implements 9-step `_handle_envelope` |
| `AgentExecutor` | `atelier/agent_executor.py` | LangGraph agent wrapper; retry loop, streaming, tool error guard, session compaction |
| `AgentResult` | `atelier/agent_executor.py` | Frozen dataclass: `reply_text`, `messages_raw`, `tool_call_count`, `tool_error_count`, `subagent_traces` |
| `SubagentTrace` | `atelier/agent_executor.py` | Frozen dataclass: per-delegated-subagent metrics captured by `SubagentMessageCapture` |
| `CompactResult` | `atelier/agent_executor.py` | Frozen dataclass: `messages_before`, `messages_after`, `cutoff_index` (session compaction output) |
| `ToolErrorGuard` | `atelier/agent_executor.py` | Tracks consecutive and total tool errors; raises `AgentExecutionError` at thresholds |
| `SubagentMessageCapture` | `atelier/agent_executor.py` | LangChain callback handler that captures per-subagent metrics into `SubagentTrace` instances |
| `SoulAssembler` | `atelier/soul_assembler.py` | Validates 4-layer prompt file paths; returns `memory_paths: list[str]` for `create_deep_agent(memory=)` |
| `ToolPolicy` | `atelier/tool_policy.py` | Resolves skill directories and filters MCP tools per role |
| `SubagentRegistry` | `atelier/subagent_registry.py` | 3-tier subagent discovery (user config > native > bundles); hot-reload; fnmatch role filtering |
| `McpSessionManager` | `atelier/mcp_session_manager.py` | Singleton managing all MCP server sessions; per-server locks; dead-session eviction |
| `ErrorSynthesizer` | `atelier/error_synthesizer.py` | Lightweight LLM call producing empathetic user-facing error replies |
| `SouvenirBackend` | `atelier/souvenir_backend.py` | `BackendProtocol` implementation routing `/memories/` paths to `relais:memory:request` |
| `ProfileConfig` | `common/profile_loader.py` | Dataclass loaded from `profiles.yaml` via the config cascade |

---

## Important Design Decisions

### Why `ack_mode="on_success"`?

Atelier is the only brick where message loss is unacceptable — a lost task means a user message is silently dropped. `ack_mode="on_success"` ensures that transient LLM backend failures cause the envelope to remain in the PEL and be re-delivered after the backend recovers. The XACK pattern is strict: ACK only after a successful publish to `relais:messages:outgoing_pending` or a final route to the DLQ.

### Why a singleton McpSessionManager?

MCP stdio servers are subprocesses. Spawning a fresh subprocess per request would add hundreds of milliseconds of startup latency and leave orphaned processes if Atelier crashes mid-request. The singleton starts once, keeps sessions warm, and uses per-server locks to serialise concurrent pipe calls without blocking the asyncio event loop.

### Why ToolErrorGuard with two counters?

A single consecutive counter would reset after a successful tool call, allowing an adversarial or misconfigured skill to produce many errors across multiple tools without triggering the guard. A single total counter would abort after the first tool's errors, preventing the agent from diagnosing the root cause using other tools. The two-counter design gives the agent room to self-diagnose (re-reading SKILL.md, trying alternative tools) while still hard-stopping runaway loops.

### Why emit skill traces even on successful turns?

Successful turns with many tool calls are the best evidence for the `edit_call_threshold` path in Forgeron: they show how a skill is actually used in production, enabling Forgeron to enrich SKILL.md with real examples. Error-free turns that reach the threshold threshold are worth editing proactively, before errors accumulate.

### Why the 4-layer soul assembly with explicit paths?

The 4-layer design reflects four axes of personalisation: personality (soul), role, individual user, and channel formatting. Separating them into files means each can be updated independently — a channel formatting overlay does not require editing the core soul file, and a per-user tone override does not affect other users on the same role. Using explicit configured paths (rather than inferring paths from role names or channel names) means a prompt file can be shared across multiple roles or channels without renaming it, and operators have full control over which files are loaded.

### Why `SouvenirBackend` at `/memories/` paths?

The `CompositeBackend` pattern allows the agent to use familiar file-like tool calls (`read_file /memories/todo_list`, `write_file /memories/todo_list "..."`) to interact with Souvenir's persistent memory files, without the agent needing to know about Redis Streams or the memory protocol. The backend intercepts these path-prefixed calls and translates them to `ACTION_MEMORY_FILE_*` envelopes, keeping the abstraction transparent to the LLM.
