"""Profile loader — shared utility for all bricks needing LLM profiles.

Reads LLM profiles from a YAML configuration file following the standard
config cascade: ~/.relais/config/ > /opt/relais/config/ > ./config/.
The profiles file is located at atelier/profiles.yaml in the config cascade.

This module is intentionally part of ``common/`` so that any brick
(Atelier, Forgeron, …) can load profiles without depending on another brick's
package.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from common.config_loader import resolve_config_path

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

# Matches any $VAR or ${VAR} that survives os.path.expandvars — i.e. the variable was unset.
_UNEXPANDED_VAR_RE: Final[re.Pattern[str]] = re.compile(
    r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)"
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
        model: LangChain model identifier in 'provider:model' format
            (e.g. "anthropic:claude-haiku-4-5", "openai:gpt-4o-mini").
            The provider prefix is required.
        temperature: Sampling temperature controlling response randomness.
        max_tokens: Maximum number of tokens the LLM may generate.
        resilience: Retry and fallback configuration for transient failures.
        base_url: Override the provider's default API endpoint. Used for local
            models (Ollama, LM Studio) or custom deployments. None means use
            the provider's built-in default.
        api_key_env: Name of the environment variable holding the API key for
            this provider. The value is read at call time via os.environ[].
            None means no API key is injected (e.g. local Ollama).
        max_turns: Maximum number of agentic turns in the tool-use loop.
        fallback_model: Model identifier to use when the primary model fails;
            None means no fallback at the profile level.
        mcp_timeout: Seconds to wait for a single MCP tool call before raising
            asyncio.TimeoutError. Default 10.
        mcp_max_tools: Maximum number of MCP tool definitions passed to the model.
            0 means no MCP tools are exposed. Internal tools are not counted.
            Default 20.
        parallel_tool_calls: When False, disables parallel tool calls via the
            OpenAI-compatible API parameter. When None (default), the parameter
            is not forwarded and the provider default applies.
    """

    model: str
    temperature: float
    max_tokens: int
    resilience: ResilienceConfig
    base_url: str | None
    api_key_env: str | None
    max_turns: int = 20
    fallback_model: str | None = None
    mcp_timeout: int = 10
    mcp_max_tools: int = 20
    parallel_tool_calls: bool | None = None


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
            atelier/profiles.yaml found in the config cascade.
        KeyError: The YAML file is missing the top-level 'profiles' key.
        yaml.YAMLError: The file content is not valid YAML.
    """
    if config_path is not None:
        resolved = Path(config_path)
    else:
        resolved = resolve_config_path("atelier/profiles.yaml")

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
        fallback_model: str | None = cfg.get("fallback_model") or None

        base_url_raw: str | None = cfg["base_url"]
        if base_url_raw is not None:
            expanded = os.path.expandvars(str(base_url_raw))
            if _UNEXPANDED_VAR_RE.search(expanded):
                raise ValueError(
                    f"base_url for profile '{name}' references an unset environment variable: "
                    f"{base_url_raw!r}"
                )
            base_url: str | None = expanded
        else:
            base_url = None

        api_key_env: str | None = cfg["api_key_env"]

        raw_ptc = cfg.get("parallel_tool_calls")
        if raw_ptc is not None and not isinstance(raw_ptc, bool):
            raise ValueError(
                f"parallel_tool_calls for profile '{name}' must be a boolean, "
                f"got {type(raw_ptc).__name__!r}"
            )
        parallel_tool_calls: bool | None = raw_ptc

        result[name] = ProfileConfig(
            model=str(cfg["model"]),
            temperature=float(cfg["temperature"]),
            max_tokens=int(cfg["max_tokens"]),
            resilience=resilience,
            base_url=base_url,
            api_key_env=api_key_env,
            max_turns=int(cfg["max_turns"]) if "max_turns" in cfg else 20,
            fallback_model=fallback_model,
            mcp_timeout=int(cfg.get("mcp_timeout", 10)),
            mcp_max_tools=int(cfg.get("mcp_max_tools", 20)),
            parallel_tool_calls=parallel_tool_calls,
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


def build_chat_model(profile: ProfileConfig) -> "BaseChatModel":
    """Build a LangChain BaseChatModel from a ProfileConfig.

    Injects ``base_url``, ``api_key`` (from the named env var), and
    ``parallel_tool_calls`` (as a model kwarg) when present on the profile.
    Unlike ``_resolve_profile_model`` in ``agent_executor``, this function
    always returns a ``BaseChatModel`` — suitable for direct ``ainvoke()``
    calls in Forgeron (ChangelogWriter, SkillConsolidator, IntentLabeler).

    Args:
        profile: The profile whose parameters drive the LLM instantiation.

    Returns:
        An instantiated ``BaseChatModel`` ready for async invocation.

    Raises:
        KeyError: If ``profile.api_key_env`` is set but the environment
            variable is not present.
    """
    from langchain.chat_models import init_chat_model  # noqa: PLC0415

    kwargs: dict[str, Any] = {}
    if profile.base_url is not None:
        kwargs["base_url"] = profile.base_url
    if profile.api_key_env is not None:
        api_key = os.environ.get(profile.api_key_env)
        if api_key is None:
            raise EnvironmentError(
                f"Required environment variable '{profile.api_key_env}' for profile "
                f"model '{profile.model}' is not set."
            )
        kwargs["api_key"] = api_key
    if profile.parallel_tool_calls is not None:
        kwargs["model_kwargs"] = {"parallel_tool_calls": profile.parallel_tool_calls}
    return init_chat_model(profile.model, **kwargs)
