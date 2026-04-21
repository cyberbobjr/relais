"""LLM factory for Forgeron — builds BaseChatModel from ProfileConfig."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

from common.profile_loader import ProfileConfig


def build_chat_model(profile: ProfileConfig) -> "BaseChatModel":
    """Build a LangChain BaseChatModel from a ProfileConfig.

    Args:
        profile: The profile whose parameters drive the LLM instantiation.

    Returns:
        An instantiated BaseChatModel ready for async invocation.

    Raises:
        EnvironmentError: If profile.api_key_env is set but the environment
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
