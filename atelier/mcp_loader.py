"""MCP server configuration loader for the Atelier brick.

Reads MCP (Model Context Protocol) server definitions from a YAML configuration
file following the standard config cascade:
    ~/.relais/config/ > /opt/relais/config/ > ./config/

MCP is optional — when no config file is found the loader returns an empty list
rather than raising, ensuring graceful degradation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerConfig:
    """Immutable configuration for a single MCP server entry.

    Two transports are supported:

    - ``type="stdio"``: the MCP server is a subprocess spawned by the client.
      ``command`` and ``args`` are required; ``url`` is ignored.
    - ``type="sse"``: the MCP server runs independently and the client connects
      to it via HTTP/SSE.  ``url`` is required; ``command`` and ``args`` are
      ignored.

    Attributes:
        name: Human-readable identifier for the server.
        type: Transport type — ``"stdio"`` (default) or ``"sse"``.
        command: Executable to spawn (stdio only, e.g. ``"npx"``, ``"uvx"``).
        args: Command-line arguments passed to the executable (stdio only).
        url: HTTP endpoint to connect to (SSE only, e.g. ``"http://127.0.0.1:8100"``).
        env: Environment variables injected into the subprocess (stdio) or
            passed alongside the connection (SSE, optional).
            Values are kept verbatim (e.g. ``"${MY_VAR}"``) and resolved at
            connection time, not at load time.
    """

    name: str
    type: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)


_FILENAME = "atelier/mcp_servers.yaml"


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

    YAML file structure::

        mcp_servers:
          global:          # active for all profiles
            - name: ...
              enabled: true|false
              type: stdio|sse     # default: stdio
              command: ...        # stdio only
              args: [...]         # stdio only
              url: ...            # sse only
              env: {}
          contextual:      # active only for specific profiles
            - name: ...
              enabled: true|false
              type: stdio|sse
              command: ...
              args: [...]
              url: ...
              profiles: [profile1, profile2]
              env: {}

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
        resolved: Path = Path(config_path)
    else:
        try:
            resolved = resolve_config_path(_FILENAME)
        except FileNotFoundError:
            return []

    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    mcp_section = (data or {}).get("mcp_servers") or {}
    result: list[McpServerConfig] = []

    # --- global servers ---
    for entry in mcp_section.get("global") or []:
        if not entry.get("enabled", False):
            continue
        result.append(_build_config(entry))

    # --- contextual servers ---
    for entry in mcp_section.get("contextual") or []:
        if not entry.get("enabled", False):
            continue
        profiles: list[str] = entry.get("profiles") or []
        if profile_name is not None and profile_name not in profiles:
            continue
        result.append(_build_config(entry))

    return result


def load_for_sdk(
    profile: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, dict]:
    """Return MCP server configs as a dict for SDKExecutor.

    Calls load_mcp_servers() internally and converts the list of McpServerConfig
    instances to the mapping used by SDKExecutor.

    For stdio servers::

        {
            "server_name": {
                "type": "stdio",
                "command": "...",
                "args": [...],
                "env": {...},   # only present when non-empty
            },
        }

    For SSE servers::

        {
            "server_name": {
                "type": "sse",
                "url": "http://...",
                "env": {...},   # only present when non-empty
            },
        }

    Args:
        profile: LLM profile name used to filter contextual servers.
            Pass None to include all enabled contextual servers.
        config_path: Optional explicit path to the YAML file. Bypasses the
            config cascade when provided.

    Returns:
        Dict mapping server names to their config dicts, ready to be passed
        as mcp_servers to SDKExecutor.  Returns an empty dict when no
        config file is found or no servers are active.
    """
    servers = load_mcp_servers(profile_name=profile, config_path=config_path)
    result: dict[str, dict] = {}
    for server in servers:
        entry: dict = {"type": server.type}
        if server.type == "stdio":
            entry["command"] = server.command
            entry["args"] = server.args
        elif server.type == "sse":
            entry["url"] = server.url
        if server.env:
            entry["env"] = server.env
        result[server.name] = entry
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_config(entry: dict) -> McpServerConfig:
    """Construct a McpServerConfig from a raw YAML entry dict.

    Args:
        entry: A single server definition dict parsed from the YAML file.

    Returns:
        Populated McpServerConfig instance with type defaulting to ``"stdio"``.
    """
    transport = str(entry.get("type", "stdio"))
    return McpServerConfig(
        name=str(entry["name"]),
        type=transport,
        command=str(entry["command"]) if "command" in entry else None,
        args=[str(a) for a in (entry.get("args") or [])],
        url=str(entry["url"]) if "url" in entry else None,
        env=dict(entry.get("env") or {}),
    )
