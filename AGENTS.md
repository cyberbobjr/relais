# AGENTS.md

This file provides guidance to Codex and other coding agents working in this repository.

## Primary Context

- Read `README.md` first for the current implemented state of the repository.
- Read `CLAUDE.md` second for additional architecture and operational context.
- If `README.md` and `CLAUDE.md` differ, prefer `README.md` for current behavior and use `CLAUDE.md` as supplemental background.

## Project Summary

RELAIS is a micro-brick autonomous AI assistant architecture built around Redis Streams. The system is organized as an asynchronous pipeline of specialized services ("bricks") that exchange `Envelope` messages through named streams.

Core flow:
1. `aiguilleur/` receives channel input and stamps channel context.
2. `portail/` resolves user context and LLM profile.
3. `sentinelle/` applies incoming and outgoing ACL/security checks.
4. `atelier/` executes the agent loop and emits replies plus memory requests.
5. `souvenir/` persists conversation memory and file-related memory actions.

Additional active bricks called out in `README.md`:
6. `commandant/` handles slash commands outside the LLM flow.
7. `archiviste/` handles logs and observation.
8. `forgeron/` handles skill improvement and automatic skill creation workflows.

## Developer Commands

```bash
uv sync                                    # install deps (NOT poetry — project uses hatchling)
cp .env.example .env                       # then fill in keys
python -c "from common.init import initialize_user_dir; initialize_user_dir()"
alembic upgrade head                       # DB migrations (script_location = souvenir/migrations)

./supervisor.sh start all                  # full system via supervisord
./supervisor.sh --verbose start all        # start + tail logs (Ctrl+C detaches)
./supervisor.sh status
./supervisor.sh stop all
./supervisor.sh restart all
./supervisor.sh clear                      # clear .relais/logs
./supervisor.sh force-kill                 # kill orphan launcher processes

uv run pytest tests/ -v                    # all tests
uv run pytest tests/test_atelier.py -v     # single test file
uv run pytest -m unit                      # unit tests only
uv run pytest -m integration               # integration tests (need Redis)
```

## Critical Test Rules

- `tests/test_smoke_e2e.py` is **always skipped** — never include in automated loops
- Always run pytest with `--timeout=30` (set in pyproject.toml)
- If a test times out, stop and report — do not retry blindly
- E2E marker exists but no tests should auto-run with it

## Running a Single Brick

```bash
uv run python launcher.py atelier/main.py   # via launcher (debugpy support)
uv run python atelier/main.py               # direct (no debugpy)
```

All bricks go through `launcher.py` under supervisord, which handles `DEBUGPY_ENABLED`, `DEBUGPY_PORT`, `DEBUGPY_WAIT` env vars.

Debug ports: atelier=5678, portail=5679, sentinelle=5680, archiviste=5681, souvenir=5682, commandant=5683, aiguilleur=5684, forgeron=5685

## Architecture at a Glance

Pipeline flow: **Aiguilleur → Portail → Sentinelle → Atelier → Sentinelle(out) → Aiguilleur**

Side channels: Commandant (slash commands), Souvenir (memory), Forgeron (skill improvement), Archiviste (logging).

Key Redis streams (full list in `common/streams.py`):

| Stream | Producer | Consumer |
|--------|----------|----------|
| `relais:messages:incoming` | Aiguilleur | Portail |
| `relais:security` | Portail | Sentinelle |
| `relais:tasks` | Sentinelle | Atelier |
| `relais:commands` | Sentinelle | Commandant |
| `relais:memory:request` | Atelier, Commandant | Souvenir, Forgeron |
| `relais:messages:outgoing_pending` | Atelier | Sentinelle |
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant | Aiguilleur |
| `relais:tasks:failed` | Atelier | diagnostics |
| `relais:skill:trace` | Atelier | Forgeron |
| `relais:logs` | all bricks | Archiviste |

## Brick Ownership — Edit the Right File

- **Aiguilleur** (`aiguilleur/`): channel adapters. Only Discord adapter fully implemented. Entry: `aiguilleur/main.py` (unified manager, not per-channel processes).
- **Portail** (`portail/`): user resolution via `UserRegistry`, stamps `user_id`, `user_record`, `llm_profile`.
- **Sentinelle** (`sentinelle/`): ACL checks, bifurcates to `relais:tasks` or `relais:commands`.
- **Atelier** (`atelier/`): LLM execution via DeepAgents/LangGraph. Owns tool policy, MCP integration, streaming, subagent registry.
- **Commandant** (`commandant/`): slash commands (`/help`, `/clear`). Do NOT put command logic in Atelier.
- **Souvenir** (`souvenir/`): SQLite memory persistence. No LLM calls inside Souvenir.
- **Forgeron** (`forgeron/`): skill changelog/consolidation + auto-creation from archives.
- **Archiviste** (`archiviste/`): logging observer. Does NOT observe all streams.

## Working Rules

- Preserve the envelope-based pipeline contract. Do not introduce ad hoc message formats when an `Envelope` can be used.
- Respect context namespace ownership in `Envelope.context`. Each brick should only write to its own namespace.
- Keep Redis stream names and helpers centralized in `common/streams.py`.
- Keep shared context keys and types centralized in `common/contexts.py`.
- Follow the config cascade already used by the project: user > system > project.
- Prefer extending existing brick responsibilities over adding cross-cutting logic in the wrong service.

## Context Namespace Rules

Each brick writes ONLY to its own `context` namespace in the envelope:
- `context["aiguilleur"]`, `context["portail"]`, `context["sentinelle"]`, `context["atelier"]`, `context["souvenir_request"]`

Constants in `common/contexts.py`. Use `ensure_ctx(envelope, key)` to get-or-create. Never mutate another brick's namespace.

## Envelope Contract

- All messages use `Envelope` dataclass from `common/envelope.py`
- Use `Envelope.from_json()` to deserialize, `Envelope.to_json()` to serialize
- Use `Envelope.from_parent()` for child envelopes (deep-copies traces + context)
- Use `Envelope.add_trace(brick, action)` to record pipeline steps
- Action constants in `common/envelope_actions.py`

## ACK Contract (Critical)

- Only ACK after downstream publish succeeds OR final DLQ path reached
- Never ACK on transient/retriable errors — message stays in PEL for redelivery
- `ExhaustedRetriesError` → route to DLQ, then ACK (avoid PEL poisoning)
- Delivery semantics are at-least-once via Redis Streams consumer groups

## Configuration

- Cascade: `RELAIS_HOME/config/` > `/opt/relais/config/` > `./config/`
- All bricks support hot-reload via `watchfiles` on their YAML configs
- `config/aiguilleur.yaml` is **NOT** copied by `initialize_user_dir()` — create manually from `config/aiguilleur.yaml.default`
- `base_url` in profiles.yaml supports `${VAR}` interpolation — fails immediately if var missing
- Default `RELAIS_HOME` is `./.relais` at repo root
- Redis uses Unix socket at `./.relais/redis.sock` with per-brick ACL passwords

## Adding a New Brick

1. Create `{brick}/main.py` inheriting `common.brick_base.BrickBase`
2. Implement `_load()`, `stream_specs()`, `_create_shutdown()`
3. Register in `supervisord.conf` with priority (1=infra, 8=observers, 10=core, 20=adapters)
4. Add Redis ACL in `config/redis.conf`
5. Use `RedisClient("<brick_name>")` — resolves `REDIS_PASS_<BRICK>` then `REDIS_PASSWORD`

## Adding a New Subagent

1. Create `config/atelier/subagents/{name}.yaml` (file stem must match `name` field)
2. Add to roles' `allowed_subagents` in `portail.yaml` (fnmatch patterns)
3. Atelier picks up automatically on hot-reload — no code changes needed

## Debugging

```bash
redis-cli -s ./.relais/redis.sock XLEN relais:tasks
redis-cli -s ./.relais/redis.sock XRANGE relais:tasks - +
```

Logs: `.relais/logs/events.jsonl`, `.relais/logs/*.log`

## When Changing Behavior

- Verify which brick owns the behavior before editing.
- Check whether the change impacts stream contracts, context fields, ACL flow, or memory persistence.
- Prefer documenting durable architectural decisions in `README.md`, `CLAUDE.md`, or another versioned doc so future agents inherit the context.

## References

- `README.md` — current implemented state, architecture diagram
- `docs/ARCHITECTURE.md` — per-brick technical reference
- `docs/REDIS_BUS_API.md` — canonical Redis stream schemas and consumer groups
- `docs/ENV.md` — environment variables
- `docs/CONTRIBUTING.md` — contribution workflow
- `common/streams.py` — all stream name constants
- `common/contexts.py` — context namespace constants and TypedDicts
- `common/envelope_actions.py` — ACTION_* constants
