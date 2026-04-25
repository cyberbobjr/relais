"""Directory-based subagent registry for the Atelier brick.

Each subagent is a **directory** under ``config/atelier/subagents/<name>/``
found in the config cascade (user > system > project).  Merges by ``name``
with user-priority: the first occurrence of each name wins.

Directory layout::

    config/atelier/subagents/
    └── my-agent/
        ├── subagent.yaml       # Required — spec (name, description, system_prompt, …)
        ├── tools/              # Optional — Python modules exporting BaseTool instances
        │   ├── search.py
        │   └── write.py
        └── skills/             # Optional — skill directories passed to create_deep_agent()
            └── my-skill/
                └── SKILL.md

YAML Schema (``subagent.yaml``)::

    name: my-agent
    description: |
      Short description — first line is used for auto-generated delegation snippet.
    system_prompt: |
      Full multi-line system prompt …
    tool_tokens: []       # optional; token forms: local:<name>, mcp:<glob>, inherit, <static_name>
    skill_tokens: []      # optional; token forms: local:<name>
    delegation_snippet: |   # optional; auto-generated from description if absent
      - **my-agent**: Custom text …

Token forms for ``tool_tokens``:

- ``local:<name>`` — tool exported from ``tools/<name>.py`` inside this subagent's pack dir
- ``mcp:<glob>`` — fnmatch filter on per-request MCP tools (already ToolPolicy-filtered)
- ``inherit`` — all per-request MCP tools
- ``<bare-name>`` — static tool from the global ``ToolRegistry`` (atelier/tools/*.py)

Token forms for ``skill_tokens``:

- ``local:<name>`` — skill directory ``skills/<name>/`` inside this subagent's pack dir

Validation rules (fail-closed per directory — ERROR log + skip on violation):

- name: required, non-empty, matches [a-z0-9][a-z0-9-]*
- description: required, non-empty
- system_prompt: required, non-empty
- tool_tokens: optional; list of strings (default [])
- skill_tokens: optional; list of strings (default [])
- Directory name must equal name field (prevents silent cascade duplicates)
- Unknown extra fields: WARNING logged, directory still loaded
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from atelier.prompts import SUBAGENT_OPERATIONAL_RULES
from common.config_loader import CONFIG_SEARCH_PATH, get_relais_project_dir, resolve_bundles_dir
from common.pattern_matcher import matches as _matches_patterns
from common.pattern_matcher import parse_patterns as _parse_subagent_patterns
from atelier.subagents_resolver import (  # noqa: F401 — re-exported for test imports
    _ALLOWED_MODULE_PREFIXES,
    _load_tools_from_import,
    _load_tools_from_module,
    _resolve_inherit_tokens,
    _resolve_local_token,
    _resolve_mcp_token,
    _resolve_module_token,
    _resolve_static_token,
    _resolve_tool_tokens,
    _resolve_skill_tokens,
    validate_module_token,
)

logger = logging.getLogger(__name__)

# Path to the native subagents bundled with the source tree (second tier).
# Patched in tests via conftest.isolated_search_path to prevent real packs loading.
NATIVE_SUBAGENTS_PATH: Path = get_relais_project_dir() / "atelier" / "subagents"

# Regex for valid subagent names
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Known YAML fields — extra fields beyond these are warned about
_KNOWN_FIELDS = frozenset({
    "name", "description", "system_prompt",
    "tool_tokens", "skill_tokens", "delegation_snippet",
    "response_format",
})

_DELEGATION_PREAMBLE = """
Subagent delegation:
- You have access to specialized subagents via the task() tool.
- When a request matches one of the subagents below, delegate via task() \
instead of doing it yourself. Delegating keeps your context clean and \
produces better results.
""".strip()


@dataclass(frozen=True)
class SubagentSpec:
    """Immutable metadata spec for a single directory-based subagent.

    Contains only serialisable/hashable values — no callables.  Runtime
    objects (loaded tool callables, resolved skill paths) are stored in
    ``SubagentRegistry`` keyed by ``name``.

    Attributes:
        name: Unique identifier, matches allowed_subagents fnmatch patterns.
        description: Shown to the main agent; first line used for auto-snippet.
        system_prompt: Full prompt injected into the subagent's context.
        tool_tokens: Raw token strings from the YAML ``tool_tokens`` field.
        skill_tokens: Raw token strings from the YAML ``skill_tokens`` field.
        delegation_snippet: Optional custom snippet; auto-generated if None.
        source_path: Filesystem path to ``subagent.yaml`` (for diagnostics).
        pack_dir: Filesystem path to the containing directory (for tool/skill loading).
        response_format: Optional dict with a ``type`` key forwarded to deepagents;
            must include a ``"type"`` key or it is ignored at resolution time.
    """

    name: str
    description: str
    system_prompt: str
    tool_tokens: tuple[str, ...]
    skill_tokens: tuple[str, ...]
    delegation_snippet: str | None
    source_path: Path
    pack_dir: Path
    response_format: dict | None = field(default=None)
    degraded_tokens: tuple[str, ...] = field(default=())



def _load_yaml_file(path: Path) -> dict | None:
    """Load a YAML file and return its contents as a dict.

    Logs ERROR and returns None on parse failure (fail-closed for malformed
    YAML at startup).

    Args:
        path: Filesystem path to the YAML file.

    Returns:
        The parsed dict, or None on failure.
    """
    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            logger.error(
                "SubagentRegistry: %s does not contain a YAML mapping — skipping",
                path,
            )
            return None
        return raw
    except Exception as exc:
        logger.error(
            "SubagentRegistry: failed to parse %s — %s — skipping", path, exc
        )
        return None


def _validate_and_build_spec(data: dict, yaml_path: Path, pack_dir: Path) -> SubagentSpec | None:
    """Validate a raw YAML dict and build a SubagentSpec.

    Logs ERROR and returns None for any missing required field or directory
    name mismatch.  Logs WARNING for unknown extra fields (but still returns
    the spec).

    Args:
        data: The raw YAML dict from ``subagent.yaml``.
        yaml_path: Path to ``subagent.yaml`` (for diagnostics).
        pack_dir: Path to the containing subagent directory.

    Returns:
        A SubagentSpec if valid, None if invalid.
    """
    # Warn about unknown extra fields (but don't reject)
    extra_fields = set(data.keys()) - _KNOWN_FIELDS
    if extra_fields:
        logger.warning(
            "SubagentRegistry: %s has unknown fields %s — ignoring them",
            yaml_path,
            sorted(extra_fields),
        )

    # Required field: name
    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'name' field — skipping", yaml_path
        )
        return None

    name = name.strip()

    # Validate name format
    if not _NAME_RE.match(name):
        logger.error(
            "SubagentRegistry: %s — 'name' field '%s' does not match [a-z0-9][a-z0-9-]* "
            "— skipping",
            yaml_path, name,
        )
        return None

    # Directory name must match the name field
    if pack_dir.name != name:
        logger.error(
            "SubagentRegistry: directory name '%s' != name '%s' in %s — skipping "
            "(rename directory to '%s' to prevent silent cascade duplicates)",
            pack_dir.name, name, yaml_path, name,
        )
        return None

    # Required field: description
    description = data.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'description' field — skipping", yaml_path
        )
        return None

    # Required field: system_prompt
    system_prompt = data.get("system_prompt")
    if not system_prompt or not isinstance(system_prompt, str) or not system_prompt.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'system_prompt' field — skipping",
            yaml_path,
        )
        return None

    # Optional: tool_tokens (list of strings; default [])
    raw_tool_tokens = data.get("tool_tokens") or []
    if not isinstance(raw_tool_tokens, (list, tuple)):
        logger.warning(
            "SubagentRegistry: %s 'tool_tokens' field is not a list — treating as []", yaml_path
        )
        raw_tool_tokens = []
    tool_tokens = tuple(str(t) for t in raw_tool_tokens)

    # Optional: skill_tokens (list of strings; default [])
    raw_skill_tokens = data.get("skill_tokens") or []
    if not isinstance(raw_skill_tokens, (list, tuple)):
        logger.warning(
            "SubagentRegistry: %s 'skill_tokens' field is not a list — treating as []", yaml_path
        )
        raw_skill_tokens = []
    skill_tokens = tuple(str(t) for t in raw_skill_tokens)

    # Optional: delegation_snippet
    delegation_snippet: str | None = data.get("delegation_snippet") or None
    if delegation_snippet and isinstance(delegation_snippet, str):
        delegation_snippet = delegation_snippet.strip() or None

    # Optional: response_format (dict or None)
    response_format_raw = data.get("response_format")
    response_format: dict | None = None
    if response_format_raw is not None:
        if isinstance(response_format_raw, dict):
            response_format = response_format_raw
        else:
            logger.warning(
                "SubagentRegistry: %s 'response_format' is not a dict — ignoring",
                yaml_path,
            )

    return SubagentSpec(
        name=name,
        description=description.strip(),
        system_prompt=system_prompt.strip(),
        tool_tokens=tool_tokens,
        skill_tokens=skill_tokens,
        delegation_snippet=delegation_snippet,
        source_path=yaml_path,
        pack_dir=pack_dir,
        response_format=response_format,
    )




def _load_subagent_tier(
    tier_name: str,
    pack_dirs: list[Path],
    seen_names: set[str],
) -> list[tuple[SubagentSpec, dict[str, Any], dict[str, str]]]:
    """Scan a list of pack directories and load all valid subagent specs.

    Shared scan/load/validate/register logic for all three tiers (user config,
    native, bundle).  Each tier calls this function with its resolved list of
    pack directories; the ``seen_names`` set is updated in-place so that
    higher-priority tiers already registered prevent lower-priority ones from
    overriding them.

    Args:
        tier_name: Human-readable tier label used in log messages (e.g.
            ``"user"``, ``"native"``, ``"bundle"``).  The empty string ``""``
            means the primary user-config tier (log messages omit the prefix).
        pack_dirs: Sorted list of candidate pack directories to inspect.  Each
            entry must be a directory; those without a ``subagent.yaml`` are
            silently skipped at DEBUG level.
        seen_names: Mutable set of subagent names already registered by
            higher-priority tiers.  Updated in-place for every accepted spec.

    Returns:
        A list of ``(spec, local_tools, local_skills)`` triples for every
        pack that passed validation and was not shadowed by a higher-priority
        tier.  The caller is responsible for merging these into the shared
        ``specs``, ``local_tools``, and ``local_skills`` accumulators and
        emitting any cross-tier warning (e.g. F-17 override warnings).
    """
    prefix = f"{tier_name} " if tier_name else ""
    results: list[tuple[SubagentSpec, dict[str, Any], dict[str, str]]] = []

    for pack_dir in pack_dirs:
        yaml_path = pack_dir / "subagent.yaml"
        if not yaml_path.is_file():
            logger.debug(
                "SubagentRegistry: %s has no subagent.yaml — skipping",
                pack_dir,
            )
            continue

        data = _load_yaml_file(yaml_path)
        if data is None:
            continue

        spec = _validate_and_build_spec(data, yaml_path, pack_dir)
        if spec is None:
            continue

        if spec.name in seen_names:
            logger.debug(
                "SubagentRegistry: skipping %s%s — '%s' already loaded "
                "from a higher-priority path",
                prefix, pack_dir, spec.name,
            )
            continue

        tools = _load_local_tools(pack_dir, spec.name)

        skills: dict[str, str] = {}
        skills_dir = pack_dir / "skills"
        if skills_dir.is_dir():
            for skill_dir in sorted(
                p for p in skills_dir.iterdir() if p.is_dir()
            ):
                skills[skill_dir.name] = str(skill_dir.resolve())
                logger.debug(
                    "SubagentRegistry: subagent '%s' — found local skill '%s' at %s",
                    spec.name, skill_dir.name, skill_dir,
                )

        seen_names.add(spec.name)
        results.append((spec, tools, skills))

        label = f"{tier_name} subagent" if tier_name else "subagent"
        logger.info(
            "SubagentRegistry: loaded %s '%s' from %s "
            "(%d local tool(s), %d local skill(s))",
            label, spec.name, pack_dir, len(tools), len(skills),
        )

    return results


def _load_local_tools(pack_dir: Path, spec_name: str) -> dict[str, Any]:
    """Discover and load all tools from a subagent's ``tools/`` directory.

    Each ``.py`` file in ``<pack_dir>/tools/`` is loaded in isolation via
    ``_load_tools_from_module``.  Errors in individual files are logged and
    skipped; other files continue loading.

    Args:
        pack_dir: Subagent pack directory (must contain a ``tools/`` subdirectory
            to have any effect).
        spec_name: Subagent name, used for synthetic module naming and logging.

    Returns:
        A flat dict of ``{tool_name: tool_object}`` from all modules combined.
        Later modules overwrite earlier ones on name collision (alphabetical
        file order; WARNING logged on collision).
    """
    tools_dir = pack_dir / "tools"
    if not tools_dir.is_dir():
        return {}

    combined: dict[str, Any] = {}
    for py_file in sorted(tools_dir.glob("*.py")):
        loaded = _load_tools_from_module(py_file, spec_name)
        for tool_name, tool_obj in loaded.items():
            if tool_name in combined:
                logger.warning(
                    "SubagentRegistry: subagent '%s' — tool name '%s' defined in "
                    "multiple modules; %s overrides previous",
                    spec_name, tool_name, py_file.name,
                )
            combined[tool_name] = tool_obj
            logger.debug(
                "SubagentRegistry: subagent '%s' — loaded local tool '%s' from %s",
                spec_name, tool_name, py_file,
            )
    return combined



@dataclass
class SubagentRegistry:
    """Registry of subagent specs loaded from pack directories.

    Built once at Atelier startup via ``load(tool_registry)``, then
    queried per-request.  Hot-reloads swap the registry reference
    atomically under the config lock.

    ``_local_tools_by_subagent`` and ``_local_skills_by_subagent`` hold
    the runtime-loaded objects (callables and paths); these are kept
    separate from the frozen ``SubagentSpec`` metadata to maintain
    hashability of specs.

    Attributes:
        _specs: Tuple of all loaded SubagentSpec instances (unique by name).
            Immutable after construction — rebuilt atomically by
            ``_validate_tool_tokens()`` (called once inside ``load()`` before
            the registry is shared with any caller).
        _tool_registry: The static ToolRegistry used for bare-name token resolution.
        _local_tools_by_subagent: Dict mapping subagent name → {tool_name → callable}.
        _local_skills_by_subagent: Dict mapping subagent name → {skill_name → abs_path}.
        _runtime_degraded: Dict mapping subagent name → frozenset of token strings
            that failed during ``specs_for_user()`` resolution.  Accumulated
            across requests; never reset except by a new ``load()`` call that
            replaces the registry reference entirely.
    """

    _specs: tuple[SubagentSpec, ...]
    _tool_registry: Any  # ToolRegistry — avoid circular import at type-check time
    _local_tools_by_subagent: dict[str, dict[str, Any]] = field(default_factory=dict)
    _local_skills_by_subagent: dict[str, dict[str, str]] = field(default_factory=dict)
    _runtime_degraded: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, tool_registry: Any) -> SubagentRegistry:
        """Walk the config cascade and load all valid subagent pack directories.

        Scans ``config/atelier/subagents/*/`` under each root in
        ``CONFIG_SEARCH_PATH`` (user > system > project).  Each subdirectory
        must contain a ``subagent.yaml`` file.  The first occurrence of each
        subagent name wins (user priority).

        For each valid subagent directory:
        - ``tools/*.py`` modules are loaded via importlib (isolated, no sys.modules)
        - ``skills/<name>/`` directories are resolved to absolute paths

        Malformed directories and files failing validation are logged and
        skipped; startup always continues.

        Args:
            tool_registry: A ``ToolRegistry`` instance used later for
                static tool token resolution.

        Returns:
            A ``SubagentRegistry`` populated with all valid specs and their
            locally loaded tools and skills.
        """
        seen_names: set[str] = set()
        specs: list[SubagentSpec] = []
        local_tools: dict[str, dict[str, Any]] = {}
        local_skills: dict[str, dict[str, str]] = {}

        # --- Tier 1: user config cascade (user > system > project) ---
        user_pack_dirs: list[Path] = []
        for base in CONFIG_SEARCH_PATH:
            subagents_dir = base / "config" / "atelier" / "subagents"
            if subagents_dir.is_dir():
                user_pack_dirs.extend(
                    sorted(p for p in subagents_dir.iterdir() if p.is_dir())
                )
        for spec, tools, skills in _load_subagent_tier("", user_pack_dirs, seen_names):
            specs.append(spec)
            local_tools[spec.name] = tools
            local_skills[spec.name] = skills

        # Scan native subagents (second tier — user packs already take priority)
        native_pack_dirs: list[Path] = []
        if NATIVE_SUBAGENTS_PATH.is_dir():
            native_pack_dirs = sorted(
                p for p in NATIVE_SUBAGENTS_PATH.iterdir() if p.is_dir()
            )
        for spec, tools, skills in _load_subagent_tier("native", native_pack_dirs, seen_names):
            specs.append(spec)
            local_tools[spec.name] = tools
            local_skills[spec.name] = skills

        # F-17: snapshot registered names before bundle tier to detect overrides
        names_before_bundles = frozenset(seen_names)

        # Scan bundle subagents (third tier — user and native packs take priority)
        bundle_pack_dirs: list[Path] = []
        bundles_dir = resolve_bundles_dir()
        if bundles_dir.is_dir():
            for bundle_dir in sorted(p for p in bundles_dir.iterdir() if p.is_dir()):
                bundle_subagents_dir = bundle_dir / "subagents"
                if bundle_subagents_dir.is_dir():
                    bundle_pack_dirs.extend(
                        sorted(p for p in bundle_subagents_dir.iterdir() if p.is_dir())
                    )

        # F-17: warn for every bundle subagent shadowed by a higher-priority tier
        for pack_dir in bundle_pack_dirs:
            yaml_path = pack_dir / "subagent.yaml"
            if not yaml_path.is_file():
                continue
            data = _load_yaml_file(yaml_path)
            if data is None or not isinstance(data.get("name"), str):
                continue
            bundle_name = data["name"].strip()
            if bundle_name in names_before_bundles:
                logger.warning(
                    "SubagentRegistry: subagent '%s' from bundle at %s is shadowed "
                    "by a higher-priority user or native subagent with the same name",
                    bundle_name,
                    pack_dir,
                )

        for spec, tools, skills in _load_subagent_tier("bundle", bundle_pack_dirs, seen_names):
            specs.append(spec)
            local_tools[spec.name] = tools
            local_skills[spec.name] = skills

        logger.info("SubagentRegistry: %d subagent(s) loaded", len(specs))
        registry = cls(
            _specs=tuple(specs),
            _tool_registry=tool_registry,
            _local_tools_by_subagent=local_tools,
            _local_skills_by_subagent=local_skills,
        )
        registry._validate_tool_tokens()
        return registry

    @property
    def all_names(self) -> frozenset[str]:
        """Return all registered subagent names.

        Returns:
            A frozenset of subagent name strings.
        """
        return frozenset(s.name for s in self._specs)

    @property
    def degraded_names(self) -> frozenset[str]:
        """Return names of subagents that have at least one invalid tool token.

        A subagent is considered degraded when one or more of its
        statically-resolvable tool tokens could not be resolved — either
        at startup (bare names / module: tokens) or at runtime (local:
        tokens dropped by ``_resolve_tool_tokens``).

        Returns:
            A frozenset of subagent name strings whose ``degraded_tokens``
            tuple is non-empty, or that have runtime failures in
            ``_runtime_degraded``.
        """
        startup = frozenset(s.name for s in self._specs if s.degraded_tokens)
        return startup | frozenset(self._runtime_degraded)

    def _validate_tool_tokens(self) -> None:
        """Validate statically-resolvable tool tokens at startup.

        Iterates all loaded specs and checks:
        - bare ``<name>`` tokens against ``_tool_registry``
        - ``module:<dotted.path>`` tokens via ``validate_module_token()``

        Skips ``mcp:``, ``inherit``, and ``local:`` tokens — these are
        dynamic or validated at runtime.

        Rebuilds ``_specs`` as a new tuple (immutability preserved) where
        any spec with invalid tokens has its ``degraded_tokens`` field set
        to the tuple of unresolvable token strings.  Logs a WARNING for
        each failure.
        """
        updated: list[SubagentSpec] = []
        for spec in self._specs:
            failed: list[str] = []
            for token in spec.tool_tokens:
                if token == "inherit" or token.startswith("mcp:") or token.startswith("local:"):
                    continue
                if token.startswith("module:"):
                    module_path = token[len("module:"):]
                    error = validate_module_token(module_path, spec.name)
                    if error is not None:
                        logger.warning(
                            "SubagentRegistry: subagent '%s' — token '%s' invalid at "
                            "startup: %s — marking as degraded",
                            spec.name, token, error,
                        )
                        failed.append(token)
                else:
                    # Bare static name — global ToolRegistry, then local pack fallback
                    tool = self._tool_registry.get(token)
                    if tool is None:
                        local_tools = self._local_tools_by_subagent.get(spec.name, {})
                        if local_tools.get(token) is not None:
                            logger.debug(
                                "SubagentRegistry: subagent '%s' — bare token '%s' not in "
                                "ToolRegistry at startup, resolved via local pack fallback",
                                spec.name, token,
                            )
                        else:
                            logger.warning(
                                "SubagentRegistry: subagent '%s' — static tool '%s' not "
                                "found in ToolRegistry or local pack at startup — marking as degraded",
                                spec.name, token,
                            )
                            failed.append(token)

            if failed:
                spec = dataclasses.replace(spec, degraded_tokens=tuple(failed))
            updated.append(spec)
        self._specs = tuple(updated)

    def _filter_for_user(self, user_record: dict[str, Any]) -> list[SubagentSpec]:
        """Return specs allowed for the given user record.

        Args:
            user_record: The user record dict stamped by Portail.

        Returns:
            Filtered list of SubagentSpec instances.
        """
        patterns = _parse_subagent_patterns(user_record.get("allowed_subagents"))
        if not patterns:
            return []
        return [s for s in self._specs if _matches_patterns(s.name, patterns)]

    def specs_for_user(
        self,
        user_record: dict[str, Any],
        request_tools: list | None = None,
        project_context: str = "",
    ) -> list[dict[str, Any]]:
        """Return deepagents-compatible spec dicts filtered by user ACL.

        Tool tokens are resolved using *request_tools* (the per-request MCP
        tool pool, already filtered by ``ToolPolicy``) and the static
        ``ToolRegistry``.  Skill tokens are resolved to absolute path strings.

        Tool token forms:
        - ``local:<name>`` → tool loaded from ``tools/<name>.py`` in the pack dir
        - ``mcp:<glob>`` → fnmatch filter on request_tools names
        - ``inherit`` → all request_tools
        - ``<name>`` (no prefix) → ``tool_registry.get(name)``; WARNING + drop if missing

        Skill token forms:
        - ``local:<name>`` → absolute path to ``skills/<name>/`` in the pack dir

        Args:
            user_record: The user record dict stamped by Portail.
            request_tools: Per-request list of ``BaseTool`` instances (MCP
                tools filtered by ``ToolPolicy``).  Defaults to ``[]``.
            project_context: Pre-built project environment block (RELAIS_HOME,
                RELAIS_PROJECT_DIR) to append to each subagent's system prompt.
                Empty string skips injection.

        Returns:
            A list of dicts with keys ``name``, ``description``,
            ``system_prompt``, and optionally ``tools`` and ``skills`` —
            ready for ``create_deep_agent(subagents=...)``.
            ``tools`` is omitted when empty; ``skills`` is omitted when empty.
        """
        if request_tools is None:
            request_tools = []

        allowed_specs = self._filter_for_user(user_record)
        result: list[dict[str, Any]] = []

        for spec in allowed_specs:
            local_tools = self._local_tools_by_subagent.get(spec.name, {})
            resolved_tools, failed_tokens = _resolve_tool_tokens(
                spec.tool_tokens, request_tools, self._tool_registry,
                local_tools, spec.name,
            )

            # Track runtime failures in _runtime_degraded (never mutates _specs)
            if failed_tokens:
                existing = self._runtime_degraded.get(spec.name, frozenset())
                self._runtime_degraded[spec.name] = existing | frozenset(failed_tokens)

            local_skills = self._local_skills_by_subagent.get(spec.name, {})
            resolved_skills = _resolve_skill_tokens(
                spec.skill_tokens, local_skills, spec.name
            )

            enriched_prompt = spec.system_prompt
            if project_context and project_context not in enriched_prompt:
                enriched_prompt = f"{enriched_prompt}\n\n{project_context}"
            if SUBAGENT_OPERATIONAL_RULES not in enriched_prompt:
                enriched_prompt = f"{enriched_prompt}\n\n{SUBAGENT_OPERATIONAL_RULES}"
            entry: dict[str, Any] = {
                "name": spec.name,
                "description": spec.description,
                "system_prompt": enriched_prompt,
            }
            if resolved_tools:
                entry["tools"] = resolved_tools
            if resolved_skills:
                entry["skills"] = resolved_skills
            if spec.response_format is not None:
                if "type" not in spec.response_format:
                    logger.warning(
                        "SubagentRegistry: subagent '%s' — response_format missing 'type' "
                        "key — ignoring",
                        spec.name,
                    )
                else:
                    entry["response_format"] = spec.response_format

            result.append(entry)

        return result

    def delegation_prompt_for_user(self, user_record: dict[str, Any]) -> str:
        """Assemble the delegation prompt for the given user.

        Prepends ``_DELEGATION_PREAMBLE``, then appends one snippet per
        allowed subagent.  Returns ``""`` if no subagents match — callers
        should skip appending to the system prompt in that case.

        Args:
            user_record: The user record dict stamped by Portail.

        Returns:
            The full delegation prompt string, or ``""`` if no subagents.
        """
        allowed_specs = self._filter_for_user(user_record)
        if not allowed_specs:
            return ""

        snippets: list[str] = []
        for spec in allowed_specs:
            if spec.delegation_snippet:
                snippets.append(spec.delegation_snippet)
            else:
                # Auto-generate from first line of description
                first_line = spec.description.splitlines()[0].strip()
                snippets.append(f"- **{spec.name}**: {first_line}")

        if not snippets:
            return ""

        return f"{_DELEGATION_PREAMBLE}\n\n" + "\n".join(snippets)


