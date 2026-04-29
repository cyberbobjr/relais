"""Profile-to-model resolution for the Atelier agent executor."""

from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import BaseChatModel, init_chat_model

from common.profile_loader import ProfileConfig


try:
    from langchain_deepseek import ChatDeepSeek as _ChatDeepSeek

    class _ChatDeepSeekReasoningPassback(_ChatDeepSeek):
        """ChatDeepSeek that re-injects reasoning_content on multi-turn calls.

        BaseChatOpenAI._convert_message_to_dict drops additional_kwargs fields
        it doesn't know about, including reasoning_content.  DeepSeek's API
        requires reasoning_content to be echoed back in every subsequent
        assistant message when thinking mode is active — without it the API
        returns 400.  This override re-injects the value after the base-class
        payload is built.

        use_responses_api is forced False to guarantee the Chat Completions
        code path, so payload["messages"] is always a 1:1 list aligned with
        the input messages and the zip below is safe.
        """

        use_responses_api: bool = False  # type: ignore[assignment]

        def _get_request_payload(
            self,
            input_: Any,
            *,
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> dict:
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            payload_messages = payload.get("messages", [])
            if not payload_messages:
                return payload
            try:
                messages = self._convert_input(input_).to_messages()
            except Exception:
                return payload  # best-effort: don't break the call on edge inputs
            if len(messages) != len(payload_messages):
                return payload  # alignment broken, skip injection
            for msg, msg_dict in zip(messages, payload_messages):
                if (
                    msg_dict.get("role") == "assistant"
                    and hasattr(msg, "additional_kwargs")
                    # Empty string is also passed back: DeepSeek requires the field
                    # to be present on every assistant turn once thinking started.
                    and msg.additional_kwargs.get("reasoning_content") is not None
                ):
                    msg_dict["reasoning_content"] = msg.additional_kwargs[
                        "reasoning_content"
                    ]
            return payload

except ImportError:
    _ChatDeepSeekReasoningPassback = None  # type: ignore[assignment,misc]


def _resolve_profile_model(
    profile: ProfileConfig,
) -> BaseChatModel | str:
    """Build the model argument for create_deep_agent from a ProfileConfig.

    For ``deepseek:`` provider prefix models, always returns a
    ``_ChatDeepSeekReasoningPassback`` instance (when ``langchain_deepseek``
    is installed) so that ``reasoning_content`` is echoed back in multi-turn
    conversations.

    Returns the model string directly only when none of base_url,
    api_key_env, parallel_tool_calls, or max_tokens require an explicit
    init_chat_model() call AND the model is not a deepseek: model.
    Otherwise, constructs a BaseChatModel via init_chat_model(), passing
    only the kwargs that are present.

    Args:
        profile: The resolved ProfileConfig for the current envelope.

    Returns:
        Either the model identifier string, or a pre-built BaseChatModel
        instance with the configured endpoint, credentials, and generation
        parameters (including max_tokens).

    Raises:
        KeyError: api_key_env is set but the environment variable is absent.
    """
    is_deepseek = profile.model.startswith("deepseek:")
    needs_init = (
        is_deepseek
        or profile.base_url is not None
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

    # For direct DeepSeek models (deepseek: provider prefix), use the patched
    # subclass so reasoning_content is echoed back in multi-turn calls.
    # ChatDeepSeek uses api_base instead of base_url; reuse the rest of kwargs.
    if is_deepseek and _ChatDeepSeekReasoningPassback is not None:
        model_name = profile.model.split(":", 1)[1]
        base_url = kwargs.pop("base_url", None)
        if base_url is not None:
            kwargs["api_base"] = base_url
        return _ChatDeepSeekReasoningPassback(model=model_name, **kwargs)

    return init_chat_model(profile.model, **kwargs)
