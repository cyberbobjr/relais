"""Tests for Phase 6 TUI improvements — TDD RED phase.

Covers:
  6a: RichLog → Log widget migration
  6b: last_session_id persistence in Config
  6c: fetch_history() in RelaisClient
  6d: /resume session switch
  6e: /clear generates new session_id
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from relais_tui.client import RelaisClient
from relais_tui.config import Config, load_config, save_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> Config:
    """Build a Config with test defaults."""
    defaults: dict[str, Any] = {
        "api_url": "http://localhost:8080",
        "api_key": "test-key-123",
        "request_timeout": 10,
    }
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Phase 6b — last_session_id in Config
# ---------------------------------------------------------------------------


class TestLastSessionIdField:
    """Config must expose and persist last_session_id."""

    def test_default_is_empty_string(self) -> None:
        """last_session_id defaults to empty string, not None."""
        cfg = Config()
        assert cfg.last_session_id == ""

    def test_custom_value_accepted(self) -> None:
        """last_session_id can be set to a UUID string."""
        cfg = Config(last_session_id="abc-123")
        assert cfg.last_session_id == "abc-123"

    def test_frozen_rejects_mutation(self) -> None:
        """Config is still frozen — no direct mutation allowed."""
        cfg = Config(last_session_id="abc")
        with pytest.raises(FrozenInstanceError):
            cfg.last_session_id = "other"  # type: ignore[misc]

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """last_session_id survives a save/load roundtrip."""
        config_path = tmp_path / "config.yaml"
        original = Config(last_session_id="session-uuid-1234")
        save_config(original, config_path)
        loaded = load_config(config_path)
        assert loaded.last_session_id == "session-uuid-1234"

    def test_empty_string_roundtrip(self, tmp_path: Path) -> None:
        """Empty last_session_id saves/loads as empty string."""
        config_path = tmp_path / "config.yaml"
        original = Config(last_session_id="")
        save_config(original, config_path)
        loaded = load_config(config_path)
        assert loaded.last_session_id == ""

    def test_missing_key_in_yaml_defaults_to_empty(self, tmp_path: Path) -> None:
        """YAML file without last_session_id key loads default empty string."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"api_url": "http://x"}))
        cfg = load_config(config_path)
        assert cfg.last_session_id == ""


# ---------------------------------------------------------------------------
# Phase 6c — fetch_history() in RelaisClient
# ---------------------------------------------------------------------------


def _mock_http_get(
    *,
    status_code: int = 200,
    json_data: dict | None = None,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Build an AsyncMock for ``_http.get`` that returns a fake httpx.Response.

    Args:
        status_code: HTTP status code for the fake response.
        json_data: JSON body dict; if None an empty dict is used.
        side_effect: If set, the mock raises this exception instead.

    Returns:
        Configured AsyncMock suitable for patching ``client._http.get``.
    """
    if side_effect is not None:
        return AsyncMock(side_effect=side_effect)

    request = httpx.Request("GET", "http://test/v1/history")
    body = json.dumps(json_data or {}).encode()
    resp = httpx.Response(status_code, content=body, request=request)
    return AsyncMock(return_value=resp)


class TestFetchHistory:
    """RelaisClient.fetch_history() must return parsed turns or [] on error."""

    @pytest.mark.asyncio
    async def test_returns_turns_on_200(self) -> None:
        """200 response with turns list is parsed and returned."""
        cfg = _config()
        client = RelaisClient(cfg)
        turns = [
            {"user_content": "Hello", "assistant_content": "Hi there"},
            {"user_content": "How are you?", "assistant_content": "Fine"},
        ]
        resp_data = {"turns": turns, "session_id": "s-1", "total": 2}

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(status_code=200, json_data=resp_data)
            result = await client.fetch_history("s-1", limit=20)

        assert result == turns
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self) -> None:
        """Non-200 status returns empty list without raising."""
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(status_code=404)
            result = await client.fetch_history("no-such-session")

        assert result == []
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self) -> None:
        """Network error (any exception) returns empty list without raising."""
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(side_effect=httpx.ConnectError("refused"))
            result = await client.fetch_history("s-1")

        assert result == []
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_empty_when_turns_key_missing(self) -> None:
        """200 response without 'turns' key returns empty list."""
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(
                status_code=200, json_data={"session_id": "s-1"}
            )
            result = await client.fetch_history("s-1")

        assert result == []
        await client.close()

    @pytest.mark.asyncio
    async def test_passes_correct_params(self) -> None:
        """fetch_history sends session_id and limit as query params."""
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(
                status_code=200, json_data={"turns": []}
            )
            await client.fetch_history("my-session", limit=10)

            call_kwargs = mock_http.get.call_args
            url_arg = (
                call_kwargs.args[0] if call_kwargs.args
                else call_kwargs.kwargs.get("url", "")
            )
            params = call_kwargs.kwargs.get("params", {})

        assert "history" in url_arg
        assert params.get("session_id") == "my-session"
        assert params.get("limit") == 10
        await client.close()

    @pytest.mark.asyncio
    async def test_default_limit_is_20(self) -> None:
        """Default limit parameter is 20 when not specified."""
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http") as mock_http:
            mock_http.get = _mock_http_get(
                status_code=200, json_data={"turns": []}
            )
            await client.fetch_history("s-1")

            call_kwargs = mock_http.get.call_args
            params = call_kwargs.kwargs.get("params", {})

        assert params.get("limit") == 20
        await client.close()


# ---------------------------------------------------------------------------
# Phase 6a — _render_markup helper
# ---------------------------------------------------------------------------


class TestRenderMarkup:
    """RelaisApp._render_markup converts Rich markup to a plain string."""

    def test_plain_text_passthrough(self) -> None:
        """Plain text without markup is returned as-is (modulo trailing newline)."""
        from relais_tui.__main__ import RelaisApp
        from relais_tui.config import Config
        from pathlib import Path

        app = RelaisApp.__new__(RelaisApp)
        app._config = Config()
        result = app._render_markup("Hello World")
        assert "Hello World" in result

    def test_bold_markup_converted(self) -> None:
        """[bold]text[/bold] is converted without keeping raw markup tags."""
        from relais_tui.__main__ import RelaisApp

        app = RelaisApp.__new__(RelaisApp)
        app._config = Config()
        result = app._render_markup("[bold]Important[/bold]")
        assert "Important" in result
        # Raw markup tags should not appear in output
        assert "[bold]" not in result

    def test_dim_markup_converted(self) -> None:
        """[dim] markup is processed without keeping raw tags."""
        from relais_tui.__main__ import RelaisApp

        app = RelaisApp.__new__(RelaisApp)
        app._config = Config()
        result = app._render_markup("[dim]subtle[/dim]")
        assert "subtle" in result
        assert "[dim]" not in result

    def test_returns_string(self) -> None:
        """Return type is always str."""
        from relais_tui.__main__ import RelaisApp

        app = RelaisApp.__new__(RelaisApp)
        app._config = Config()
        result = app._render_markup("[green]OK[/green]")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Phase 6b — _save_session_id saves to config
# ---------------------------------------------------------------------------


class TestSaveSessionId:
    """RelaisApp._save_session_id updates config.last_session_id."""

    def test_save_session_id_updates_config(self, tmp_path: Path) -> None:
        """_save_session_id updates self._config and writes to disk."""
        from relais_tui.__main__ import RelaisApp

        config_path = tmp_path / "config.yaml"
        config = Config()
        save_config(config, config_path)

        app = RelaisApp.__new__(RelaisApp)
        app._config = config
        app._config_path = config_path
        app._session_id = "new-session-id-xyz"

        app._save_session_id()

        # Config object is updated
        assert app._config.last_session_id == "new-session-id-xyz"

        # Value is written to disk
        reloaded = load_config(config_path)
        assert reloaded.last_session_id == "new-session-id-xyz"

    def test_save_session_id_none_writes_empty(self, tmp_path: Path) -> None:
        """_save_session_id with None session_id saves empty string."""
        from relais_tui.__main__ import RelaisApp

        config_path = tmp_path / "config.yaml"
        config = Config(last_session_id="old-id")
        save_config(config, config_path)

        app = RelaisApp.__new__(RelaisApp)
        app._config = config
        app._config_path = config_path
        app._session_id = None

        app._save_session_id()

        reloaded = load_config(config_path)
        assert reloaded.last_session_id == ""
