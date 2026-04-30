"""Unit tests for atelier.profile_model.

Tests validate:
- _resolve_profile_model is importable from atelier.profile_model
- When neither base_url nor api_key_env is set, returns model string directly
- When base_url is set, constructs a BaseChatModel via init_chat_model
- When api_key_env is set and present in env, passes api_key to init_chat_model
- When api_key_env is set but absent from env, raises KeyError
- When parallel_tool_calls is set, passes it via model_kwargs
- deepseek: prefix returns _ChatDeepSeekReasoningPassback instance
- _ChatDeepSeekReasoningPassback re-injects reasoning_content into payload
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_profile(
    model: str = "anthropic:claude-haiku-4-5",
    base_url: str | None = None,
    api_key_env: str | None = None,
    parallel_tool_calls: bool | None = None,
    max_tokens: int = 0,
) -> MagicMock:
    """Return a minimal ProfileConfig mock."""
    p = MagicMock()
    p.model = model
    p.base_url = base_url
    p.api_key_env = api_key_env
    p.parallel_tool_calls = parallel_tool_calls
    p.max_tokens = max_tokens
    return p


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_importable() -> None:
    """_resolve_profile_model must be importable from atelier.profile_model."""
    from atelier.profile_model import _resolve_profile_model  # noqa: F401


# ---------------------------------------------------------------------------
# Simple model string (no base_url / api_key_env)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_returns_string_when_no_extra_config() -> None:
    """Returns model string directly when neither base_url nor api_key_env is set."""
    from atelier.profile_model import _resolve_profile_model

    profile = _make_profile(model="anthropic:claude-haiku-4-5")
    result = _resolve_profile_model(profile)
    assert result == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# With base_url — constructs BaseChatModel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_with_base_url_calls_init_chat_model() -> None:
    """When base_url is set, calls init_chat_model with base_url kwarg."""
    from atelier.profile_model import _resolve_profile_model

    fake_model = MagicMock()
    profile = _make_profile(model="openai:gpt-4", base_url="http://localhost:8080")

    with patch("atelier.profile_model.init_chat_model", return_value=fake_model) as mock_init:
        result = _resolve_profile_model(profile)

    mock_init.assert_called_once()
    call_kwargs = mock_init.call_args.kwargs
    assert call_kwargs.get("base_url") == "http://localhost:8080"
    assert result is fake_model


# ---------------------------------------------------------------------------
# With api_key_env — injects api_key from environment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_with_api_key_env_present() -> None:
    """When api_key_env is set and the env var exists, injects api_key."""
    from atelier.profile_model import _resolve_profile_model

    fake_model = MagicMock()
    profile = _make_profile(model="openai:gpt-4", api_key_env="MY_API_KEY")

    with patch.dict(os.environ, {"MY_API_KEY": "sk-secret"}):
        with patch("atelier.profile_model.init_chat_model", return_value=fake_model) as mock_init:
            result = _resolve_profile_model(profile)

    call_kwargs = mock_init.call_args.kwargs
    assert call_kwargs.get("api_key") == "sk-secret"
    assert result is fake_model


@pytest.mark.unit
def test_resolve_profile_model_with_api_key_env_missing_raises_key_error() -> None:
    """When api_key_env is set but the env var is missing, raises KeyError."""
    from atelier.profile_model import _resolve_profile_model

    profile = _make_profile(model="openai:gpt-4", api_key_env="MISSING_API_KEY")

    env_without_key = {k: v for k, v in os.environ.items() if k != "MISSING_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises(KeyError, match="MISSING_API_KEY"):
            _resolve_profile_model(profile)


# ---------------------------------------------------------------------------
# With parallel_tool_calls — injects via model_kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_with_parallel_tool_calls() -> None:
    """When parallel_tool_calls is set, passes it in model_kwargs to init_chat_model."""
    from atelier.profile_model import _resolve_profile_model

    fake_model = MagicMock()
    profile = _make_profile(model="openai:gpt-4", parallel_tool_calls=False)

    with patch("atelier.profile_model.init_chat_model", return_value=fake_model) as mock_init:
        result = _resolve_profile_model(profile)

    call_kwargs = mock_init.call_args.kwargs
    assert call_kwargs.get("model_kwargs", {}).get("parallel_tool_calls") is False
    assert result is fake_model


# ---------------------------------------------------------------------------
# deepseek: prefix — uses _ChatDeepSeekReasoningPassback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_deepseek_model_returns_patched_subclass() -> None:
    """deepseek: prefix instantiates _ChatDeepSeekReasoningPassback, not init_chat_model."""
    from atelier.profile_model import _ChatDeepSeekReasoningPassback, _resolve_profile_model

    if _ChatDeepSeekReasoningPassback is None:
        pytest.skip("langchain_deepseek not installed")

    fake_instance = MagicMock()
    profile = _make_profile(model="deepseek:deepseek-chat", api_key_env="DS_KEY")

    with patch.dict(os.environ, {"DS_KEY": "sk-fake"}):
        with patch("atelier.profile_model._ChatDeepSeekReasoningPassback", return_value=fake_instance) as mock_cls:
            result = _resolve_profile_model(profile)

    mock_cls.assert_called_once()
    call_kwargs = mock_cls.call_args.kwargs
    assert call_kwargs["model"] == "deepseek-chat"
    assert call_kwargs["api_key"] == "sk-fake"
    assert result is fake_instance


@pytest.mark.unit
def test_resolve_deepseek_model_maps_base_url_to_api_base() -> None:
    """base_url is remapped to api_base when building a deepseek: model."""
    from atelier.profile_model import _ChatDeepSeekReasoningPassback, _resolve_profile_model

    if _ChatDeepSeekReasoningPassback is None:
        pytest.skip("langchain_deepseek not installed")

    fake_instance = MagicMock()
    profile = _make_profile(
        model="deepseek:deepseek-chat",
        base_url="http://custom.api/v1",
        api_key_env="DS_KEY",
    )

    with patch.dict(os.environ, {"DS_KEY": "sk-fake"}):
        with patch("atelier.profile_model._ChatDeepSeekReasoningPassback", return_value=fake_instance) as mock_cls:
            _resolve_profile_model(profile)

    call_kwargs = mock_cls.call_args.kwargs
    assert call_kwargs.get("api_base") == "http://custom.api/v1"
    assert "base_url" not in call_kwargs


@pytest.mark.unit
def test_resolve_deepseek_model_no_import_raises_import_error() -> None:
    """When langchain_deepseek is absent, deepseek: raises ImportError (no silent fallback)."""
    from atelier.profile_model import _resolve_profile_model

    profile = _make_profile(model="deepseek:deepseek-chat", api_key_env="DS_KEY")

    with patch.dict(os.environ, {"DS_KEY": "sk-fake"}):
        with patch("atelier.profile_model._ChatDeepSeekReasoningPassback", None):
            with pytest.raises(ImportError, match="langchain_deepseek"):
                _resolve_profile_model(profile)


# ---------------------------------------------------------------------------
# _ChatDeepSeekReasoningPassback._get_request_payload
# ---------------------------------------------------------------------------


@pytest.fixture
def deepseek_instance():
    """Return a _ChatDeepSeekReasoningPassback instance, skip if not installed."""
    from atelier.profile_model import _ChatDeepSeekReasoningPassback

    if _ChatDeepSeekReasoningPassback is None:
        pytest.skip("langchain_deepseek not installed")
    return _ChatDeepSeekReasoningPassback(model="deepseek-chat", api_key="fake")


@pytest.mark.unit
def test_reasoning_passback_use_responses_api_forced_false(deepseek_instance) -> None:
    """use_responses_api must be False on every instance to force Chat Completions path."""
    assert deepseek_instance.use_responses_api is False


@pytest.mark.unit
def test_reasoning_passback_injects_reasoning_content(deepseek_instance) -> None:
    """reasoning_content in AIMessage.additional_kwargs is re-injected into the payload dict."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="Answer", additional_kwargs={"reasoning_content": "chain of thought"}),
        HumanMessage(content="Follow-up"),
    ]
    parent_payload = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Answer"},
            {"role": "user", "content": "Follow-up"},
        ]
    }

    mock_value = MagicMock()
    mock_value.to_messages.return_value = messages

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", return_value=mock_value):
            result = deepseek_instance._get_request_payload(messages)

    assert result["messages"][1]["reasoning_content"] == "chain of thought"
    assert "reasoning_content" not in result["messages"][0]
    assert "reasoning_content" not in result["messages"][2]


@pytest.mark.unit
def test_reasoning_passback_passes_empty_string_reasoning(deepseek_instance) -> None:
    """Empty string reasoning_content is also re-injected (DeepSeek requires the field)."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [
        HumanMessage(content="Hi"),
        AIMessage(content="OK", additional_kwargs={"reasoning_content": ""}),
    ]
    parent_payload = {
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "OK"},
        ]
    }

    mock_value = MagicMock()
    mock_value.to_messages.return_value = messages

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", return_value=mock_value):
            result = deepseek_instance._get_request_payload(messages)

    assert result["messages"][1]["reasoning_content"] == ""


@pytest.mark.unit
def test_reasoning_passback_skips_when_no_reasoning_content(deepseek_instance) -> None:
    """Messages without reasoning_content in additional_kwargs are left unchanged."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="No thinking"),
    ]
    parent_payload = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "No thinking"},
        ]
    }

    mock_value = MagicMock()
    mock_value.to_messages.return_value = messages

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", return_value=mock_value):
            result = deepseek_instance._get_request_payload(messages)

    assert "reasoning_content" not in result["messages"][0]
    assert "reasoning_content" not in result["messages"][1]


@pytest.mark.unit
def test_reasoning_passback_returns_unchanged_on_length_mismatch(deepseek_instance) -> None:
    """When message list and payload list have different lengths, payload is returned as-is."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [HumanMessage(content="Hi"), AIMessage(content="Reply")]
    parent_payload = {
        "messages": [
            {"role": "user", "content": "Hi"},
        ]
    }

    mock_value = MagicMock()
    mock_value.to_messages.return_value = messages  # 2 messages vs 1 payload entry

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", return_value=mock_value):
            result = deepseek_instance._get_request_payload(messages)

    assert result is parent_payload


@pytest.mark.unit
def test_reasoning_passback_returns_unchanged_on_convert_input_error(deepseek_instance) -> None:
    """Exception in _convert_input is swallowed and original payload is returned."""
    from langchain_core.messages import HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [HumanMessage(content="Hi")]
    parent_payload = {"messages": [{"role": "user", "content": "Hi"}]}

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", side_effect=ValueError("oops")):
            result = deepseek_instance._get_request_payload(messages)

    assert result is parent_payload


@pytest.mark.unit
def test_reasoning_passback_fills_missing_reasoning_in_thinking_mode(deepseek_instance) -> None:
    """When session has thinking-mode messages, assistant messages without reasoning_content
    (e.g. diagnostic AIMessages from inject_diagnostic_message) must receive reasoning_content=''
    to satisfy DeepSeek's contract that all assistant turns echo reasoning_content."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_deepseek import ChatDeepSeek

    messages = [
        HumanMessage(content="Turn 1"),
        AIMessage(content="Thinking reply", additional_kwargs={"reasoning_content": "thoughts"}),
        HumanMessage(content="Turn 2"),
        AIMessage(content="Diagnostic error message"),  # no reasoning_content — injected by inject_diagnostic_message
        HumanMessage(content="Turn 3"),
    ]
    parent_payload = {
        "messages": [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Thinking reply"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Diagnostic error message"},
            {"role": "user", "content": "Turn 3"},
        ]
    }

    mock_value = MagicMock()
    mock_value.to_messages.return_value = messages

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        with patch.object(deepseek_instance, "_convert_input", return_value=mock_value):
            result = deepseek_instance._get_request_payload(messages)

    assert result["messages"][1]["reasoning_content"] == "thoughts"
    assert result["messages"][3]["reasoning_content"] == ""  # diagnostic filled with ""
    assert "reasoning_content" not in result["messages"][0]
    assert "reasoning_content" not in result["messages"][2]
    assert "reasoning_content" not in result["messages"][4]


@pytest.mark.unit
def test_reasoning_passback_returns_unchanged_when_payload_has_no_messages(deepseek_instance) -> None:
    """When payload has no 'messages' key, payload is returned as-is."""
    from langchain_core.messages import HumanMessage
    from langchain_deepseek import ChatDeepSeek

    parent_payload: dict = {}

    with patch.object(ChatDeepSeek, "_get_request_payload", return_value=parent_payload):
        result = deepseek_instance._get_request_payload([HumanMessage(content="Hi")])

    assert result is parent_payload


# ---------------------------------------------------------------------------
# Factory pattern — ModelHandler protocol, DeepSeekModelHandler, DefaultModelHandler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_factory_classes_importable() -> None:
    """ModelHandler, DeepSeekModelHandler, DefaultModelHandler must be importable."""
    from atelier.profile_model import (  # noqa: F401
        DefaultModelHandler,
        DeepSeekModelHandler,
        ModelHandler,
    )


@pytest.mark.unit
def test_handler_registry_importable() -> None:
    """_HANDLER_REGISTRY must be importable and be a list."""
    from atelier.profile_model import _HANDLER_REGISTRY

    assert isinstance(_HANDLER_REGISTRY, list)
    assert len(_HANDLER_REGISTRY) >= 2


@pytest.mark.unit
def test_deepseek_handler_always_instantiate_is_true() -> None:
    """DeepSeekModelHandler.always_instantiate must be True."""
    from atelier.profile_model import DeepSeekModelHandler

    assert DeepSeekModelHandler.always_instantiate is True


@pytest.mark.unit
def test_default_handler_always_instantiate_is_false() -> None:
    """DefaultModelHandler.always_instantiate must be False."""
    from atelier.profile_model import DefaultModelHandler

    assert DefaultModelHandler.always_instantiate is False


@pytest.mark.unit
def test_deepseek_handler_can_handle_deepseek_prefix() -> None:
    """DeepSeekModelHandler.can_handle returns True for 'deepseek'."""
    from atelier.profile_model import DeepSeekModelHandler

    handler = DeepSeekModelHandler()
    assert handler.can_handle("deepseek") is True


@pytest.mark.unit
def test_deepseek_handler_cannot_handle_other_prefixes() -> None:
    """DeepSeekModelHandler.can_handle returns False for non-deepseek providers."""
    from atelier.profile_model import DeepSeekModelHandler

    handler = DeepSeekModelHandler()
    assert handler.can_handle("openai") is False
    assert handler.can_handle("anthropic") is False
    assert handler.can_handle("") is False


@pytest.mark.unit
def test_default_handler_can_handle_any_provider() -> None:
    """DefaultModelHandler.can_handle returns True for any provider string."""
    from atelier.profile_model import DefaultModelHandler

    handler = DefaultModelHandler()
    assert handler.can_handle("openai") is True
    assert handler.can_handle("anthropic") is True
    assert handler.can_handle("") is True
    assert handler.can_handle("some-unknown-provider") is True


@pytest.mark.unit
def test_handler_registry_deepseek_before_default() -> None:
    """_HANDLER_REGISTRY must list DeepSeekModelHandler before DefaultModelHandler."""
    from atelier.profile_model import (
        DefaultModelHandler,
        DeepSeekModelHandler,
        _HANDLER_REGISTRY,
    )

    types = [type(h) for h in _HANDLER_REGISTRY]
    assert DeepSeekModelHandler in types
    assert DefaultModelHandler in types
    assert types.index(DeepSeekModelHandler) < types.index(DefaultModelHandler)


@pytest.mark.unit
def test_deepseek_handler_build_raises_import_error_when_lib_absent() -> None:
    """DeepSeekModelHandler.build raises ImportError when langchain_deepseek is not installed."""
    from atelier.profile_model import DeepSeekModelHandler

    handler = DeepSeekModelHandler()
    profile = _make_profile(model="deepseek:deepseek-chat")

    with patch("atelier.profile_model._ChatDeepSeekReasoningPassback", None):
        with pytest.raises(ImportError, match="langchain_deepseek"):
            handler.build(profile, {})


@pytest.mark.unit
def test_default_handler_build_calls_init_chat_model() -> None:
    """DefaultModelHandler.build delegates to init_chat_model with profile.model and base_kwargs."""
    from atelier.profile_model import DefaultModelHandler

    handler = DefaultModelHandler()
    profile = _make_profile(model="openai:gpt-4")
    fake_model = MagicMock()

    with patch("atelier.profile_model.init_chat_model", return_value=fake_model) as mock_init:
        result = handler.build(profile, {"base_url": "http://localhost"})

    mock_init.assert_called_once_with("openai:gpt-4", base_url="http://localhost")
    assert result is fake_model
