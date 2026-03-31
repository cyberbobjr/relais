"""Profile loader for the Atelier brick.

Reads LLM profiles from a YAML configuration file following the standard
config cascade: ~/.relais/config/ > /opt/relais/config/ > ./config/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from common.config_loader import resolve_config_path

_VALID_MEMORY_SCOPES: Final[frozenset[str]] = frozenset(
    {"global", "own", "sender", "task"}
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResilienceConfig:
    """Resilience parameters for a single LLM profile.

    Attributes:
        retry_attempts: Maximum number of retry attempts on transient failure.
        retry_delays: Progressive backoff delays in seconds between retries.
        fallback_model: Optional model identifier to use when all retries fail.
    """

    retry_attempts: int
    retry_delays: list[int]
    fallback_model: str | None = None


@dataclass(frozen=True)
class ProfileConfig:
    """Configuration for a single named LLM profile.

    Attributes:
        model: DeepAgents model identifier in 'provider:model' format
            (e.g. "anthropic:claude-haiku-4-5", "openai:gpt-4o-mini").
            The provider prefix is required — LiteLLM proxy has been removed.
        temperature: Sampling temperature controlling response randomness.
        max_tokens: Maximum number of tokens the LLM may generate.
        max_turns: Maximum number of agentic turns in the tool-use loop.
        resilience: Retry and fallback configuration for transient failures.
            Loaded and exposed on AgentExecutor; retry logic is not yet
            enforced — see TODO in agent_executor.py (Phase 5).
        allowed_tools: Tuple of allowed MCP tool names; None means unrestricted.
            Loaded for forward-compatibility; not yet enforced.
        allowed_mcp: Tuple of allowed MCP server names; None means unrestricted.
            Loaded for forward-compatibility; not yet enforced.
        guardrails: Content guardrail rules (e.g. "no_bash", "no_code_exec").
            Loaded for forward-compatibility; not yet enforced.
        memory_scope: Memory visibility scope — one of "global", "own", "sender",
            or "task".
        fallback_model: Model identifier to use when the primary model fails;
            None means no fallback at the profile level.
        mcp_timeout: Seconds to wait for a single MCP tool call before raising
            asyncio.TimeoutError. Default 10.
        mcp_max_tools: Maximum number of MCP tool definitions passed to the model.
            0 means no MCP tools are exposed. Internal tools are not counted.
            Default 20.
    """

    model: str
    temperature: float
    max_tokens: int
    resilience: ResilienceConfig
    max_turns: int = 20
    allowed_tools: tuple[str, ...] | None = None
    allowed_mcp: tuple[str, ...] | None = None
    guardrails: tuple[str, ...] = ()
    memory_scope: str = "own"
    fallback_model: str | None = None
    mcp_timeout: int = 10
    mcp_max_tools: int = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_profiles(
    config_path: str | Path | None = None,
) -> dict[str, ProfileConfig]:
    """Load all LLM profiles from a YAML configuration file.

    When config_path is given, reads that file directly (useful for tests).
    Otherwise, walks the standard cascade: ~/.relais/config/ > /opt/relais/config/
    > ./config/.

    Args:
        config_path: Optional explicit path to a profiles YAML file. When
            provided, the config cascade is bypassed entirely.

    Returns:
        Dictionary mapping profile name strings to ProfileConfig instances.

    Raises:
        FileNotFoundError: config_path provided but does not exist, or no
            profiles.yaml found in the config cascade.
        KeyError: The YAML file is missing the top-level 'profiles' key.
        yaml.YAMLError: The file content is not valid YAML.
    """
    if config_path is not None:
        resolved = Path(config_path)
    else:
        resolved = resolve_config_path("profiles.yaml")

    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    profiles_data: dict = data["profiles"]
    result: dict[str, ProfileConfig] = {}

    for name, cfg in profiles_data.items():
        resilience_raw: dict = cfg["resilience"]
        resilience = ResilienceConfig(
            retry_attempts=int(resilience_raw["retry_attempts"]),
            retry_delays=[int(d) for d in resilience_raw["retry_delays"]],
            fallback_model=resilience_raw.get("fallback_model") or None,
        )
        # Parse optional collection fields into tuples (immutability requirement).
        raw_allowed_tools = cfg.get("allowed_tools")
        allowed_tools: tuple[str, ...] | None = (
            tuple(str(t) for t in raw_allowed_tools)
            if raw_allowed_tools is not None
            else None
        )

        raw_allowed_mcp = cfg.get("allowed_mcp")
        allowed_mcp: tuple[str, ...] | None = (
            tuple(str(m) for m in raw_allowed_mcp)
            if raw_allowed_mcp is not None
            else None
        )

        raw_guardrails = cfg.get("guardrails")
        guardrails: tuple[str, ...] = (
            tuple(str(g) for g in raw_guardrails) if raw_guardrails else ()
        )

        memory_scope: str = str(cfg.get("memory_scope", "own"))
        if memory_scope not in _VALID_MEMORY_SCOPES:
            raise ValueError(
                f"Invalid memory_scope '{memory_scope}' for profile '{name}'. "
                f"Must be one of: {sorted(_VALID_MEMORY_SCOPES)}"
            )

        fallback_model: str | None = cfg.get("fallback_model") or None

        result[name] = ProfileConfig(
            model=str(cfg["model"]),
            temperature=float(cfg["temperature"]),
            max_tokens=int(cfg["max_tokens"]),
            resilience=resilience,
            max_turns=int(cfg["max_turns"]) if "max_turns" in cfg else 20,
            allowed_tools=allowed_tools,
            allowed_mcp=allowed_mcp,
            guardrails=guardrails,
            memory_scope=memory_scope,
            fallback_model=fallback_model,
            mcp_timeout=int(cfg.get("mcp_timeout", 10)),
            mcp_max_tools=int(cfg.get("mcp_max_tools", 20)),
        )

    return result


def resolve_profile(
    profiles: dict[str, ProfileConfig],
    name: str,
) -> ProfileConfig:
    """Return the named profile, falling back to 'default' if not found.

    Args:
        profiles: Dictionary of loaded ProfileConfig instances keyed by name.
        name: The profile name to look up.

    Returns:
        The ProfileConfig for name, or the 'default' ProfileConfig if name
        is absent from the dictionary.

    Raises:
        KeyError: Neither the requested name nor 'default' exists in profiles.
    """
    if name in profiles:
        return profiles[name]
    return profiles["default"]
