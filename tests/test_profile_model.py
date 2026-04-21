"""Unit tests for atelier.profile_model — TDD RED first.

Tests validate:
- _resolve_profile_model is importable from atelier.profile_model
- When neither base_url nor api_key_env is set, returns model string directly
- When base_url is set, constructs a BaseChatModel via init_chat_model
- When api_key_env is set and present in env, passes api_key to init_chat_model
- When api_key_env is set but absent from env, raises KeyError
- When parallel_tool_calls is set, passes it via model_kwargs
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
) -> MagicMock:
    """Return a minimal ProfileConfig mock."""
    p = MagicMock()
    p.model = model
    p.base_url = base_url
    p.api_key_env = api_key_env
    p.parallel_tool_calls = parallel_tool_calls
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
