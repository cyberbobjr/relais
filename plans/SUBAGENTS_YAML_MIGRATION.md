# Implementation Plan: Subagents — Directory-Based Pack Format

## Overview

Each subagent is a **directory** under `config/atelier/subagents/<name>/` found in the config
cascade (user > system > project).  The directory bundles all subagent resources together:

```
config/atelier/subagents/
└── my-agent/
    ├── subagent.yaml       # Required — spec (name, description, system_prompt, …)
    ├── tools/              # Optional — Python modules exporting BaseTool instances
    │   ├── search.py
    │   └── write.py
    └── skills/             # Optional — skill directories for create_deep_agent()
        └── my-skill/
            └── SKILL.md
```

This replaces the previous flat `<name>.yaml` format.  There is **no backward compatibility**
with flat YAML files — flat files in `subagents/` are silently ignored.

## Design Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Layout | **Directory per subagent** — `<name>/subagent.yaml` + optional `tools/` + optional `skills/` |
| 2 | Cascade merge | Merge by name, user priority (`~/.relais/` > `/opt/relais/` > `./`) |
| 3 | `tool_tokens` field | Extended token system: `local:<name>`, `mcp:<glob>`, `inherit`, `<static_name>` |
| 4 | `skill_tokens` field | Token system: `local:<name>` resolves to pack-local skill dir |
| 5 | `specs_for_user` | `specs_for_user(user_record, request_tools)` — tools/skills resolved at call time |
| 6 | Module isolation | `importlib.util.spec_from_file_location`, synthetic names, NO `sys.modules` insertion |
| 7 | Metadata immutability | `SubagentSpec` frozen dataclass; callables kept in non-frozen `SubagentRegistry` |
| 8 | Path traversal guard | `local:` skill tokens validated: resolved path must stay inside `<pack_dir>/skills/` |

## Tool Token Forms

| Token | Source | Resolution |
|---|---|---|
| `local:<name>` | Pack's `tools/<name>.py` | Loaded at startup via importlib |
| `mcp:<glob>` | Per-request MCP pool (ToolPolicy-filtered) | Dynamic, per-request |
| `inherit` | All per-request MCP tools | Dynamic, per-request |
| `<name>` (no prefix) | Global `ToolRegistry` (`atelier/tools/*.py`) | Loaded at startup |

Mixed entries allowed: `tool_tokens: ["local:search", "mcp:git_*", "read_config_file"]`

Unknown or unresolvable tokens → `WARNING` log + dropped (fail-closed, no raise).

## Skill Token Forms

| Token | Resolution |
|---|---|
| `local:<name>` | Absolute path to `skills/<name>/` inside the pack dir |

Unknown token forms → `WARNING` log + dropped.

## Architecture

### Data Model

```python
# atelier/subagents.py

@dataclass(frozen=True)
class SubagentSpec:
    name: str                        # unique id, matches allowed_subagents patterns
    description: str                 # shown to main agent
    system_prompt: str               # full prompt for the subagent
    tool_tokens: tuple[str, ...]     # raw tokens from YAML tool_tokens field
    skill_tokens: tuple[str, ...]    # raw tokens from YAML skill_tokens field
    delegation_snippet: str | None   # optional; auto-gen from description first line if None
    source_path: Path                # path to subagent.yaml (diagnostics)
    pack_dir: Path                   # containing directory (tool/skill loading)

@dataclass  # NOT frozen — holds dicts of loaded callables
class SubagentRegistry:
    _specs: tuple[SubagentSpec, ...]
    _tool_registry: Any
    _local_tools_by_subagent: dict[str, dict[str, Any]]
    _local_skills_by_subagent: dict[str, dict[str, str]]
```

### YAML Schema (`subagent.yaml`)

```yaml
name: my-agent

description: |
  Short description — first line is used for auto-generated delegation snippet.

system_prompt: |
  Full multi-line system prompt …

# Tool tokens — resolved at request-time (default: []):
#   local:<name>  → tool from tools/<name>.py in this pack
#   mcp:<glob>    → MCP tools matching the glob (e.g. mcp:filesystem_*)
#   inherit       → all tools from the main agent (post-ToolPolicy)
#   <name>        → static tool from global ToolRegistry (atelier/tools/)
tool_tokens: []

# Skill tokens — resolved at load-time (default: []):
#   local:<name>  → skills/<name>/ directory inside this pack
skill_tokens: []

# Optional — auto-generated from first line of description if absent
# delegation_snippet: |
#   - **my-agent**: Short description …
```

Validation rules (fail-closed per directory — log ERROR + skip on violation):
- `name`: required, non-empty, matches `[a-z0-9][a-z0-9-]*`
- `description`: required, non-empty
- `system_prompt`: required, non-empty
- `tool_tokens`: optional, list of strings; defaults to `[]`
- `skill_tokens`: optional, list of strings; defaults to `[]`
- **Directory name must equal `name` field** (prevents silent cascade duplicates)
- Unknown extra fields: WARNING logged, pack still loaded

## Files Changed

### Created / Rewritten

| File | Change |
|---|---|
| `atelier/subagents.py` | Full rewrite — directory-based `SubagentSpec` + `SubagentRegistry` |
| `config/atelier/subagents/relais-config/subagent.yaml.default` | Migrated from flat `relais-config.yaml.default` |
| `tests/test_subagents_registry.py` | Full rewrite — directory-based fixtures |
| `tests/test_subagents_tools_resolution.py` | Updated — `_write_pack` helper, `tool_tokens` YAML field, `local:` token tests |
| `tests/test_subagents_packs.py` | New — pack-specific tests (importlib isolation, path traversal guard, skill discovery, error containment) |

### Updated

| File | Change |
|---|---|
| `common/init.py` | `DEFAULT_FILES` and `dirs` updated to directory-based paths |
| `atelier/main.py` | Comment updated; file watcher already covers `subagents/` recursively |
| `README.md` | Atelier description updated |
| `plans/SUBAGENTS_YAML_MIGRATION.md` | This file — updated to reflect directory-based design |

### Removed

| File | Reason |
|---|---|
| `config/atelier/subagents/relais-config.yaml.default` | Superseded by `relais-config/subagent.yaml.default` |

## `specs_for_user` output contract

```python
# tools key omitted when empty (not resolved or all tokens dropped)
# skills key omitted when empty
[
    {
        "name": "my-agent",
        "description": "...",
        "system_prompt": "...",
        "tools": [...],   # only present when non-empty
        "skills": [...],  # only present when non-empty
    }
]
```

## Security Properties

- `local:` tool tokens only resolve from `<pack_dir>/tools/` — no arbitrary imports
- `local:` skill tokens are path-traversal-guarded: resolved path must be inside `<pack_dir>/skills/`
- `inherit` yields exactly the tools already allowed by `ToolPolicy` — never widens scope
- Broken module in `tools/`: ERROR logged, module skipped, other modules in same pack still load
- Broken pack directory: ERROR logged, pack skipped, other packs still load
- Module not inserted into `sys.modules` → no cross-subagent module namespace collisions
- Hot-reload: registry reference swapped atomically; in-flight requests keep old ref and complete

## Success Criteria

- [x] Each subagent is a directory `config/atelier/subagents/<name>/` with `subagent.yaml`
- [x] `tools/*.py` modules loaded via importlib, isolated (no sys.modules insertion)
- [x] `skills/*/` directories discovered and resolved to absolute paths
- [x] `local:` tool tokens resolve from pack's loaded tools
- [x] `local:` skill tokens resolve from pack's discovered skills, path-traversal-guarded
- [x] `mcp:<glob>`, `inherit`, bare-name tokens unchanged from previous implementation
- [x] Unknown tokens: WARNING + drop (fail-closed)
- [x] Directory name must equal `name` field (ERROR + skip on mismatch)
- [x] Flat YAML files in `subagents/` are ignored
- [x] `SubagentSpec` is frozen (no callables); `SubagentRegistry` holds runtime dicts
- [x] `specs_for_user` omits `tools` key when empty, omits `skills` key when empty
- [x] `delegation_snippet` optional; auto-generated from description first line when absent
- [x] Hot-reload of `config/atelier/subagents/` via file-watcher (existing, unchanged)
- [x] `relais-config` subagent migrated to directory format, bootstrapped by `initialize_user_dir`
- [x] All new tests passing: `test_subagents_registry.py`, `test_subagents_tools_resolution.py`, `test_subagents_packs.py`
- [x] CLAUDE.md updated (Adding a New Subagent section)
- [x] README.md updated

## Relevant Files

### Core implementation
- `atelier/subagents.py`
- `config/atelier/subagents/relais-config/subagent.yaml.default`
- `common/init.py`

### Tests
- `tests/test_subagents_registry.py`
- `tests/test_subagents_tools_resolution.py`
- `tests/test_subagents_packs.py`

### Context (read-only)
- `atelier/agent_executor.py` (consumes subagent spec dicts)
- `atelier/tool_policy.py` (ToolPolicy.filter_mcp_tools — security boundary)
- `atelier/mcp_adapter.py` (make_mcp_tools — MCP pool source)
- `common/config_loader.py` (cascade resolution)
- `portail.yaml` `allowed_subagents` (fnmatch ACL — unchanged)
