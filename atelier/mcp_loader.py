"""MCP server configuration loader for the Atelier brick.

Reads MCP (Model Context Protocol) server definitions from a YAML configuration
file following the standard config cascade:
    ~/.relais/config/ > /opt/relais/config/ > ./config/

MCP is optional — when no config file is found the loader returns an empty list
rather than raising, ensuring graceful degradation.

Also loads subagent definitions from the same YAML file and converts them to
AgentDefinition instances for the claude-agent-sdk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from claude_agent_sdk import AgentDefinition  # type: ignore[import-untyped]
except ImportError:
    AgentDefinition = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerConfig:
    """Immutable configuration for a single MCP server entry.

    Attributes:
        name: Human-readable identifier for the server.
        type: Transport type — "stdio" or "sse".
        command: Executable to spawn (e.g. "npx").
        args: Command-line arguments passed to the executable.
        env: Environment variables injected into the subprocess.
            Values are kept verbatim (e.g. "${MY_VAR}") and resolved at
            subprocess spawn time, not at load time.
    """

    name: str
    type: str
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentConfig:
    """Immutable configuration for a single subagent entry.

    Subagents are specialized sub-agents invoked by the principal agent via
    the Task tool. They inherit the parent model and all MCP servers by default.

    Attributes:
        name: Unique identifier used as the dict key in AgentDefinition mapping.
        description: Human-readable role description. Used to generate the
            subagent's system prompt.
        tools: Optional tuple of tool names to restrict this subagent to. When
            None, the subagent may use all available tools.
    """

    name: str
    description: str
    tools: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Config cascade paths
# ---------------------------------------------------------------------------

_CASCADE_DIRS: list[Path] = [
    Path.home() / ".relais" / "config",
    Path("/opt/relais/config"),
    Path("./config"),
]

_FILENAME = "mcp_servers.yaml"
_CONFIG_FILENAME = "config.yaml"


def _find_in_cascade(filename: str) -> Path | None:
    """Locate the first file matching filename in the config cascade.

    Args:
        filename: The filename to search for in each cascade directory.

    Returns:
        Path to the first existing file found, or None when no file is found
        in any cascade directory.
    """
    for directory in _CASCADE_DIRS:
        candidate = directory / filename
        if candidate.exists():
            return candidate
    return None


def _find_config_file() -> Path | None:
    """Locate the first mcp_servers.yaml in the config cascade.

    Returns:
        Path to the first existing mcp_servers.yaml, or None when no file
        is found in any cascade directory (MCP config is optional).
    """
    return _find_in_cascade(_FILENAME)


def _find_config_yaml() -> Path | None:
    """Locate the first config.yaml in the config cascade.

    Returns:
        Path to the first existing config.yaml, or None when no file
        is found in any cascade directory.
    """
    return _find_in_cascade(_CONFIG_FILENAME)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_mcp_servers(
    profile_name: str | None = None,
    config_path: str | Path | None = None,
) -> list[McpServerConfig]:
    """Load MCP server definitions filtered by enabled state and profile.

    When config_path is given, reads that file directly (useful for tests).
    Otherwise, walks the standard cascade: ~/.relais/config/ > /opt/relais/config/
    > ./config/.

    Selection rules:
    - ``global`` section: include servers where ``enabled: true``.
    - ``contextual`` section: include servers where ``enabled: true`` AND
      (``profile_name`` is in the server's ``profiles`` list OR
      ``profile_name`` is ``None``).

    Env values such as ``"${MY_VAR}"`` are returned verbatim; expansion is
    deferred to subprocess spawn time.

    Args:
        profile_name: LLM profile name used to filter contextual servers.
            Pass ``None`` to include all enabled contextual servers regardless
            of their profiles restriction.
        config_path: Optional explicit path to the YAML file. Bypasses the
            config cascade when provided.

    Returns:
        List of McpServerConfig instances for servers that are active given
        the current profile. Returns an empty list when no config file is
        found (MCP is optional).

    Raises:
        yaml.YAMLError: The config file content is not valid YAML.
    """
    if config_path is not None:
        resolved: Path | None = Path(config_path)
    else:
        resolved = _find_config_file()

    if resolved is None:
        return []

    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    result: list[McpServerConfig] = []

    # --- global servers ---
    for entry in data.get("global") or []:
        if not entry.get("enabled", False):
            continue
        result.append(_build_config(entry))

    # --- contextual servers ---
    for entry in data.get("contextual") or []:
        if not entry.get("enabled", False):
            continue
        # When profile_name is None: include all enabled contextual servers.
        # When profile_name is set: include only servers whose profiles list
        # contains the requested profile.
        profiles: list[str] = entry.get("profiles") or []
        if profile_name is not None and profile_name not in profiles:
            continue
        result.append(_build_config(entry))

    return result


def load_for_sdk(
    profile: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, dict]:
    """Return MCP server configs in the dict format expected by ClaudeAgentOptions.

    Calls load_mcp_servers() internally and converts the list of McpServerConfig
    instances to the mapping required by the claude-agent-sdk:

        {
            "server_name": {
                "command": "...",
                "args": [...],
                "env": {...},   # only present when non-empty
            },
            ...
        }

    Args:
        profile: LLM profile name used to filter contextual servers.
            Pass None to include all enabled contextual servers.
        config_path: Optional explicit path to the YAML file. Bypasses the
            config cascade when provided.

    Returns:
        Dict mapping server names to their config dicts, ready to be passed
        as mcp_servers to ClaudeAgentOptions.  Returns an empty dict when no
        config file is found or no servers are active.
    """
    servers = load_mcp_servers(profile_name=profile, config_path=config_path)
    result: dict[str, dict] = {}
    for server in servers:
        entry: dict = {
            "command": server.command,
            "args": server.args,
        }
        if server.env:
            entry["env"] = server.env
        result[server.name] = entry
    return result


def _subagents_master_switch_enabled(config_yaml_path: Path | None) -> bool:
    """Check whether the subagents master switch is enabled in config.yaml.

    Reads the ``subagents.enabled`` key from config.yaml. Returns True when the
    switch is absent (default-on) or explicitly set to true.

    Args:
        config_yaml_path: Optional explicit path to config.yaml. When None the
            standard config cascade is used.

    Returns:
        True when subagents are enabled (default), False when explicitly disabled.
    """
    if config_yaml_path is not None:
        resolved: Path | None = Path(config_yaml_path)
    else:
        resolved = _find_config_yaml()

    if resolved is None or not resolved.exists():
        return True  # no config.yaml → default on

    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "Failed to parse config.yaml for subagents master switch "
            "(path=%s): %s — defaulting to enabled",
            resolved,
            exc,
        )
        return True

    subagents_section: dict = raw.get("subagents") or {}
    return bool(subagents_section.get("enabled", True))


def load_subagents(
    config_path: str | Path | None = None,
) -> list[SubagentConfig]:
    """Load subagent definitions from the mcp_servers YAML file.

    Reads the top-level ``subagents`` list from the YAML and returns only
    entries where ``enabled: true``. When the ``subagents`` section is absent
    the function returns an empty list without raising (subagents are optional).

    Args:
        config_path: Optional explicit path to the mcp_servers YAML file.
            Bypasses the config cascade when provided.

    Returns:
        List of SubagentConfig instances for enabled subagents.

    Raises:
        yaml.YAMLError: The config file content is not valid YAML.
    """
    if config_path is not None:
        resolved: Path | None = Path(config_path)
    else:
        resolved = _find_config_file()

    if resolved is None:
        return []

    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}

    result: list[SubagentConfig] = []
    for entry in data.get("subagents") or []:
        if not entry.get("enabled", False):
            continue
        tools_raw = entry.get("tools")
        tools: tuple[str, ...] | None = (
            tuple(str(t) for t in tools_raw) if tools_raw is not None else None
        )
        result.append(
            SubagentConfig(
                name=str(entry["name"]),
                description=str(entry["description"]),
                tools=tools,
            )
        )

    return result


def load_subagents_for_sdk(
    config_path: str | Path | None = None,
    config_yaml_path: str | Path | None = None,
    max_turns: int | None = None,
) -> dict[str, Any]:
    """Return subagent configs as an AgentDefinition dict for ClaudeAgentOptions.

    Checks the master switch in config.yaml first — returns {} immediately when
    ``subagents.enabled`` is false. Otherwise calls load_subagents() and converts
    each SubagentConfig to an AgentDefinition instance.

    The generated AgentDefinition has:
    - ``prompt``: ``f"You are a specialized subagent. Your role: {description}."``
    - ``model``: None (inherits principal agent's model)
    - ``mcpServers``: None (inherits all MCPs from parent)
    - ``tools``: forwarded verbatim from the YAML entry (None when absent)
    - ``maxTurns``: forwarded from max_turns parameter (None when absent)

    Args:
        config_path: Optional explicit path to the mcp_servers YAML file.
            Bypasses the config cascade when provided.
        config_yaml_path: Optional explicit path to config.yaml for reading the
            master switch. Bypasses the cascade when provided.
        max_turns: Optional maximum number of agentic turns for each subagent.
            When provided, passed as maxTurns to each AgentDefinition.

    Returns:
        Dict mapping subagent name to AgentDefinition instance, ready to be
        passed as ``agents=`` to ClaudeAgentOptions. Returns {} when the master
        switch is disabled or no enabled subagents are found.

    Raises:
        RuntimeError: claude_agent_sdk is not installed but subagents are
            configured (subagents require the SDK to function).
    """
    resolved_config_yaml = (
        Path(config_yaml_path) if config_yaml_path is not None else None
    )
    if not _subagents_master_switch_enabled(resolved_config_yaml):
        return {}

    subagents = load_subagents(config_path=config_path)
    if not subagents:
        return {}

    if AgentDefinition is None:
        raise RuntimeError(
            "claude_agent_sdk is required for subagents support. "
            "Install it with: pip install claude-agent-sdk"
        )

    result: dict[str, Any] = {}
    for sub in subagents:
        prompt = f"You are a specialized subagent. Your role: {sub.description}."
        result[sub.name] = AgentDefinition(
            description=sub.description,
            prompt=prompt,
            tools=list(sub.tools) if sub.tools is not None else None,
            model=None,
            mcpServers=None,
            maxTurns=max_turns,
        )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_config(entry: dict) -> McpServerConfig:
    """Construct a McpServerConfig from a raw YAML entry dict.

    Args:
        entry: A single server definition dict parsed from the YAML file.

    Returns:
        Populated McpServerConfig instance with env defaulting to {}.
    """
    return McpServerConfig(
        name=str(entry["name"]),
        type=str(entry["type"]),
        command=str(entry["command"]),
        args=[str(a) for a in (entry.get("args") or [])],
        env=dict(entry.get("env") or {}),
    )
