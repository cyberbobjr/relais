# RELAIS — Contribution Guide

**Last updated:** 2026-04-23

This guide describes the contribution workflow actually adapted to the current repository.

---

## Development Setup

### Prerequisites

- Python `>=3.11`
- `uv`
- Local Redis if you want to run the pipeline
- `supervisord` if you use supervised mode

### Local Installation

```bash
git clone <repo-url>
cd relais

uv sync

cp .env.example .env

python -c "from common.init import initialize_user_dir; initialize_user_dir()"
```

### Working Directory

By default, RELAIS uses `./.relais` at the repo root. You can override this with `RELAIS_HOME`.

`initialize_user_dir()`:

- creates the working structure under `RELAIS_HOME`
- copies the shipped templates
- never overwrites existing files

It is correct for brick entry points to call it at startup.

### Local Startup

#### Recommended Option

```bash
./supervisor.sh start all             # Start the system
./supervisor.sh --verbose start all   # Start + follow logs (Ctrl+C to detach)
./supervisor.sh status                # Check brick status
./supervisor.sh --verbose restart all # Restart + follow logs
```

#### Manual Option

```bash
redis-server config/redis.conf

uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

---

## Test Architecture

The repository primarily uses:

- unit tests with `pytest`
- async tests with `pytest-asyncio`
- `unittest.mock` mocks
- `fakeredis` for some local integration tests

Markers defined in `pyproject.toml`:

- `unit`
- `integration`

### Useful Test Types

- loader/config unit tests: e.g. `tests/test_channel_config.py`
- brick unit tests: e.g. `tests/test_commandant.py`, `tests/test_soul_assembler.py`
- smoke / pipeline integration: e.g. `tests/test_smoke_e2e.py`

### Common Commands

```bash
uv run pytest tests/ -v

uv run pytest tests/test_channel_config.py -v

uv run pytest tests/test_smoke_e2e.py -v
```

With coverage:

```bash
uv run pytest tests/ --cov=common,portail,sentinelle,atelier,souvenir,aiguilleur,archiviste,commandant --cov-report=term-missing
```

---

## Contributing to an Existing Brick

### Practical Rules

- start from the brick's `main.py` to understand the actual flow
- check the Redis streams actually consumed and produced
- look at existing tests before introducing new patterns
- update the docs if you change a stream, an environment variable, or an entry point

### Useful References

- [README.md](/Users/benjaminmarchand/IdeaProjects/relais/README.md)
- [docs/ARCHITECTURE.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ARCHITECTURE.md)
- [docs/ENV.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ENV.md)

---

## Adding a New Brick

### Minimal Checklist

- create a dedicated package with a `main.py`
- use `RedisClient("<brick_name>")`
- call `initialize_user_dir()` in the `__main__` block
- document the consumed and produced streams
- add targeted tests
- add the brick to `supervisord.conf` if it should start with the rest of the system
- update the README and `docs/ARCHITECTURE.md`

### Minimal Pattern

All bricks inherit from `common.brick_base.BrickBase`. The minimal template:

```python
import asyncio

from common.brick_base import BrickBase, StreamSpec
from common.shutdown import GracefulShutdown  # noqa: F401 — test patch target


class MyBrick(BrickBase):
    def __init__(self) -> None:
        super().__init__("mybrick")

    def _create_shutdown(self) -> GracefulShutdown:
        return GracefulShutdown()

    def _load(self) -> None:
        pass  # load YAML into self attributes here

    def stream_specs(self) -> list[StreamSpec]:
        return [StreamSpec(stream="relais:my:stream", group="mybrick_group",
                           consumer="mybrick_1", handler=self._handle)]

    async def _handle(self, envelope, redis_conn) -> bool:
        ...
        return True  # True = XACK, False = leave in PEL


if __name__ == "__main__":
    asyncio.run(MyBrick().start())
```

### Key Points

- `RedisClient` uses `REDIS_PASS_<BRICK>` first, then falls back to `REDIS_PASSWORD`
- the repository favors Redis consumer groups for consumption loops
- ACKs are managed explicitly in read loops

---

## Adding an Aiguilleur Channel

The channel supervisor entry point is [aiguilleur/main.py](/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/main.py). Channels are configured via `config/aiguilleur.yaml` and loaded by `load_channels_config()`.

### Two Supported Modes

- `type: native`: Python adapter loaded dynamically
- `type: external`: subprocess supervised by the aiguilleur

### For a Native Channel

- create a module `aiguilleur/channels/<channel>/adapter.py`
- expose a `*Aiguilleur` class
- implement the contract expected by `AiguilleurManager`
- publish incoming messages to `relais:messages:incoming`
- consume `relais:messages:outgoing:<channel>` for output

The current reference example is [aiguilleur/channels/discord/adapter.py](/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/channels/discord/adapter.py).

### For an External Channel

Declare in `aiguilleur.yaml`:

```yaml
channels:
  mychannel:
    enabled: true
    streaming: false
    type: external
    command: node
    args:
      - adapters/mychannel/index.js
```

### Configuration

`config/aiguilleur.yaml` is not copied automatically by `initialize_user_dir()`. If you add a channel, remember to:

- update [config/aiguilleur.yaml.default](/Users/benjaminmarchand/IdeaProjects/relais/config/aiguilleur.yaml.default)
- document the manual creation of the override file at `RELAIS_HOME/config/aiguilleur.yaml`

---

## Adding a Custom Subagent

Subagents are discovered automatically by atelier on hot-reload. Two tiers are supported:

### User Subagents (in `$RELAIS_HOME/config/atelier/subagents/`)

1. Create `$RELAIS_HOME/config/atelier/subagents/{name}/` (the directory name must exactly match the `name` field in the YAML)
2. Add `subagent.yaml` with required fields: `name`, `description`, `system_prompt` and optionally `tool_tokens`, `skill_tokens`, `delegation_snippet`
3. Add the subagent name to the relevant roles in `portail.yaml` via `allowed_subagents` (fnmatch patterns, e.g. `["my-agent"]` or `["my-*"]`)
4. Optional: create `tools/` containing Python modules exporting `BaseTool` instances
5. Optional: create `skills/` containing skill directories
6. No code changes required — atelier discovers changes on hot-reload

### Tool Tokens

<!-- AUTO-GENERATED: Docstring from atelier/subagents.py -->
The `tool_tokens` section of `subagent.yaml` accepts several token forms:

- `local:<name>` — tool loaded from `tools/<name>.py` in the subagent directory
- `mcp:<glob>` — fnmatch filter on the request's MCP tools (already filtered by `ToolPolicy`)
- `inherit` — all MCP tools from the request
- `module:<dotted.path>` — imports a Python module and collects all `BaseTool` instances. **Security**: only prefixes in `_ALLOWED_MODULE_PREFIXES` (`aiguilleur.channels.`, `atelier.tools.`, `relais_tools.`) are allowed. Others produce a WARNING and are ignored.
- `<name>` (no prefix) — static tool from `ToolRegistry` (`atelier/tools/*.py`)

**Token validation**:
- At `load()` time, `module:` tokens and static `<name>` references are validated at startup
- `mcp:`, `inherit`, and `local:` forms are dynamic and validated at runtime
- A degraded subagent (one or more invalid tokens) remains accessible but runs with only the valid tools
- Unresolved tokens are logged as WARNING with the reason (non-importable module, tool not found, etc.)

### Native Subagents (in `atelier/subagents/`, shipped with the source)

Native subagents are scanned **after** user subagents. To add one to the repository:

1. Create `atelier/subagents/{name}/` with `subagent.yaml` (same structure as user subagents)
2. Optional: add to `common/init.py` in `DEFAULT_FILES` if the file should be copied during initialization
3. Hot-reload is automatically supported for native subagents

---

## Code Conventions

- Python 3.11+
- type hints wherever useful
- short but concrete docstrings
- tests for any change to a flow, config, or Redis contract
- keep docs anchored in the actual code, not a target architecture

---

## Before Opening a PR

- verify that documented entry points still exist
- check any modified streams and environment variables
- run the targeted tests affected by your change
- re-read `README.md`, `docs/ARCHITECTURE.md`, and `docs/ENV.md` if you change public-facing behavior
