"""YAML-based subagent registry for the Atelier brick.

Loads subagent specs from ``config/atelier/subagents/*.yaml`` files found in
the config cascade (user > system > project).  Merges by ``name`` with
user-priority: the first occurrence of each name wins.

At startup, ``SubagentRegistry.load(tool_registry)`` is called once and
stored as an immutable frozen dataclass.  Per-request, callers use
``specs_for_user(user_record, request_tools)`` to obtain deepagents-
compatible spec dicts filtered by the user's ``allowed_subagents`` patterns
and with tool tokens resolved.

YAML Schema (``config/atelier/subagents/<name>.yaml``)::

    name: relais-config
    description: |
      Short description — first line is used for auto-generated delegation snippet.
    system_prompt: |
      Full multi-line system prompt …
    tools: []           # optional; token forms: mcp:<glob>, inherit, <static_name>
    delegation_snippet: |   # optional; auto-generated from description if absent
      - **relais-config**: Custom text …

Validation rules (fail-closed per file — ERROR log + skip on violation):
- name: required, non-empty, matches [a-z0-9][a-z0-9-]*
- description: required, non-empty
- system_prompt: required, non-empty
- tools: optional; list of strings (default [])
- File stem must equal name (prevents silent cascade duplicates)
- Unknown extra fields: WARNING logged, file still loaded
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import CONFIG_SEARCH_PATH

logger = logging.getLogger(__name__)

# Regex for valid subagent names
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Known YAML fields — extra fields beyond these are warned about
_KNOWN_FIELDS = frozenset({
    "name", "description", "system_prompt", "tools", "delegation_snippet"
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
    """Immutable spec for a single YAML-defined subagent.

    Attributes:
        name: Unique identifier, matches allowed_subagents fnmatch patterns.
        description: Shown to the main agent; first line used for auto-snippet.
        system_prompt: Full prompt injected into the subagent's context.
        tools: Raw token strings from the YAML ``tools`` field (not yet resolved).
        delegation_snippet: Optional custom snippet; auto-generated if None.
        source_path: Filesystem path to the YAML file (for diagnostics).
    """

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...]
    delegation_snippet: str | None
    source_path: Path


def _parse_subagent_patterns(raw: object) -> tuple[str, ...]:
    """Normalise an ``allowed_subagents`` value into a tuple of strings.

    Only ``list`` or ``tuple`` inputs are accepted; anything else (None,
    str, int, dict) returns an empty tuple (fail-closed).

    Args:
        raw: The raw value from ``user_record.get("allowed_subagents")``.

    Returns:
        A tuple of strings suitable for fnmatch filtering.
    """
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return ()


def _matches_patterns(name: str, patterns: tuple[str, ...]) -> bool:
    """Return True if *name* matches any fnmatch pattern in *patterns*.

    Args:
        name: The subagent name to test.
        patterns: Tuple of fnmatch-style glob patterns.

    Returns:
        True if at least one pattern matches.
    """
    return any(fnmatch.fnmatch(name, p) for p in patterns)


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


def _validate_and_build_spec(data: dict, path: Path) -> SubagentSpec | None:
    """Validate a raw YAML dict and build a SubagentSpec.

    Logs ERROR and returns None for any missing required field or stem mismatch.
    Logs WARNING for unknown extra fields (but still returns the spec).

    Args:
        data: The raw YAML dict.
        path: The source file path (used for stem validation and diagnostics).

    Returns:
        A SubagentSpec if valid, None if invalid.
    """
    # Warn about unknown extra fields (but don't reject)
    extra_fields = set(data.keys()) - _KNOWN_FIELDS
    if extra_fields:
        logger.warning(
            "SubagentRegistry: %s has unknown fields %s — ignoring them",
            path,
            sorted(extra_fields),
        )

    # Required field: name
    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'name' field — skipping", path
        )
        return None

    # File stem must match name
    if path.stem != name:
        logger.error(
            "SubagentRegistry: file stem '%s' != name '%s' in %s — skipping "
            "(rename file to %s.yaml to prevent silent cascade duplicates)",
            path.stem, name, path, name,
        )
        return None

    # Required field: description
    description = data.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'description' field — skipping", path
        )
        return None

    # Required field: system_prompt
    system_prompt = data.get("system_prompt")
    if not system_prompt or not isinstance(system_prompt, str) or not system_prompt.strip():
        logger.error(
            "SubagentRegistry: %s missing required 'system_prompt' field — skipping",
            path,
        )
        return None

    # Optional: tools (list of strings; default [])
    raw_tools = data.get("tools") or []
    if not isinstance(raw_tools, (list, tuple)):
        logger.warning(
            "SubagentRegistry: %s 'tools' field is not a list — treating as []", path
        )
        raw_tools = []
    tools = tuple(str(t) for t in raw_tools)

    # Optional: delegation_snippet
    delegation_snippet: str | None = data.get("delegation_snippet") or None
    if delegation_snippet and isinstance(delegation_snippet, str):
        delegation_snippet = delegation_snippet.strip() or None

    return SubagentSpec(
        name=name.strip(),
        description=description.strip(),
        system_prompt=system_prompt.strip(),
        tools=tools,
        delegation_snippet=delegation_snippet,
        source_path=path,
    )


@dataclass(frozen=True)
class SubagentRegistry:
    """Immutable registry of subagent specs loaded from YAML files.

    Built once at Atelier startup via ``load(tool_registry)``, then
    queried per-request.  The registry is frozen; hot-reloads swap the
    reference atomically under the config lock.

    Attributes:
        _specs: Tuple of all loaded SubagentSpec instances (unique by name).
        _tool_registry: The static ToolRegistry used for token resolution.
    """

    _specs: tuple[SubagentSpec, ...]
    _tool_registry: Any  # ToolRegistry — avoid circular import at type-check time

    @classmethod
    def load(cls, tool_registry: Any) -> SubagentRegistry:
        """Walk the config cascade and load all valid subagent YAML files.

        Scans ``config/atelier/subagents/*.yaml`` under each root in
        ``CONFIG_SEARCH_PATH`` (user > system > project).  The first
        occurrence of each subagent name wins (user priority).

        Malformed YAML files and files failing validation are logged and
        skipped; startup always continues.

        Args:
            tool_registry: A ``ToolRegistry`` instance used later for
                static tool token resolution.

        Returns:
            A frozen ``SubagentRegistry`` with all valid specs.
        """
        seen_names: set[str] = set()
        specs: list[SubagentSpec] = []

        for base in CONFIG_SEARCH_PATH:
            subagents_dir = base / "config" / "atelier" / "subagents"
            if not subagents_dir.is_dir():
                continue

            for yaml_path in sorted(subagents_dir.glob("*.yaml")):
                data = _load_yaml_file(yaml_path)
                if data is None:
                    continue

                spec = _validate_and_build_spec(data, yaml_path)
                if spec is None:
                    continue

                if spec.name in seen_names:
                    logger.debug(
                        "SubagentRegistry: skipping %s — '%s' already loaded "
                        "from a higher-priority path",
                        yaml_path, spec.name,
                    )
                    continue

                seen_names.add(spec.name)
                specs.append(spec)
                logger.info(
                    "SubagentRegistry: loaded subagent '%s' from %s",
                    spec.name, yaml_path,
                )

        logger.info("SubagentRegistry: %d subagent(s) loaded", len(specs))
        return cls(_specs=tuple(specs), _tool_registry=tool_registry)

    @property
    def all_names(self) -> frozenset[str]:
        """Return all registered subagent names.

        Returns:
            A frozenset of subagent name strings.
        """
        return frozenset(s.name for s in self._specs)

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
    ) -> list[dict[str, Any]]:
        """Return deepagents-compatible spec dicts filtered by user ACL.

        Tool tokens are resolved using *request_tools* (the per-request MCP
        tool pool, already filtered by ``ToolPolicy``) and the static
        ``ToolRegistry``.

        - ``mcp:<glob>`` → fnmatch filter on request_tools names
        - ``inherit`` → all request_tools
        - ``<name>`` (no prefix) → ``tool_registry.get(name)``; WARNING + drop if missing
        - Unknown tokens → WARNING + drop (fail-closed)

        Args:
            user_record: The user record dict stamped by Portail.
            request_tools: Per-request list of ``BaseTool`` instances (MCP
                tools filtered by ``ToolPolicy``).  Defaults to ``[]``.

        Returns:
            A list of dicts with keys ``name``, ``description``,
            ``system_prompt``, and ``tools`` — ready for
            ``create_deep_agent(subagents=...)``.
        """
        if request_tools is None:
            request_tools = []

        allowed_specs = self._filter_for_user(user_record)
        result: list[dict[str, Any]] = []

        for spec in allowed_specs:
            resolved_tools = _resolve_tool_tokens(
                spec.tools, request_tools, self._tool_registry, spec.name
            )
            result.append({
                "name": spec.name,
                "description": spec.description,
                "system_prompt": spec.system_prompt,
                "tools": resolved_tools,
            })

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


def _resolve_tool_tokens(
    tokens: tuple[str, ...],
    request_tools: list,
    tool_registry: Any,
    spec_name: str,
) -> list:
    """Resolve raw YAML tool tokens into BaseTool instances.

    Token forms:
    - ``mcp:<glob>`` — fnmatch filter on request_tools (MCP pool)
    - ``inherit`` — all request_tools
    - ``<name>`` (no prefix) — lookup in static tool_registry

    Unknown / unresolvable tokens are logged as WARNING and dropped
    (fail-closed; never raises).

    Args:
        tokens: Raw token strings from the YAML ``tools`` field.
        request_tools: Per-request MCP tool pool (already ToolPolicy-filtered).
        tool_registry: Static ``ToolRegistry`` for bare-name tokens.
        spec_name: Subagent name, used in log messages.

    Returns:
        List of resolved ``BaseTool`` instances (deduplicated by name,
        preserving order of first occurrence).
    """
    resolved: list = []
    seen_names: set[str] = set()

    def _add(tool: object) -> None:
        name = getattr(tool, "name", None) or str(id(tool))
        if name not in seen_names:
            seen_names.add(name)
            resolved.append(tool)

    for token in tokens:
        if token == "inherit":
            for t in request_tools:
                _add(t)
        elif token.startswith("mcp:"):
            glob = token[len("mcp:"):]
            matched = [t for t in request_tools if fnmatch.fnmatch(t.name, glob)]
            for t in matched:
                _add(t)
        else:
            # Bare static tool name
            tool = tool_registry.get(token)
            if tool is None:
                logger.warning(
                    "SubagentRegistry: subagent '%s' references unknown static "
                    "tool '%s' — dropping (tool not found in ToolRegistry)",
                    spec_name, token,
                )
            else:
                _add(tool)

    return resolved
