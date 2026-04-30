"""Profile-to-model resolution for the Atelier agent executor."""

from __future__ import annotations

import os
from typing import Any, Protocol

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

            # Pass 1 — inject reasoning_content for messages that carry it.
            # Track whether any assistant turn received the field so Pass 2 knows
            # whether to back-fill diagnostic AIMessages that lack additional_kwargs.
            any_thinking = False
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
                    any_thinking = True

            # Pass 2 — once thinking mode is active, ALL assistant turns must echo
            # reasoning_content.  Diagnostic messages injected by inject_diagnostic_message
            # (AIMessage without additional_kwargs) would otherwise cause DeepSeek to return
            # 400 "reasoning_content must be passed back" on the very next turn.
            if any_thinking:
                for msg_dict in payload_messages:
                    if msg_dict.get("role") == "assistant":
                        msg_dict.setdefault("reasoning_content", "")

            return payload

except ImportError:
    _ChatDeepSeekReasoningPassback = None  # type: ignore[assignment,misc]


class ModelHandler(Protocol):
    """Protocol for provider-specific model instantiation."""

    always_instantiate: bool

    def can_handle(self, provider: str) -> bool: ...

    def build(self, profile: ProfileConfig, base_kwargs: dict[str, Any]) -> BaseChatModel: ...


class DeepSeekModelHandler:
    """Handler for the ``deepseek:`` provider prefix."""

    always_instantiate = True

    def can_handle(self, provider: str) -> bool:
        return provider == "deepseek"

    def build(self, profile: ProfileConfig, base_kwargs: dict[str, Any]) -> BaseChatModel:
        if _ChatDeepSeekReasoningPassback is None:
            raise ImportError(
                "langchain_deepseek is required for deepseek: models. "
                "Install it with: pip install langchain-deepseek"
            )
        model_name = profile.model.partition(":")[2]
        base_url = base_kwargs.pop("base_url", None)
        if base_url is not None:
            base_kwargs["api_base"] = base_url
        return _ChatDeepSeekReasoningPassback(model=model_name, **base_kwargs)


class DefaultModelHandler:
    """Catch-all handler that delegates to ``init_chat_model`` for all providers."""

    always_instantiate = False

    def can_handle(self, provider: str) -> bool:
        return True

    def build(self, profile: ProfileConfig, base_kwargs: dict[str, Any]) -> BaseChatModel:
        return init_chat_model(profile.model, **base_kwargs)


_HANDLER_REGISTRY: list[ModelHandler] = [
    DeepSeekModelHandler(),
    DefaultModelHandler(),
]


def _resolve_profile_model(
    profile: ProfileConfig,
) -> BaseChatModel | str:
    """Build the model argument for create_deep_agent from a ProfileConfig.

    Extracts the provider prefix from ``profile.model`` (e.g. ``deepseek``
    from ``deepseek:deepseek-chat``) and dispatches to the first matching
    handler in ``_HANDLER_REGISTRY``.  ``DefaultModelHandler`` is always last
    and accepts any provider, so dispatch never falls through.

    Returns the model string directly when no special handling is needed
    (no base_url, api_key_env, parallel_tool_calls, max_tokens, and the
    handler does not require instantiation).  Otherwise builds and returns
    a ``BaseChatModel``.

    Args:
        profile: The resolved ProfileConfig for the current envelope.

    Returns:
        Either the model identifier string, or a pre-built BaseChatModel
        instance with the configured endpoint, credentials, and generation
        parameters (including max_tokens).

    Raises:
        KeyError: api_key_env is set but the environment variable is absent.
        ImportError: A provider-specific library is required but not installed.
    """
    provider = profile.model.partition(":")[0]
    handler = next(h for h in _HANDLER_REGISTRY if h.can_handle(provider))

    needs_init = (
        handler.always_instantiate
        or profile.base_url is not None
        or profile.api_key_env is not None
        or profile.parallel_tool_calls is not None
        or profile.max_tokens != 0
    )
    if not needs_init:
        return profile.model

    base_kwargs: dict[str, Any] = {}
    if profile.max_tokens:
        base_kwargs["max_tokens"] = profile.max_tokens
    if profile.base_url is not None:
        base_kwargs["base_url"] = profile.base_url
    if profile.api_key_env is not None:
        api_key = os.environ.get(profile.api_key_env)
        if api_key is None:
            raise KeyError(
                f"Environment variable '{profile.api_key_env}' (required by profile "
                f"'{profile.model}') is not set."
            )
        base_kwargs["api_key"] = api_key
    if profile.parallel_tool_calls is not None:
        base_kwargs.setdefault("model_kwargs", {})["parallel_tool_calls"] = profile.parallel_tool_calls

    return handler.build(profile, base_kwargs)
