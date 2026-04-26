"""Profile-to-model resolution for the Atelier agent executor.

Provides ``_resolve_profile_model`` extracted from ``atelier/agent_executor.py``
to keep that module under the 800-line limit.

Re-exported in ``atelier/agent_executor.py`` for backward compatibility.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import BaseChatModel, init_chat_model

from common.profile_loader import ProfileConfig


def _resolve_profile_model(
    profile: ProfileConfig,
) -> BaseChatModel | str:
    """Build the model argument for create_deep_agent from a ProfileConfig.

    Returns the model string directly only when none of base_url,
    api_key_env, parallel_tool_calls, or max_tokens require an explicit
    init_chat_model() call. Otherwise, constructs a BaseChatModel via
    init_chat_model(), passing only the kwargs that are present.

    Args:
        profile: The resolved ProfileConfig for the current envelope.

    Returns:
        Either the model identifier string, or a pre-built BaseChatModel
        instance with the configured endpoint, credentials, and generation
        parameters (including max_tokens).

    Raises:
        KeyError: api_key_env is set but the environment variable is absent.
    """
    needs_init = (
        profile.base_url is not None
        or profile.api_key_env is not None
        or profile.parallel_tool_calls is not None
        or profile.max_tokens != 0
    )
    if not needs_init:
        return profile.model
    kwargs: dict[str, Any] = {}
    if profile.max_tokens:
        kwargs["max_tokens"] = profile.max_tokens
    if profile.base_url is not None:
        kwargs["base_url"] = profile.base_url
    if profile.api_key_env is not None:
        api_key = os.environ.get(profile.api_key_env)
        if api_key is None:
            raise KeyError(
                f"Environment variable '{profile.api_key_env}' (required by profile "
                f"'{profile.model}') is not set."
            )
        kwargs["api_key"] = api_key
    if profile.parallel_tool_calls is not None:
        kwargs.setdefault("model_kwargs", {})["parallel_tool_calls"] = profile.parallel_tool_calls
    return init_chat_model(profile.model, **kwargs)
