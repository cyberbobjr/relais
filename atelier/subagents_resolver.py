"""Resolver helpers for atelier.subagents — tool and skill token resolution.

This module contains the pure functions used by ``SubagentRegistry.specs_for_user``
to turn raw YAML token strings into callable tool instances and absolute skill paths.
It also provides the module-import helpers used by ``_load_local_tools``.

Extracted from ``atelier.subagents`` to keep that file under the 800-line limit.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import logging
import types
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module prefixes allowed for 'module:<dotted.path>' tool tokens.
# Only these namespaces may be dynamically imported to prevent arbitrary
# code execution from untrusted subagent YAML files.
_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "aiguilleur.channels.",
    "atelier.tools.",
    "relais_tools.",
)


def _load_tools_from_import(module_path: str, spec_name: str) -> dict[str, Any]:
    """Load BaseTool instances from a dotted module path string.

    Only module paths with a prefix listed in ``_ALLOWED_MODULE_PREFIXES`` are
    imported.  Any other prefix is rejected with a WARNING and returns an
    empty dict (fail-closed security boundary).

    Collects all module-level attributes that duck-type as BaseTool instances
    (have ``name`` and ``run`` attributes).

    Args:
        module_path: Dotted Python module path, e.g. ``atelier.tools.my_tool``.
        spec_name: Subagent name used in log messages.

    Returns:
        Dict mapping tool name to tool instance.  Empty dict on any error or
        security rejection.
    """
    if not any(module_path.startswith(prefix) for prefix in _ALLOWED_MODULE_PREFIXES):
        logger.warning(
            "SubagentRegistry: subagent '%s' — module: token '%s' uses a disallowed "
            "prefix — dropping (allowed prefixes: %s)",
            spec_name, module_path, _ALLOWED_MODULE_PREFIXES,
        )
        return {}

    try:
        import importlib
        module = importlib.import_module(module_path)
    except Exception as exc:
        logger.error(
            "SubagentRegistry: subagent '%s' — failed to import module '%s' — %s — skipping",
            spec_name, module_path, exc,
        )
        return {}

    tools: dict[str, Any] = {}
    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name, None)
        if obj is None:
            continue
        tool_name = getattr(obj, "name", None)
        if tool_name and hasattr(obj, "run") and callable(getattr(obj, "run", None)):
            tools[str(tool_name)] = obj
    return tools


def _load_tools_from_module(py_path: Path, spec_name: str) -> dict[str, Any]:
    """Load all BaseTool instances from a Python module file.

    Uses ``importlib.util.spec_from_file_location`` with a synthetic module
    name to avoid ``sys.modules`` collisions across subagents.  Each file is
    isolated: module objects are not inserted into ``sys.modules``.

    All module-level attributes that are ``BaseTool`` instances (duck-typed:
    have ``name`` and ``run`` attributes) are collected.

    Args:
        py_path: Absolute path to the ``.py`` file.
        spec_name: Subagent name used to build a unique synthetic module name.

    Returns:
        A dict mapping tool names to their callable/tool instances.
        Empty dict on any import error (fail-closed; ERROR logged).
    """
    synthetic_name = f"relais_subagent_{spec_name}_{py_path.stem}"
    try:
        mod_spec = importlib.util.spec_from_file_location(synthetic_name, py_path)
        if mod_spec is None or mod_spec.loader is None:
            logger.error(
                "SubagentRegistry: could not create module spec for %s — skipping", py_path
            )
            return {}
        module = types.ModuleType(synthetic_name)
        mod_spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        logger.error(
            "SubagentRegistry: failed to import tools from %s — %s — skipping", py_path, exc
        )
        return {}

    tools: dict[str, Any] = {}
    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name, None)
        if obj is None:
            continue
        # Duck-type: BaseTool has .name and .run
        tool_name = getattr(obj, "name", None)
        if tool_name and hasattr(obj, "run") and callable(getattr(obj, "run", None)):
            tools[str(tool_name)] = obj
    return tools


def validate_module_token(module_path: str, spec_name: str) -> str | None:
    """Validate a ``module:`` token at startup.

    Checks that the module uses an allowed prefix and that it can be
    imported and exports at least one ``BaseTool`` instance.

    Note: This function calls ``_load_tools_from_import``, which uses
    ``importlib.import_module`` and therefore inserts the module into
    ``sys.modules``.  This is intentional — the module is expected to
    remain importable for the lifetime of the process.

    Args:
        module_path: Dotted Python module path extracted from the token
            (e.g. ``aiguilleur.channels.whatsapp.tools``).
        spec_name: Subagent name used in log messages.

    Returns:
        ``None`` if the token is valid (importable, exports ≥ 1 BaseTool).
        An error string describing the problem otherwise.
    """
    if not any(module_path.startswith(prefix) for prefix in _ALLOWED_MODULE_PREFIXES):
        return (
            f"module '{module_path}' uses a disallowed prefix "
            f"(allowed prefixes: {_ALLOWED_MODULE_PREFIXES})"
        )

    tools = _load_tools_from_import(module_path, spec_name)
    if not tools:
        return f"module '{module_path}' is not importable or exports no BaseTools"

    return None


def _resolve_tool_tokens(
    tokens: tuple[str, ...],
    request_tools: list,
    tool_registry: Any,
    local_tools: dict[str, Any],
    spec_name: str,
) -> tuple[list, list[str]]:
    """Resolve raw YAML tool_tokens into callable/BaseTool instances.

    Token forms:
    - ``local:<name>`` — tool loaded from pack's tools/ dir
    - ``mcp:<glob>`` — fnmatch filter on request_tools (MCP pool)
    - ``inherit`` — all request_tools
    - ``module:<dotted.path>`` — import a Python module and collect BaseTool instances
    - ``<name>`` (no prefix) — lookup in static tool_registry

    Unknown / unresolvable tokens are logged as WARNING and dropped
    (fail-closed; never raises).

    ``mcp:`` and ``inherit`` tokens are never considered failures — they are
    dynamic and their resolution depends on the per-request MCP pool.

    Args:
        tokens: Raw token strings from the YAML ``tool_tokens`` field.
        request_tools: Per-request MCP tool pool (already ToolPolicy-filtered).
        tool_registry: Static ``ToolRegistry`` for bare-name tokens.
        local_tools: Dict of locally loaded tools for this subagent.
        spec_name: Subagent name, used in log messages.

    Returns:
        A 2-tuple ``(resolved, failed_tokens)`` where *resolved* is a list
        of tool/callable instances (deduplicated by name, preserving order of
        first occurrence) and *failed_tokens* is a list of token strings that
        could not be resolved (bare names missing from ToolRegistry,
        unresolvable ``module:`` tokens, and ``local:`` tools not found in the
        pack's tools/ dir).
    """
    resolved: list = []
    failed_tokens: list[str] = []
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
        elif token.startswith("local:"):
            tool_name = token[len("local:"):]
            tool = local_tools.get(tool_name)
            if tool is None:
                logger.warning(
                    "SubagentRegistry: subagent '%s' references unknown local "
                    "tool '%s' — dropping (not found in pack's tools/ dir)",
                    spec_name, tool_name,
                )
                failed_tokens.append(token)
            else:
                _add(tool)
        elif token.startswith("mcp:"):
            glob = token[len("mcp:"):]
            matched = [t for t in request_tools if fnmatch.fnmatch(t.name, glob)]
            for t in matched:
                _add(t)
        elif token.startswith("module:"):
            module_path = token[len("module:"):]
            imported_tools = _load_tools_from_import(module_path, spec_name)
            if not imported_tools:
                logger.warning(
                    "SubagentRegistry: subagent '%s' — module: token '%s' "
                    "resolved to zero tools at runtime — dropping",
                    spec_name, token,
                )
                failed_tokens.append(token)
            else:
                for t in imported_tools.values():
                    _add(t)
        else:
            # Bare static tool name — global ToolRegistry, then local fallback
            tool = tool_registry.get(token)
            if tool is None:
                tool = local_tools.get(token)
                if tool is None:
                    logger.warning(
                        "SubagentRegistry: subagent '%s' references unknown static "
                        "tool '%s' — dropping (tool not found in ToolRegistry or local pack)",
                        spec_name, token,
                    )
                    failed_tokens.append(token)
                else:
                    logger.debug(
                        "SubagentRegistry: subagent '%s' — bare token '%s' resolved via "
                        "local pack fallback",
                        spec_name, token,
                    )
                    _add(tool)
            else:
                _add(tool)

    return resolved, failed_tokens


def _resolve_skill_tokens(
    tokens: tuple[str, ...],
    local_skills: dict[str, str],
    spec_name: str,
) -> list[str]:
    """Resolve raw YAML skill_tokens into absolute path strings.

    Token forms:
    - ``local:<name>`` — skill directory inside the pack's skills/ dir

    Unknown / unresolvable tokens are logged as WARNING and dropped
    (fail-closed; never raises).

    Args:
        tokens: Raw token strings from the YAML ``skill_tokens`` field.
        local_skills: Dict of {skill_name → abs_path} for this subagent.
        spec_name: Subagent name, used in log messages.

    Returns:
        List of resolved absolute path strings (deduplicated, order preserved).
    """
    resolved: list[str] = []
    seen: set[str] = set()

    for token in tokens:
        if token.startswith("local:"):
            skill_name = token[len("local:"):]
            path = local_skills.get(skill_name)
            if path is None:
                logger.warning(
                    "SubagentRegistry: subagent '%s' references unknown local "
                    "skill '%s' — dropping (not found in pack's skills/ dir)",
                    spec_name, skill_name,
                )
            elif path not in seen:
                seen.add(path)
                resolved.append(path)
        else:
            logger.warning(
                "SubagentRegistry: subagent '%s' — unknown skill token form '%s' — dropping",
                spec_name, token,
            )

    return resolved
