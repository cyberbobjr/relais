# Implementation Plan: Subagents YAML Migration

## Overview

Migrate RELAIS subagents from Python modules (`atelier/agents/*.py`) to YAML declarations loaded
from the config cascade. Collapse the `atelier/agents/` package into a single `atelier/subagents.py`
module. Introduce a static `@tool` registry under `atelier/tools/` for Python-native tools, and
extend the YAML `tools` field with a hybrid token system (`mcp:<glob>`, `inherit`, `<static_name>`).

## Design Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Package layout | **Option B** — collapse `atelier/agents/` into `atelier/subagents.py` |
| 2 | Cascade merge | **Option A** — merge by name, user priority (`~/.relais/` > `/opt/relais/` > `./`) |
| 3 | `tools` field | **Hybrid token system** — `mcp:<glob>`, `inherit`, `<static_name>` |
| 4 | `specs_for_user` signature | `specs_for_user(user_record, request_tools)` — second arg is per-request MCP pool |
| 5 | `delegation_snippet` | Optional YAML field; auto-generated from `description` first line if absent |
| 6 | `atelier/tools/` | **In scope** — new package for static `@tool`-decorated Python functions |

## Tool Resolution Design

The YAML `tools` token list resolves to actual `BaseTool` instances from two sources:

| Token | Source | Resolution timing |
|---|---|---|
| `mcp:<glob>` | `make_mcp_tools()` filtered by ToolPolicy | Dynamic, per-request |
| `inherit` | All tools from main agent's request pool | Dynamic, per-request |
| `<name>` (no prefix) | Static `@tool` registry from `atelier/tools/*.py` | Loaded at startup |

Mixed entries allowed: `tools: ["mcp:git_*", "read_config_file"]`

`inherit` never widens the security boundary — it yields exactly the tools already allowed by
`ToolPolicy` for the current request.

Unknown tokens → `WARNING` log + dropped (fail-closed, no raise).

## delegation_snippet

RELAIS-specific field injected into the main agent's soul prompt to instruct it *when* and *how*
to delegate. Supplements the deepagents `description` mechanism (which handles `task()` routing).
Optional: if absent, auto-generated as `- **{name}**: {description_first_line}`.

## Architecture Changes

### Create

| File | Purpose |
|---|---|
| `atelier/subagents.py` | `SubagentSpec` dataclass + `SubagentRegistry` (loader, cascade-merge, filter, delegation prompt, token resolution) |
| `atelier/tools/__init__.py` | Package marker |
| `atelier/tools/_registry.py` | `ToolRegistry` — discovers `@tool`-decorated callables in `atelier/tools/*.py` |
| `config/atelier/subagents/relais-config.yaml.default` | YAML port of `atelier/agents/config_admin.py` |
| `tests/test_subagents_registry.py` | Unit tests: YAML load, cascade merge, filter, delegation prompt |
| `tests/test_subagents_tools_resolution.py` | Unit tests: all token forms + unknown token behaviour |
| `tests/test_tool_registry.py` | Unit tests: `ToolRegistry` discovery |
| `tests/test_subagents_hot_reload.py` | Hot-reload file-watcher test |

### Modify

| File | Change |
|---|---|
| `atelier/main.py` | Import path, `ToolRegistry.discover()`, `SubagentRegistry.load()`, pass `request_tools` to `specs_for_user`, add subagents dir to watch paths |
| `atelier/agent_executor.py` | Docstring references only (no behaviour change) |
| `common/init.py` | Add `config/atelier/subagents/relais-config.yaml` to `DEFAULT_FILES`; add `config/subagents` to `dirs` |
| `common/config_loader.py` | Add `resolve_config_dir(subpath)` helper if not present |
| `tests/test_config_admin_subagent.py` | Replace module-import-based tests with YAML-load-based tests |
| `tests/test_atelier_file_watcher.py` | Add subagents dir to expected watch-path set |
| `tests/test_atelier_hot_reload.py` | Add subagent reload path assertions |

### Delete

| File |
|---|
| `atelier/agents/__init__.py` |
| `atelier/agents/_protocol.py` |
| `atelier/agents/_registry.py` |
| `atelier/agents/config_admin.py` |
| `atelier/agents/` (directory) |

## Data Model

```python
# atelier/subagents.py
@dataclass(frozen=True)
class SubagentSpec:
    name: str                        # unique id, matches allowed_subagents patterns
    description: str                 # shown to main agent via build_spec
    system_prompt: str               # full prompt for the subagent
    tools: tuple[str, ...]           # raw tokens from YAML
    delegation_snippet: str | None   # optional; auto-gen if None
    source_path: Path                # for logging / hot-reload diagnostics
```

## YAML Schema (`config/atelier/subagents/<name>.yaml`)

```yaml
name: relais-config

description: |
  Reads and modifies RELAIS YAML configuration files
  (portail.yaml, channels.yaml, profiles.yaml, …).

system_prompt: |
  You are the RELAIS configuration administrator …
  (full multi-line prompt)

# Tool tokens — resolved at request-time:
#   mcp:<glob>   → MCP tools matching the glob (e.g. mcp:filesystem_*)
#   inherit      → all tools from the main agent (post-ToolPolicy)
#   <name>       → static @tool function from atelier/tools/
tools: []

# Optional — auto-generated from first line of description if absent
# delegation_snippet: |
#   - **relais-config**: Reads and modifies RELAIS configuration files …
```

Validation rules (fail-closed per file — log ERROR + skip on violation):
- `name`: required, non-empty, matches `[a-z0-9][a-z0-9-]*`
- `description`: required, non-empty
- `system_prompt`: required, non-empty
- `tools`: optional, list of strings; defaults to `[]`
- File stem must equal `name` (prevents silent duplicates on cascade merge)
- Unknown extra fields: logged as WARNING, ignored

---

## Implementation Phases

### Phase 1 — ToolRegistry (static `@tool` discovery)

**Step 1** — Create `atelier/tools/__init__.py`
- Action: Empty package marker with docstring.
- Risk: Low.

**Step 2** — Write tests (File: `tests/test_tool_registry.py`) — RED
- Action: Test `ToolRegistry.discover()` finds `@tool` functions in modules under `atelier/tools/`;
  `get(name)` returns the `BaseTool`; `all()` returns the full dict;
  underscore-prefixed modules (e.g. `_registry.py`) are skipped.
- Risk: Low.

**Step 3** — Implement `ToolRegistry` (File: `atelier/tools/_registry.py`)
- Action: `@dataclass(frozen=True)` with `_tools: dict[str, BaseTool]`.
  Classmethod `discover()` uses `pkgutil.iter_modules(atelier.tools.__path__)`,
  imports each module, collects attributes that are `isinstance(obj, BaseTool)`.
  `@tool`-decorated functions are `StructuredTool`/`BaseTool` instances after decoration.
- Risk: Medium — must correctly identify `@tool` outputs vs other module-level objects.

**Step 4** — Verify GREEN: `pytest tests/test_tool_registry.py -v`

---

### Phase 2 — SubagentSpec + YAML loader

**Step 5** — Write tests (File: `tests/test_subagents_registry.py`) — RED
- Action: `tmp_path` fixtures simulate user/system/project cascade dirs.
  Assert: per-name merge with user priority; malformed YAML logged + skipped;
  missing required fields raise `ValueError`; `file stem != name` skips file.
- Risk: Low.

**Step 6** — Implement `SubagentSpec` + `SubagentRegistry.load()` (File: `atelier/subagents.py`)
- Action: `SubagentSpec` frozen dataclass. `SubagentRegistry` classmethod `load(tool_registry)`
  enumerates `config/atelier/subagents/*.yaml` through the cascade, merges by `name` (first occurrence
  in priority order wins). Stores raw `tools` tokens — does NOT resolve callables yet.
- Risk: Medium — cascade walk must be deterministic.

**Step 7** — Implement filter helpers (File: `atelier/subagents.py`)
- Action: Port `_parse_subagent_patterns` and `_matches_patterns` from `_registry.py`.
  Add `all_names` property and `_filter_for_user(user_record) -> list[SubagentSpec]`.
- Risk: Low.

**Step 8** — Implement `delegation_prompt_for_user` (File: `atelier/subagents.py`)
- Action: Preserve `_DELEGATION_PREAMBLE` constant. For each allowed spec, use
  `spec.delegation_snippet` if set, else auto-generate `- **{name}**: {first_line}`.
- Risk: Low.

**Step 9** — Verify GREEN: `pytest tests/test_subagents_registry.py -v`

---

### Phase 3 — Tool-token resolution

**Step 10** — Write tests (File: `tests/test_subagents_tools_resolution.py`) — RED
- Action: Given a spec with `tools: ["mcp:fs_*", "inherit", "read_config_file"]` and a
  `request_tools` list containing MCP tools + a `ToolRegistry` containing `read_config_file`,
  assert the resolved `tools` list in the emitted dict is correct.
  Assert `inherit` yields the full `request_tools`.
  Assert unknown static tokens are logged WARNING and dropped.
- Risk: Medium.

**Step 11** — Implement `specs_for_user(user_record, request_tools)` (File: `atelier/subagents.py`)
- Action: For each allowed `SubagentSpec`, resolve tokens:
  - `mcp:<glob>` → `fnmatch.filter([t.name for t in request_tools], glob)` mapped back to tools.
  - `inherit` → all `request_tools`.
  - bare name → `tool_registry.get(name)`; drop + log WARNING if missing.
  Build deepagents-compatible dict `{name, description, system_prompt, tools}` and return the list.
- Risk: Medium — glob matching on `.name` must match MCP tool naming conventions.

**Step 12** — Verify GREEN: `pytest tests/test_subagents_tools_resolution.py -v`

---

### Phase 4 — Integrate into Atelier

**Step 13** — Update `atelier/main.py`
- Action:
  - Replace `from atelier.agents import SubagentRegistry` with
    `from atelier.subagents import SubagentRegistry` and
    `from atelier.tools._registry import ToolRegistry`.
  - In `__init__`: `self._tool_registry = ToolRegistry.discover()`
    and `self._subagent_registry = SubagentRegistry.load(self._tool_registry)`.
  - In `_handle_message`, after building `mcp_tools`, pass `request_tools = mcp_tools` to
    `self._subagent_registry.specs_for_user(ur, request_tools)`.
  - `delegation_prompt_for_user(ur)` signature unchanged.
- Risk: High — live integration path; verify ToolPolicy filter composition.

**Step 14** — Update `atelier/agent_executor.py` docstrings
- Action: Replace `SubagentRegistry` module references. No behaviour change.
- Risk: Low.

---

### Phase 5 — Hot-reload + bootstrap

**Step 15** — Add subagents dir to watch paths (File: `atelier/main.py`)
- Action: Extend `_config_watch_paths()` to include the subagents config dir (all cascade roots
  that exist). Add `reload_subagents()` using existing `safe_reload` pattern.
  Wire into `_config_reload_listener` (Redis pub/sub `relais:config:reload:atelier`).
- Risk: Medium — `watchfiles.awatch` accepts directories; verify no spurious reload on temp files.
  Add `*.yaml` extension filter.

**Step 16** — Write hot-reload tests (File: `tests/test_subagents_hot_reload.py`)
- Action: Write new YAML file → trigger reload → assert new spec in `specs_for_user`.
  Overwrite with malformed YAML → assert previous state preserved.
- Risk: Medium.

**Step 17** — Bootstrap default YAML
- Action (File: `config/atelier/subagents/relais-config.yaml.default`):
  Port `CONFIG_ADMIN_SYSTEM_PROMPT`, description, and delegation snippet from
  `atelier/agents/config_admin.py` verbatim (use `|` block scalar for prompt).
  Set `tools: []` (relais-config uses deepagents backend builtins: read_file, write_file, run_command).
  Extend the routing table in the system prompt to include `config/atelier/subagents/*.yaml` as editable.
- Action (File: `common/init.py`):
  Add `("config/atelier/subagents/relais-config.yaml", "config/atelier/subagents/relais-config.yaml.default")` to
  `DEFAULT_FILES`. Add `"config/subagents"` to `dirs`.
- Risk: Low — pure data migration; verified by round-trip integration test.

---

### Phase 6 — Cleanup & test migration

**Step 18** — Grep for remaining `atelier.agents` references
- Action: `grep -rn "atelier.agents"` across the entire repo. Update any remaining imports.
- Risk: Low.

**Step 19** — Delete legacy package
- Action: Remove `atelier/agents/__init__.py`, `_protocol.py`, `_registry.py`, `config_admin.py`,
  and the directory.
- Dependencies: All phases green.
- Risk: Low (git-reversible).

**Step 20** — Migrate existing tests
- Action:
  - `tests/test_config_admin_subagent.py`: load default YAML via `SubagentRegistry.load()`;
    assert spec fields; assert fnmatch ACL still works.
  - `tests/test_atelier_file_watcher.py`: add subagents dir to expected watch-path set.
  - `tests/test_atelier_hot_reload.py`: add subagent reload path assertions.
- Risk: Medium — patch targets change.

**Step 21** — Full suite green
- Action: `pytest tests/ -v --cov=atelier/subagents.py --cov=atelier/tools/_registry.py
  --cov-report=term-missing`. Enforce 80%+ on new modules.

---

### Phase 7 — Documentation

**Step 22** — Update `CLAUDE.md`
- Action: Replace "Adding a New Subagent" section with YAML-first instructions.
  Document `tools` token forms. Replace `atelier/agents/` references.

**Step 23** — Update `docs/ARCHITECTURE.md`
- Action: Replace Python-module subagent section with YAML-based description + schema.
  Add `atelier/tools/` to the architecture overview.

**Step 24** — Update `docs/CONTRIBUTING.md`
- Action: Update subagent creation workflow. Add `@tool` function authoring guide.

**Step 25** — Update `config/portail.yaml.default` comment
- Action: Change comment on `allowed_subagents` from `atelier/agents/` to `config/atelier/subagents/`.

---

## Testing Strategy

| Layer | Tests |
|---|---|
| Unit | `ToolRegistry` discovery; YAML parse + validation; cascade merge; fnmatch filter; delegation prompt (explicit + auto-gen) |
| Token resolution | All 3 token forms + mixed + unknown (WARNING + drop) |
| Integration | Full `_handle_message` with mocked Redis + mocked MCP pool |
| Hot-reload | File-watcher triggers reload; malformed YAML preserves previous state; Redis pub/sub path |
| Migration regression | `test_config_admin_subagent.py` semantics preserved after YAML port |

Coverage target: **80%+** on `atelier/subagents.py` and `atelier/tools/_registry.py`.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| `@tool` detection misidentifies non-tool objects | Strict `isinstance(obj, BaseTool)` from `langchain_core.tools` |
| MCP glob patterns don't match actual tool names | Document naming convention in yaml.default comments; integration test with realistic MCP tool names |
| Hot-reload fires on editor swap files | `watchfiles` `*.yaml` extension filter; idempotent `safe_reload` |
| Cascade merge picks wrong precedence | Dedicated unit test asserting user > system > project for same subagent name |
| `inherit` leaks tools beyond intended scope | `request_tools` is already filtered by `ToolPolicy` — `inherit` can never widen the boundary; document this contract in docstring |
| Deleting `atelier/agents/` breaks unidentified imports | `grep -rn "atelier.agents"` before deletion (Step 18) |
| YAML parse fails on special chars in long system prompt | Use `|` literal block scalar; add round-trip test asserting byte-identical prompt |
| Hot-reload race with in-flight request | Registry is frozen; atomic reference swap; in-flight request keeps old ref and completes |

---

## Success Criteria

- [ ] `atelier/agents/` directory fully deleted; `atelier/subagents.py` is its replacement
- [ ] `atelier/tools/_registry.py` discovers `@tool`-decorated functions at startup
- [ ] `config/atelier/subagents/relais-config.yaml.default` bootstrapped by `initialize_user_dir`
- [ ] `SubagentRegistry.load(tool_registry)` reads YAML through cascade, merges by name with user priority
- [ ] `specs_for_user(user_record, request_tools)` resolves `mcp:<glob>`, `inherit`, `<static_name>` correctly
- [ ] Unknown static tool tokens logged WARNING and dropped (fail-closed)
- [ ] `delegation_snippet` optional; auto-generated from description first line when absent
- [ ] Hot-reload of `config/atelier/subagents/*.yaml` via file-watcher and Redis pub/sub
- [ ] All existing tests updated and passing
- [ ] New tests for ToolRegistry, YAML loader, token resolution, hot-reload all passing
- [ ] `pytest tests/ -v` green with 80%+ coverage on new modules
- [ ] `portail.yaml` `allowed_subagents` fnmatch filtering unchanged end-to-end
- [ ] Documentation updated: `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/CONTRIBUTING.md`
- [ ] relais-config routing table updated to include `config/atelier/subagents/*.yaml` as editable

---

## Relevant Files

### To create
- `atelier/subagents.py`
- `atelier/tools/__init__.py`
- `atelier/tools/_registry.py`
- `config/atelier/subagents/relais-config.yaml.default`
- `tests/test_subagents_registry.py`
- `tests/test_subagents_tools_resolution.py`
- `tests/test_tool_registry.py`
- `tests/test_subagents_hot_reload.py`

### To modify
- `atelier/main.py`
- `atelier/agent_executor.py` (docstrings only)
- `common/init.py`
- `common/config_loader.py`
- `config/portail.yaml.default`
- `tests/test_config_admin_subagent.py`
- `tests/test_atelier_file_watcher.py`
- `tests/test_atelier_hot_reload.py`
- `CLAUDE.md`
- `docs/ARCHITECTURE.md`
- `docs/CONTRIBUTING.md`

### To delete
- `atelier/agents/__init__.py`
- `atelier/agents/_protocol.py`
- `atelier/agents/_registry.py`
- `atelier/agents/config_admin.py`
- `atelier/agents/` (directory)

### Reference (read-only)
- `atelier/agent_executor.py` (consumes subagent spec dicts)
- `atelier/tool_policy.py` (ToolPolicy.filter_mcp_tools — security boundary)
- `atelier/mcp_adapter.py` (make_mcp_tools — MCP pool source)
- `atelier/mcp_session_manager.py`
- `common/config_loader.py` (cascade resolution)
- `common/config_reload.py` (safe_reload + watch_and_reload)
- `config/portail.yaml.default` (allowed_subagents — unchanged)
