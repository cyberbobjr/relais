# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

RELAIS is a **micro-brick autonomous AI assistant architecture** using Redis Streams for inter-service communication. It's structured as a modular pipeline where specialized services ("bricks") process messages asynchronously through a message bus. The system supports multiple channels (Discord, Telegram, etc.) and implements resilient LLM integrations with exponential backoff retry logic.

## Project Structure

### Core Bricks (Async Pipeline)

The main pipeline flows through these bricks in order:

1. **Aiguilleur** (`aiguilleur/`) - Unified configurable channel relay manager
   - Single process (`aiguilleur/main.py`) manages all channel adapters
   - `AiguilleurManager` loads channels from `aiguilleur.yaml` (enabled/disabled, type, restart policy)
   - `NativeAiguilleur` (thread + asyncio.run) for Python adapters (e.g., DiscordAiguilleur)
   - `ExternalAiguilleur` (subprocess.Popen) for non-Python adapters
   - Automatic restart with exponential backoff: `min(2^restart_count, 30)` seconds, max 5 restarts per channel
   - Adapter discovery by convention: `aiguilleur.channels.{name}.adapter` (looks for a class ending in `*Aiguilleur`), or `class_path` override
   - **Profile stamping**: each adapter stamps `context["aiguilleur"]["channel_profile"]` from `ChannelConfig.profile` (aiguilleur.yaml) → `get_default_llm_profile()` (config.yaml:llm.default_profile) → `"default"`
   - Produces: `relais:messages:incoming:{channel}`
   - Bridges external APIs to Redis Streams

2. **Portail** (`portail/`) - Consumer enriching message context
   - Consumes: `relais:messages:incoming`
   - Validates Envelope format, resolves user from `UserRegistry` (portail.yaml), applies reply_policy (vacation/in_meeting)
   - Stamps into `context["portail"]`: `user_id` (YAML key, e.g. `"usr_admin"` — stable cross-channel), `user_record` (full dict), `llm_profile` (from `channel_profile` or `"default"`)
   - Produces: `relais:security`

3. **Sentinelle** (`sentinelle/`) - Bidirectional security checkpoint
   - **Incoming**: Consumes `relais:security`, ACL validation (sentinelle.yaml), then bifurcates:
     - Slash command (`/cmd`): checks KNOWN_COMMANDS + command-level ACL (`action=cmd_name`) → routes to `relais:commands` or sends inline rejection reply
     - Normal message: produces `relais:tasks` (or drops silently if ACL fails)
   - **Outgoing**: Consumes `relais:messages:outgoing_pending` (single shared stream), applies outgoing guardrails, produces `relais:messages:outgoing:{channel}`

4. **Atelier** (`atelier/`) - Transformer executing LLM calls via `deepagents.create_deep_agent()`
   - Consumes: `relais:tasks`
   - Resolves memory paths via `SoulAssembler` (soul/SOUL.md + role/user/channel overlays as `memory=` to `create_deep_agent()`); executes agentic loop via `AgentExecutor` (`atelier/agent_executor.py`)
   - **System prompt**: fixed core identity loaded from `atelier/SYSTEM_PROMPT.md` (non-user-editable: agent identity, long-term memory instructions, self-diagnosis on tool errors); user-editable personality layers passed separately via `memory=`
   - Tool access controlled by `ToolPolicy` (`atelier/tool_policy.py`); skill dirs resolved per-role and passed as `skills=` to `create_deep_agent()`
   - MCP tools via `langchain-mcp-adapters` (`make_mcp_tools()` in `atelier/mcp_adapter.py`); lifecycle managed by singleton `McpSessionManager` (started once at brick startup, shared across requests; per-server locks; dead-session eviction)
   - Handles `AgentExecutionError` → synthesizes user-visible error reply via `ErrorSynthesizer` (`atelier/error_synthesizer.py`) → publishes to `relais:messages:outgoing_pending` → routes original to DLQ (`relais:tasks:failed`)
   - **Always streaming**: Atelier always streams token-by-token regardless of channel; publishes start signal to `relais:streaming:start:{channel}` (Pub/Sub) so adapters know when to begin consuming the stream; adapters buffer or forward tokens as appropriate for their channel
   - Streams output token-by-token to `relais:messages:streaming:{channel}:{correlation_id}` via `agent.astream(stream_mode="messages")`
   - **User context**: reads `user_role` and `display_name` from `context["portail"]["user_record"]` (stamped upstream by Portail) to select role-based prompt layer
   - **Execution context block**: `AgentExecutor._build_execution_context()` prepends a `<relais_execution_context>` block to the first user message on every agent turn, carrying `sender_id`, `channel`, `session_id`, `correlation_id` and `reply_to` extracted from the envelope. Skills (notably `channel-setup` for WhatsApp pairing) can read this metadata directly from the conversation state; the system prompt instructs the model NOT to echo it back to the user.
   - **LLM profile resolution**: reads `context["portail"].get("llm_profile", "default")` (stamped by Portail) to load the appropriate `ProfileConfig` from `atelier/profiles.yaml` (via `common/profile_loader.py`)
   - **Subagent registry**: 2-tier subagent architecture — `SubagentRegistry.load()` scans user subagents in `$RELAIS_HOME/config/atelier/subagents/` first, then native subagents in `atelier/subagents/` (bundled with source). User subagents take priority (first-match-wins by name). User access controlled by `allowed_subagents` in portail.yaml roles (fnmatch patterns). Native subagents shipped: `relais-config` (configuration CRUD), `horloger-manager` (job YAML CRUD), `general-purpose` (worker subagent overriding deepagents' built-in — strict no-user-facing-output contract). Tool tokens support `mcp:<glob>` (MCP pool filter), `inherit` (all request_tools), or `<static_name>` (ToolRegistry lookup). Hot-reload swaps the registry atomically when either tier's directory changes.
   - **`/compact` command**: Commandant publishes a control envelope to `relais:atelier:control` (consumer group `atelier_control_group`). Atelier's `_handle_control()` calls `AgentExecutor.compact_session()` which summarizes the oldest messages via `_DeepAgentsSummarizationMiddleware`, replaces them with a single `AIMessage` summary by writing a `_summarization_event` to the LangGraph checkpointer state, and publishes a confirmation reply. `compact_keep` (default `6`) is configured per-profile in `profiles.yaml`. See `docs/REDIS_BUS_API.md` → `relais:atelier:control` for the full wire format.
   - Produces: `relais:messages:outgoing_pending` (→ consumed by Sentinelle outgoing loop) + `relais:memory:request` (archive action with full message history for Souvenir)

5. **Souvenir** (`souvenir/`) - Consumer managing short/long-term memory and user facts
   - Single-stream consumer: `relais:memory:request` for archive/clear/file_*/history_read/sessions/resume actions
   - Archive action: Atelier publishes completed turns with full `messages_raw` list (serialized LangChain messages); Souvenir persists to SQLite
   - Short-term: Redis List `relais:context:{session_id}` (max 20 turn blobs, each blob = full serialized LangChain message list for one turn, TTL 24h)
   - Long-term: SQLite `~/.relais/storage/memory.db`; one row per turn (upsert on `correlation_id`), fields `user_content`, `assistant_content`, `messages_raw` JSON
   - Handlers: `ArchiveHandler` (persist turn), `ClearHandler`, `FileWriteHandler`, `FileReadHandler`, `FileListHandler`, `SessionsHandler`, `ResumeHandler`, `HistoryReadHandler` — no LLM calls inside Souvenir (memory extraction removed)
   - `HistoryReadHandler`: reads full session history from SQLite, truncates by token budget (~4 chars/token), publishes JSON array to `relais:memory:response:{correlation_id}` (Redis List, TTL 60s) for Forgeron to retrieve via `BRPOP` during correction pipeline

6. **Horloger** (`horloger/`) - Producer-only CRON scheduler
   - **Producer-only** brick: `stream_specs()` returns `[]`; BrickBase blocks on `shutdown_event` so background tick-loop tasks remain alive
   - Reads job specs from `~/.relais/config/horloger/jobs/*.yaml` (one YAML file per job); watchfiles hot-reload
   - On every tick (`tick_interval_seconds`, default 30s): reload registry → `Scheduler.get_due_jobs()` → publish trigger envelopes → record execution trace in SQLite
   - **Virtual channel pattern**: publishes to `relais:messages:incoming:horloger` — envelope traverses the full pipeline (Portail → Sentinelle → Atelier) like a real user message
   - **Impersonation**: `sender_id = f"horloger:{job.owner_id}"` so Portail/Sentinelle apply the correct ACL; `context["portail"]` pre-stamped (`user_id`, `llm_profile`) to bypass UserRegistry channel lookup (`"horloger"` is not a real channel in `portail.yaml`)
   - **`context["aiguilleur"]["reply_to"]`** set to `job.channel` so Sentinelle routes the reply to the correct output channel
   - Catch-up guard: jobs whose last scheduled time is older than `catch_up_window_seconds` (default 120s) are skipped, not bulk-triggered, to avoid storms after downtime
   - Execution trace: SQLite `~/.relais/storage/horloger.db` via SQLModel + aiosqlite (same pattern as Souvenir); one row per firing (`triggered` / `publish_failed` / `skipped_catchup` / `skipped_disabled` / `skipped_double_fire`)
   - **`horloger-manager` native subagent**: handles CRUD of job YAML files; accessible via `/horloger` or `/schedule` slash commands; uses `tool_tokens: [read_file, write_file, list_directory]`
   - Config: `config/horloger.yaml.default` — `tick_interval_seconds`, `catch_up_window_seconds`, `jobs_dir`, `db_path`
   - Produces: `relais:messages:incoming:horloger` (trigger envelopes) + `relais:logs`

### Observer & Support Services

- **Archiviste** (`archiviste/`) - Observer logging all events
  - Consumes: `relais:logs`, `relais:events:system`, `relais:events:messages`
  - Writes JSONL files + SQLite audit, never rejects messages

- **Aiguilleur Relays** - Discord, Telegram, Slack connectors
  - Consume: `relais:messages:outgoing:{channel}`
  - Send messages back to external APIs

### Configuration & Utilities

- **common/** - Shared utilities
  - `envelope.py`: Message wrapper with header (content, sender_id, channel, session_id, correlation_id, timestamp, action, traces) + namespaced context
  - `envelope_actions.py`: Canonical ACTION_* constants for first-class action field
  - `contexts.py`: Namespace constants (CTX_*) and TypedDicts for per-brick contexts
  - `redis_client.py`: Async Redis factory with per-brick ACL (password per service)
  - `config_loader.py`: YAML config cascade (user > system > project)
  - `user_registry.py`: UserRegistry and UserRecord for user resolution from portail.yaml; REST API keys stored as HMAC-SHA256 hashes (`_hash_api_key()`, salt from `RELAIS_API_KEY_SALT` env var)
  - `user_record.py`: UserRecord dataclass
  - `streams.py`: Canonical Redis stream name constants (`STREAM_*`) and helpers (`stream_outgoing(channel)`, `stream_outgoing_user(channel, user_id)`, `stream_streaming(channel, corr_id)`, `key_active_sessions(sender_id)`, `stream_config_reload(brick)`); all bricks import stream names from here

- **config/** - YAML configuration files
  - `config.yaml`: Redis socket, LiteLLM URL, logging, security settings
  - `aiguilleur.yaml`: Channel definitions (enabled/disabled, streaming flag, type, class_path, max_restarts)
  - `litellm.yaml`: Model definitions, router settings, master key
  - `profiles.yaml`: LLM profiles (default/fast/precise/coder) with temp, max_tokens, retry delays
  - `mcp_servers.yaml`: MCP stdio server definitions for Atelier (command, args, env per server)
  - `portail.yaml.default`: User registry with display_name, role, channels, allowed_subagents (fnmatch patterns)
  - `sentinelle.yaml.default`: ACL for sentinelle brick
  - `redis.conf`: Redis ACL definitions per brick, stream permissions

- **prompts/** - Multi-layer system prompt (assembled by `atelier/soul_assembler.py`)
  - `soul/SOUL.md.default`: Core personality — Layer 1, always loaded
  - `soul/variants/`: Personality variants (manual swap, not auto-loaded)
  - `channels/{channel}_default.md`: Channel formatting overlay — Layer 4 (e.g. `telegram_default.md`)
  - `policies/{policy}.md`: Reply-policy overlay — Layer 5 (e.g. `in_meeting.md`, `vacation.md`)
  - `roles/{role}.md`: Role overlay — Layer 2 (create as needed, not shipped)
  - `users/{channel}_{id}.md`: Per-user override — Layer 3 (create as needed, not shipped)

## Key Architecture Concepts

### Redis Streams & Consumer Groups

- **At-least-once delivery**: Consumer groups with PEL (Pending Entry List) and XACK acknowledgment
- **Stream naming**: `relais:messages:incoming`, `relais:security`, `relais:tasks`, `relais:commands`, `relais:messages:outgoing_pending`, `relais:messages:outgoing:{channel}`, `relais:memory:*`
- **Initialization**: Each brick creates its consumer group on startup (idempotent)
- **Resilience**: Failed messages left in PEL are automatically re-delivered; poison pills sent to DLQ

### Envelope Pattern

All messages use a standardized `Envelope` dataclass:
```python
@dataclass
class Envelope:
    content: str
    sender_id: str       # "discord:{user_id}", "telegram:{chat_id}"
    channel: str
    session_id: str
    correlation_id: str  # UUID for request tracking
    timestamp: float     # epoch seconds
    action: str          # self-describing intent token (see common/envelope_actions.py)
    traces: list[dict]   # ordered pipeline step records: [{brick, action, timestamp}]
    context: dict        # namespaced per-brick sub-dicts (see common/contexts.py)
    media_refs: list[MediaRef]
```

Each brick writes into its own `context` namespace and must not mutate other namespaces:
- `context["aiguilleur"]` — `channel_profile`, `channel_prompt_path`, `reply_to`, `content_type`
- `context["portail"]` — `user_id`, `user_record`, `llm_profile`, `session_start`
- `context["sentinelle"]` — `acl_passed`, `acl_role`, `outgoing_checked`
- `context["atelier"]` — `streamed`, `user_message`, `progress_event`, `progress_detail`
- `context["souvenir_request"]` — request parameters for memory actions (`session_id`, `user_id`, etc.)

Namespace constants are in `common/contexts.py` (`CTX_AIGUILLEUR`, `CTX_PORTAIL`, …). Use `ensure_ctx(envelope, key)` to get-or-create a namespace sub-dict safely.

Bricks use:
- `Envelope.from_json()` to deserialize from Redis
- `Envelope.from_parent()` to derive child envelopes (deep-copies `traces` and `context`, clears `action`)
- `Envelope.create_response_to()` to build a reply envelope (also clears `action`)
- `Envelope.add_trace(brick, action)` to record pipeline progression
- `Envelope.to_json()` to serialize for Redis

**Action is mandatory at serialization**: `Envelope.to_json()` raises `ValueError` if `envelope.action` is empty. Because `from_parent()` and `create_response_to()` intentionally clear the parent action, every producing site must set the target action explicitly before calling `xadd` — e.g. `response.action = ACTION_MESSAGE_OUTGOING_PENDING`. This prevents enveloppes without a declared intent from traversing the pipeline.

### Error Handling & Resilience

**Atelier (LLM caller)** implements resilient retry with conditional XACK:
```
On exception:
  - RETRIABLE (ConnectError, TimeoutException): Do NOT ACK → stays in PEL, re-delivered
  - AgentExecutionError: Synthesize error reply via ErrorSynthesizer → publish to outgoing_pending → route to DLQ, then ACK
  - ExhaustedRetriesError: Route to DLQ relais:tasks:failed, then ACK (avoid poisoning PEL)

XACK pattern (critical):
  - Only ACK after successful publish to output stream OR final error to DLQ
  - Never ACK on transient errors (no ACK = message stays in PEL for re-delivery)
  - This prevents silent message loss if the LLM backend restarts during processing
```

### Configuration Cascade

Priority order (highest to lowest):
1. `~/.relais/config/` - User overrides (also serves as dev mode when `RELAIS_HOME` is unset, defaulting to `<project_root>/.relais`)

Environment variables override YAML configs (e.g., `REDIS_SOCKET_PATH`, `REDIS_PASSWORD`, per-brick `REDIS_PASS_*`)

### Orchestration (supervisord)

Priority-based startup (`supervisord.conf`):
- **Priority 1**: `courier` (Redis server)
- **Priority 5**: `baileys-api` (WhatsApp gateway, autostart=false)
- **Priority 8**: `archiviste` (observer, non-blocking)
- **Priority 10**: Core bricks (portail, sentinelle, atelier, souvenir, forgeron, commandant)
- **Priority 20**: `aiguilleur` (unified channel manager)

All processes log to `~/.relais/logs/` via supervisord stdout_logfile.

## Development

### Build & Dependencies

Uses **Poetry** with `pyproject.toml`:
```bash
# Install dependencies
poetry install

# Or use uv (faster):
uv sync
```

Core dependencies: `redis >=5.0`, `deepagents`, `langchain-mcp-adapters`, `mcp >=1.0.0`, `supervisor >=4.2`, `pydantic >=2.9`
Dev: `pytest >=9.0`, `pytest-asyncio >=1.3`

Note: Atelier uses `deepagents.create_deep_agent()` for the agentic loop; the `profiles.yaml` model field uses `provider:model-id` format (e.g., `anthropic:claude-sonnet-4-6`).

### Running Services

**Single brick** (for development):
```bash
# Aiguilleur (unified channel manager — manages all adapters)
PYTHONPATH=. uv run python aiguilleur/main.py

# Portail consumer
PYTHONPATH=. uv run python portail/main.py

# Atelier transformer
PYTHONPATH=. uv run python atelier/main.py

# Souvenir memory service
PYTHONPATH=. uv run python souvenir/main.py
```

**Full system** (via supervisord wrapper):
```bash
./supervisor.sh start all              # Start all services
./supervisor.sh --verbose start all    # Start + tail all logs (Ctrl+C to detach)
./supervisor.sh status                 # Check status
./supervisor.sh restart all            # Restart
./supervisor.sh stop all               # Stop all services
```

Or direct supervisord:
```bash
supervisord -c supervisord.conf
supervisorctl -c supervisord.conf status
supervisorctl tail {service} -f  # Follow logs
```

### Testing

Uses **pytest** with async support:
```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=common --cov=portail --cov=atelier --cov=souvenir --cov-report=term-missing

# Run specific test
pytest tests/test_atelier.py::test_handle_message -v

# Mark-based selection
pytest -m unit  # Unit tests only
pytest -m integration  # Integration tests
```

Test organization:
- `tests/test_*.py` - Unit & integration tests
- Use `@pytest.mark.unit`, `@pytest.mark.integration` for categorization
- Use `pytest-asyncio` for async test fixtures

**CRITICAL — E2E tests:**
- `tests/test_smoke_e2e.py` is marked `@pytest.mark.skip` — it is **never** run automatically
- To run manually: `pytest tests/test_smoke_e2e.py -v` (explicit skip override not needed, just run the file directly)
- **Never** include `test_smoke_e2e.py` in automated test loops or retry loops
- **Never** run `pytest tests/` without `-x --timeout=30`; if a test times out, stop and report to the user instead of retrying

### Linting & Type Checking

```bash
# Lint with ruff
ruff check .

# Format with black
black .

# Sort imports with isort
isort .

# Type checking with mypy or pyright
mypy .
```

### Configuration Setup

Create `.env` from `.env.example`:
```bash
cp .env.example .env
```

Set required keys:
- `ANTHROPIC_API_KEY` - Anthropic API key (used directly by LangChain `init_chat_model`)
- `REDIS_SOCKET_PATH` - Redis Unix socket path
- Channel bot tokens: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, etc.
- Per-brick Redis passwords: `REDIS_PASS_PORTAIL`, `REDIS_PASS_ATELIER`, etc.

## Atelier — Outils & Serveurs MCP

### Tools (LangChain `BaseTool`)

All tools exposed to the agentic loop are `langchain_core.tools.BaseTool` instances. Skill directories are resolved per-role by `ToolPolicy` (`atelier/tool_policy.py`) and passed as `skills=` to `create_deep_agent()` — DeepAgents handles `list_skills`/`read_skill` natively. MCP tools are filtered by `ToolPolicy.filter_mcp_tools()` before being passed to the agent.

### MCP Servers (`mcp_servers.yaml`)

External tools are provided by MCP servers defined in `config/mcp_servers.yaml`. Two transports are supported:

```yaml
mcp_servers:
  global:
    - name: my-stdio-server       # stdio: Atelier spawns a subprocess
      enabled: true
      type: stdio
      command: "uvx"
      args: ["mcp-server-my-server"]
    - name: my-sse-server         # sse: Atelier connects to a running HTTP server
      enabled: true
      type: sse
      url: "http://127.0.0.1:8100"
  contextual: []
```

MCP tools are loaded via `langchain-mcp-adapters` (`make_mcp_tools()` in `atelier/mcp_adapter.py`). The MCP server lifecycle is managed by `McpSessionManager` (`atelier/mcp_session_manager.py`) as a **singleton**: started once at Atelier brick startup via `start()`, shared across all requests, and closed on shutdown via `close()`. Key behaviors:
- Per-server `asyncio.Lock` serializes concurrent stdio pipe calls
- Dead sessions (`BrokenPipeError`, `ConnectionError`, `EOFError`) are evicted and re-established on the next call
- On hot-reload when MCP server config changes, `_restart_mcp_sessions()` atomically replaces the manager; on failure degrades to empty tool list

`mcp_timeout` and `mcp_max_tools` are **not** in `mcp_servers.yaml` — they are per-profile fields in `profiles.yaml`.

### Agentic Loop

`AgentExecutor` (`atelier/agent_executor.py`) manages the full lifecycle using `deepagents.create_deep_agent()`:

```python
executor = AgentExecutor(
    profile=profile,            # ProfileConfig (model in provider:model-id format, max_turns, …)
    soul_prompt=soul_prompt,    # assembled system prompt string
    mcp_servers=mcp_servers,    # dict from load_for_sdk()
    tools=tools,                # list[BaseTool] (MCP tools, filtered by ToolPolicy)
)
reply = await executor.execute(envelope, context, stream_callback=...)
```

Streaming is token-by-token via `agent.astream(stream_mode="messages")`, buffered by `StreamBuffer` (flushes at `STREAM_BUFFER_CHARS` threshold).

Tool error limits are enforced by `ToolErrorGuard` (max 5 consecutive errors per tool, max 8 total errors) — raises `AgentExecutionError` to abort runaway loops.  The higher total limit (8 vs the consecutive limit of 5) gives the agent diagnostic room: the system prompt includes self-diagnosis instructions that tell the agent to re-read SKILL.md troubleshooting sections after encountering repeated errors.  On `AgentExecutionError`, the partial conversation state is captured into `exc.messages_raw` and forwarded to both `ErrorSynthesizer` (user-visible error reply) and `Forgeron` (skill improvement trace with full conversation context).

Per-profile MCP constraints (fields in `ProfileConfig`):
- `mcp_timeout` — seconds before a single MCP tool call is cancelled (returns an error string to the model; loop continues)
- `mcp_max_tools` — max MCP tool definitions passed to the model (`0` = no MCP tools; internal tools are never capped)

## Common Development Tasks

### Adding a New Brick

All bricks inherit from `common.brick_base.BrickBase`. The minimum template:

```python
from common.brick_base import BrickBase, StreamSpec
from common.shutdown import GracefulShutdown  # noqa: F401 — test patch target

class MyBrick(BrickBase):
    def __init__(self) -> None:
        super().__init__("mybrick")
        # load config here

    def _create_shutdown(self) -> GracefulShutdown:
        return GracefulShutdown()  # lets tests patch mybrick.main.GracefulShutdown

    def _load(self) -> None:
        pass  # called by reload_config(); load YAML into self attributes

    def stream_specs(self) -> list[StreamSpec]:
        return [StreamSpec(stream="relais:my:stream", group="mybrick_group",
                           consumer="mybrick_1", handler=self._handle)]

    async def _handle(self, envelope, redis_conn) -> bool:
        ...
        return True  # True = XACK, False = leave in PEL

if __name__ == "__main__":
    asyncio.run(MyBrick().start())
```

Steps:
1. Create `{brick_name}/main.py` with class inheriting `BrickBase`
2. Implement `_load()` and `stream_specs()` (both abstract — required)
3. Override `_create_shutdown()` for test patch compatibility
4. Register in `supervisord.conf` with appropriate priority
5. Add Redis ACL entry in `config/redis.conf` with stream permissions
6. Update `docs/ARCHITECTURE.md` with brick role
7. Add unit tests in `tests/test_{brick_name}.py`

### Adding a New Subagent

#### User Custom Subagents (in `$RELAIS_HOME/config/atelier/subagents/`)

1. Create `$RELAIS_HOME/config/atelier/subagents/{name}/` directory
2. Add `subagent.yaml` with required fields: `name`, `description`, `system_prompt` (and optionally `tools`, `delegation_snippet`)
3. The directory name must exactly match the `name` field in the YAML (e.g., `my-agent/subagent.yaml` → `name: my-agent`)
4. Optional: add `tools/` subdirectory with Python modules exporting BaseTool instances
5. Add the subagent name to relevant roles' `allowed_subagents` in portail.yaml (fnmatch patterns, e.g. `["my-agent"]` or `["my-*"]`)
6. No changes needed to `agent_executor.py` or `main.py` — Atelier picks up new files automatically on hot-reload (or restart)

#### Native Subagents (in `atelier/subagents/`, shipped with source)

Native subagents are bundled in the repository. The registry scans them **after** user subagents, so user subagents take priority by name.

To add a native subagent to the shipped source:
1. Create `atelier/subagents/{name}/` directory with `subagent.yaml` (same structure as user subagents)
2. Add to `common/init.py` `DEFAULT_FILES` if it should be copied to user directory on first initialization
3. Register in `atelier/main.py` `_config_watch_paths()` if you want hot-reload (native subagents are already watched)

Tool token reference for the `tools:` field:
- `mcp:<glob>` — fnmatch filter on per-request MCP tools (e.g. `mcp:git_*`)
- `inherit` — pass all MCP tools the parent agent received (stays within ToolPolicy scope)
- `module:<dotted.path>` — import a Python module and collect all `BaseTool` instances from it (e.g. `module:aiguilleur.channels.whatsapp.tools`); only prefixes in `_ALLOWED_MODULE_PREFIXES` (`aiguilleur.channels.`, `atelier.tools.`, `relais_tools.`) are permitted
- `<name>` — static tool from ToolRegistry (`atelier/tools/*.py` modules)

### Adding a New Bundle

Bundles are ZIP archives that distribute subagents, skills, and tools as a single installable unit.

#### Bundle structure

```
my-bundle.zip
└── my-bundle/           # root folder name must match bundle name
    ├── bundle.yaml      # required manifest
    ├── subagents/       # optional: one dir per subagent (same layout as user subagents)
    │   └── my-agent/
    │       └── subagent.yaml
    ├── skills/          # optional: one dir per skill (skill files directly inside)
    │   └── my-skill/    # MUST be a direct child — no nesting (ToolPolicy scans one level only)
    │       └── SKILL.md
    └── tools/           # optional: .py files exporting BaseTool instances
        └── my_tool.py
```

**`bundle.yaml` required fields:**
```yaml
name: my-bundle        # must match root folder name
description: "..."
version: "1.0.0"
author: "..."
```

#### Installing a bundle

Two entry points:
1. **CLI**: `relais bundle install /path/to/my-bundle.zip`
2. **Slash command**: `/bundle install /path/to/my-bundle.zip` (via chat)

Install destination: `~/.relais/bundles/<bundle-name>/`

#### Uninstalling a bundle

1. **CLI**: `relais bundle uninstall my-bundle`
2. **Slash command**: `/bundle uninstall my-bundle`

Uninstall removes the bundle directory; hot-reload picks up the change automatically.

#### Bundle discovery (Atelier hot-reload)

- **ToolRegistry**: scans `~/.relais/bundles/*/tools/*.py` on reload; tags each tool with `tool._bundle_name`
- **SubagentRegistry**: scans `~/.relais/bundles/*/subagents/` as the 3rd tier (after user config and native)
- **ToolPolicy**: merges `~/.relais/bundles/*/skills/*/` into skill resolution
- Hot-reload is triggered by `watchfiles` watching `~/.relais/bundles/`

#### Security constraints

- ZIP bomb protection: rejects archives with > 50 MB uncompressed content
- Path traversal protection: rejects any ZIP member whose resolved path escapes the target directory
- Bundle subagents are still role-gated via `allowed_subagents` in `portail.yaml`
- Bundle tools enter the global ToolRegistry; name conflicts emit a WARNING and the tool from the bundle that sorts lexicographically last (alphabetically) wins

See `docs/BUNDLES.md` for full format specification and `common/bundles.py` for the implementation.

### Handling Message Errors

- **Validation errors** (Portail): Log and continue (don't block pipeline)
- **Authorization failures** (Sentinelle): Return False to leave in PEL for operator review
- **LLM failures** (Atelier): Retry with backoff, then route to DLQ if exhausted
- **Memory service timeouts** (Atelier): Return False to retry (3.0s timeout per attempt — intentionally short for graceful degradation)

### Debugging Stream State

```bash
# Check Redis Streams
redis-cli -s ~/.relais/redis.sock

> XLEN relais:messages:incoming:discord  # Stream length
> XRANGE relais:messages:incoming:discord - +  # View messages
> XINFO GROUPS relais:messages:incoming:discord  # Consumer groups
> XPENDING relais:messages:incoming:discord portail_group  # PEL (pending messages)
```

### Monitoring Logs

```bash
# Follow all logs
tail -f ~/.relais/logs/supervisord.log

# Follow specific brick
supervisorctl tail portail -f

# JSON event logs (Archiviste)
tail -f ~/.relais/logs/events.jsonl
```

## Key Design Decisions

1. **Async/await everywhere**: All I/O (Redis, HTTP) is non-blocking using asyncio
2. **At-least-once semantics**: Duplicates possible but safer than message loss; idempotency is caller's responsibility
3. **Stateless bricks**: Session state in Redis, not process memory (enables horizontal scaling)
4. **Correlation IDs**: Track requests end-to-end across the pipeline for observability
5. **Envelope immutability**: Frozen dataclasses ensure message integrity in transit
6. **Environment-based config**: Supports local development, systemd, Docker deployment without code changes

## References

- `docs/ARCHITECTURE.md` - Detailed technical architecture, initialization order, flux diagrams
- `docs/CONTRIBUTING.md` - Development setup, testing patterns, brick implementation checklist
- **`docs/REDIS_BUS_API.md`** - **Canonical reference for ALL Redis Streams and Pub/Sub channels** (schemas, consumer groups, XACK contract). Consult this before writing any code that publishes to or consumes from the bus.
- `README.md` - MVP phases, quick start, configuration structure
- `pyproject.toml` - Dependencies, package metadata

## Code Exploration Policy

Always use jCodemunch-MCP tools for code navigation. Never fall back to Read, Grep, Glob, or Bash for code exploration.

**Start any session:**
1. `resolve_repo { "path": "." }` — confirm the project is indexed. If not: `index_folder { "path": "." }`
2. `suggest_queries` — when the repo is unfamiliar

**Finding code:**
- symbol by name → `search_symbols` (add `kind=`, `language=`, `file_pattern=` to narrow)
- string, comment, config value → `search_text` (supports regex, `context_lines`)
- database columns (dbt/SQLMesh) → `search_columns`

**Reading code:**
- before opening any file → `get_file_outline` first
- one or more symbols → `get_symbol_source` (single ID → flat object; array → batch)
- symbol + its imports → `get_context_bundle`
- specific line range only → `get_file_content` (last resort)

**Repo structure:**
- `get_repo_outline` → dirs, languages, symbol counts
- `get_file_tree` → file layout, filter with `path_prefix`

**Relationships & impact:**
- what imports this file → `find_importers`
- where is this name used → `find_references`
- is this identifier used anywhere → `check_references`
- file dependency graph → `get_dependency_graph`
- what breaks if I change X → `get_blast_radius` (add `include_depth_scores=true` for layered risk)
- what symbols actually changed since last commit → `get_changed_symbols`
- find unreachable/dead code → `find_dead_code`
- most important symbols by architecture → `get_symbol_importance`
- is the index current → `check_freshness`
- class hierarchy → `get_class_hierarchy`
- related symbols → `get_related_symbols`
- diff two snapshots → `get_symbol_diff`

**Retrieval with token budget:**
- best-fit context for a task → `get_ranked_context` (query + token_budget)
- bounded symbol bundle → `get_context_bundle` (add token_budget= to cap size)

**After editing a file:** `index_file { "path": "/abs/path/to/file" }` to keep the index fresh.

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (90-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk vitest run          # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%)
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
