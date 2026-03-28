# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

RELAIS is a **micro-brick autonomous AI assistant architecture** using Redis Streams for inter-service communication. It's structured as a modular pipeline where specialized services ("bricks") process messages asynchronously through a message bus. The system supports multiple channels (Discord, Telegram, etc.) and implements resilient LLM integrations with exponential backoff retry logic.

## Project Structure

### Core Bricks (Async Pipeline)

The main pipeline flows through these bricks in order:

1. **Aiguilleur** (`aiguilleur/`) - Message relay gateways for external channels (Discord, Telegram, etc.)
   - Produces: `relais:messages:incoming:{channel}`
   - Bridges external APIs to Redis Streams

2. **Portail** (`portail/`) - Consumer validating message format
   - Consumes: `relais:messages:incoming:{channel}`
   - Validates Envelope format, applies reply_policy (DND/vacation/in_meeting)
   - Produces: `relais:tasks`

3. **Sentinelle** (`sentinelle/`) - Consumer performing security checks
   - Consumes: `relais:tasks`
   - ACL validation (users.yaml), content guardrails pre/post-LLM filtering
   - Produces: `relais:tasks` (or refuses if ACL fails)

4. **Atelier** (`atelier/`) - Transformer executing LLM calls
   - Consumes: `relais:tasks`
   - Loads SOUL personality + context, calls LiteLLM with resilient retry (2s, 5s, 15s)
   - Handles ExhaustedRetriesError → DLQ (`relais:tasks:failed`)
   - Produces: `relais:messages:outgoing:{channel}`

5. **Souvenir** (`souvenir/`) - Consumer managing short/long-term memory
   - Consumes: `relais:messages:outgoing:*` (observes responses)
   - Short-term: Redis List `relais:context:{user_id}` (20 msgs, TTL 24h)
   - Long-term: SQLite `~/.relais/storage/messages.db`

### Observer & Support Services

- **Archiviste** (`archiviste/`) - Observer logging all events
  - Consumes: `relais:logs`, `relais:events:system`, `relais:events:messages`
  - Writes JSONL files + SQLite audit, never rejects messages

- **Aiguilleur Relays** - Discord, Telegram, Slack connectors
  - Consume: `relais:messages:outgoing:{channel}`
  - Send messages back to external APIs

### Configuration & Utilities

- **common/** - Shared utilities
  - `envelope.py`: Message wrapper (content, sender_id, channel, session_id, correlation_id, metadata, traces)
  - `redis_client.py`: Async Redis factory with per-brick ACL (password per service)
  - `config_loader.py`: YAML config cascade (user > system > project)

- **config/** - YAML configuration files
  - `config.yaml`: Redis socket, LiteLLM URL, logging, security settings
  - `litellm.yaml`: Model definitions, router settings, master key
  - `profiles.yaml`: LLM profiles (default/fast/precise/coder) with temp, max_tokens, retry delays
  - `users.yaml.default`: User registry with display_name, role, channels, llm_profile
  - `redis.conf`: Redis ACL definitions per brick, stream permissions

- **soul/** - RELAIS personality & context
  - `SOUL.md.default`: System prompt defining JARVIS-like assistant behavior

- **prompts/** - Channel-specific formatting
  - `telegram_default.md`: MarkdownV2 formatting, 1000 char limit
  - `whatsapp_default.md`: Plain text with bold/italic, 500 char limit
  - Context-aware variations: `in_meeting.md`, `out_of_hours.md`, `vacation.md`

## Key Architecture Concepts

### Redis Streams & Consumer Groups

- **At-least-once delivery**: Consumer groups with PEL (Pending Entry List) and XACK acknowledgment
- **Stream naming**: `relais:messages:incoming:{channel}`, `relais:tasks`, `relais:memory:*`
- **Initialization**: Each brick creates its consumer group on startup (idempotent)
- **Resilience**: Failed messages left in PEL are automatically re-delivered; poison pills sent to DLQ

### Envelope Pattern

All messages use a standardized `Envelope` dataclass:
```python
@dataclass
class Envelope:
    content: str
    sender_id: str  # "discord:{user_id}", "telegram:{chat_id}"
    channel: str
    session_id: str
    correlation_id: str  # UUID for request tracking
    timestamp: float  # epoch seconds
    metadata: dict  # Dynamic, includes reply_to, traces, session info
    media_refs: list[MediaRef]
```

Bricks use:
- `Envelope.from_json()` to deserialize from Redis
- `Envelope.from_parent()` or `.create_response_to()` to derive child envelopes
- `Envelope.add_trace(brick, action)` to record pipeline progression
- `Envelope.to_json()` to serialize for Redis

### Error Handling & Resilience

**Atelier (LLM caller)** implements resilient retry:
```
On exception:
  - RETRIABLE (httpx.ConnectError, TimeoutException): Return False → stays in PEL, re-delivered
  - HTTP 502/503/504: Retry with backoff [2s, 5s, 15s]
  - HTTP 4xx (400, 401): Re-raise (non-retriable)
  - ExhaustedRetriesError: Route to DLQ relais:tasks:failed, ACK (avoid poisoning PEL)
  - Unexpected errors: Log and ACK (safety net)
```

### Configuration Cascade

Priority order (highest to lowest):
1. `~/.relais/config/` - User overrides
2. `/opt/relais/config/` - System defaults
3. `./config/` - Project defaults

Environment variables override YAML configs (e.g., `REDIS_SOCKET_PATH`, `REDIS_PASSWORD`, per-brick `REDIS_PASS_*`)

### Orchestration (supervisord)

Priority-based startup (`supervisord.conf`):
- **Priority 1**: `courier` (Redis server)
- **Priority 5**: `litellm` (LLM proxy, `uv run`)
- **Priority 8**: `archiviste` (observer, non-blocking)
- **Priority 10**: Core bricks (portail, sentinelle, atelier, souvenir)
- **Priority 20**: Aiguilleur relays (Discord, Telegram, etc.)

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

Core dependencies: `redis >=5.0`, `litellm >=1.25`, `supervisor >=4.2`, `httpx >=0.27`, `pydantic >=2.9`
Dev: `pytest >=9.0`, `pytest-asyncio >=1.3`

### Running Services

**Single brick** (for development):
```bash
# Portail consumer
PYTHONPATH=. uv run python portail/main.py

# Atelier transformer
PYTHONPATH=. uv run python atelier/main.py

# Souvenir memory service
PYTHONPATH=. uv run python souvenir/main.py
```

**Full system** (via supervisord):
```bash
supervisord -c supervisord.conf

# Monitor
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
- `OPENROUTER_API_KEY` - LLM provider (if using OpenRouter)
- `REDIS_SOCKET_PATH` - Redis Unix socket path
- `LITELLM_BASE_URL` - LiteLLM proxy address
- Channel bot tokens: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, etc.
- Per-brick Redis passwords: `REDIS_PASS_PORTAIL`, `REDIS_PASS_ATELIER`, etc.

## Common Development Tasks

### Adding a New Brick

1. Create `{brick_name}/{main,consumer,producer,transformer}.py` with async class
2. Implement required methods: `__init__`, `start()` (entry point)
3. Register in `supervisord.conf` with appropriate priority
4. Add Redis ACL entry in `config/redis.conf` with stream permissions
5. Update `docs/ARCHITECTURE.md` with brick role
6. Add unit tests in `tests/test_{brick_name}.py`

### Handling Message Errors

- **Validation errors** (Portail): Log and continue (don't block pipeline)
- **Authorization failures** (Sentinelle): Return False to leave in PEL for operator review
- **LLM failures** (Atelier): Retry with backoff, then route to DLQ if exhausted
- **Memory service timeouts** (Atelier): Return False to retry (7.5s timeout per attempt)

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