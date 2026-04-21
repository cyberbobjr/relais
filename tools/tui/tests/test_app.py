"""Tests for RelaisApp — TDD RED phase (Cycle 2).

All tests mock the prompt_toolkit Application so no real terminal is opened.
RelaisClient is also mocked throughout.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from relais_tui.config import Config, ThemeConfig
from relais_tui.sse_parser import DoneEvent, ErrorEvent, ProgressEvent, TokenEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> Config:
    """Build a minimal Config for tests."""
    theme = ThemeConfig()
    defaults = dict(
        api_url="http://localhost:8080",
        api_key="test-key",
        request_timeout=10,
        theme=theme,
        last_session_id="",
        history_path="/tmp/relais_test_history",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


# ---------------------------------------------------------------------------
# RelaisApp construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_relais_app_can_be_instantiated() -> None:
    """RelaisApp can be constructed without starting a terminal."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert app is not None


@pytest.mark.unit
def test_relais_app_has_chat_state() -> None:
    """RelaisApp exposes a ChatState instance."""
    from relais_tui.app import RelaisApp
    from relais_tui.chat_state import ChatState

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert isinstance(app.chat_state, ChatState)


@pytest.mark.unit
def test_relais_app_stores_config() -> None:
    """RelaisApp stores the config it was given."""
    from relais_tui.app import RelaisApp

    config = _make_config(api_url="http://example.com:9999")
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert app.config.api_url == "http://example.com:9999"


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_layout_returns_layout_object() -> None:
    """_build_layout returns a prompt_toolkit Layout."""
    from relais_tui.app import RelaisApp
    from prompt_toolkit.layout import Layout

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        layout = app._build_layout()
        assert isinstance(layout, Layout)


@pytest.mark.unit
def test_build_key_bindings_returns_bindings() -> None:
    """_build_key_bindings returns a KeyBindings instance."""
    from relais_tui.app import RelaisApp
    from prompt_toolkit.key_binding import KeyBindings

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        kb = app._build_key_bindings()
        assert isinstance(kb, KeyBindings)


# ---------------------------------------------------------------------------
# get_chat_text — formatted text rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_chat_text_empty_state() -> None:
    """get_chat_text returns a list even with no messages."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        result = app.get_chat_text()
        assert isinstance(result, list)


@pytest.mark.unit
def test_get_chat_text_includes_user_message() -> None:
    """get_chat_text includes user message content in formatted tuples."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app.chat_state.add_message("user", "hello world")
        result = app.get_chat_text()
        # Result is list of (style, text) tuples
        all_text = "".join(text for _, text in result)
        assert "hello world" in all_text


@pytest.mark.unit
def test_get_chat_text_includes_assistant_message() -> None:
    """get_chat_text includes assistant message content."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app.chat_state.add_message("assistant", "I am relais")
        result = app.get_chat_text()
        all_text = "".join(text for _, text in result)
        assert "I am relais" in all_text


@pytest.mark.unit
def test_get_chat_text_includes_role_prefix() -> None:
    """get_chat_text shows role prefix ('you ›' or 'relais ›')."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app.chat_state.add_message("user", "ping")
        app.chat_state.add_message("assistant", "pong")
        result = app.get_chat_text()
        all_text = "".join(text for _, text in result)
        assert "you" in all_text
        assert "relais" in all_text


@pytest.mark.unit
def test_get_chat_text_returns_style_text_tuples() -> None:
    """Each element from get_chat_text is a (str, str) tuple."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app.chat_state.add_message("user", "test")
        result = app.get_chat_text()
        for item in result:
            assert len(item) == 2
            assert isinstance(item[0], str)  # style
            assert isinstance(item[1], str)  # text


# ---------------------------------------------------------------------------
# _add_user_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_user_message_puts_message_in_state() -> None:
    """_add_user_message adds a user ChatMessage to chat_state."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app._add_user_message("test input")
        assert len(app.chat_state.messages) == 1
        assert app.chat_state.messages[0].role == "user"
        assert app.chat_state.messages[0].content == "test input"


@pytest.mark.unit
def test_add_user_message_triggers_invalidate() -> None:
    """_add_user_message calls app.invalidate() via the listener."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app._add_user_message("hello")
        # invalidate should have been called (via chat_state listener)
        mock_app_inst.invalidate.assert_called()


# ---------------------------------------------------------------------------
# _handle_submit — command routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_submit_ignores_empty_input() -> None:
    """_handle_submit does nothing when the input is empty."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        # Must not raise and must not add any message
        app._handle_submit("")
        app._handle_submit("   ")
        assert len(app.chat_state.messages) == 0


@pytest.mark.unit
def test_handle_submit_slash_exit_sets_exit_flag() -> None:
    """_handle_submit('/exit') sets the should_exit flag."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app._handle_submit("/exit")
        assert app._should_exit is True


@pytest.mark.unit
def test_handle_submit_slash_quit_sets_exit_flag() -> None:
    """_handle_submit('/quit') also sets should_exit."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        app._handle_submit("/quit")
        assert app._should_exit is True


@pytest.mark.unit
def test_handle_submit_normal_message_adds_to_state() -> None:
    """_handle_submit with a normal message adds it to chat_state."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        with patch.object(RelaisApp, "_launch_stream_task"):
            app = RelaisApp(config, Path("/tmp/cfg.yaml"))
            app._handle_submit("hello relais")
            assert len(app.chat_state.messages) >= 1
            assert app.chat_state.messages[0].content == "hello relais"


@pytest.mark.unit
def test_handle_submit_normal_message_launches_stream_task() -> None:
    """_handle_submit with a normal message calls _launch_stream_task."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        with patch.object(RelaisApp, "_launch_stream_task") as mock_launch:
            app = RelaisApp(config, Path("/tmp/cfg.yaml"))
            app._handle_submit("ask something")
            mock_launch.assert_called_once()


# ---------------------------------------------------------------------------
# _stream_to_state — async streaming pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_to_state_appends_tokens(tmp_path) -> None:
    """_stream_to_state appends token events to the last assistant message."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield TokenEvent(text="Hello")
            yield TokenEvent(text=" world")
            yield DoneEvent(content="Hello world", correlation_id="c1", session_id="s1")

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("user", "hi")
        asst = app.chat_state.add_message("assistant", "")

        await app._stream_to_state("hi", None)
        assert "Hello world" in asst.content or asst.content == "Hello world"


@pytest.mark.asyncio
async def test_stream_to_state_updates_session_id(tmp_path) -> None:
    """_stream_to_state stores the new session_id from DoneEvent."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield DoneEvent(content="hi", correlation_id="c1", session_id="new-sess")

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("assistant", "")

        await app._stream_to_state("ping", None)
        assert app._session_id == "new-sess"


@pytest.mark.asyncio
async def test_stream_to_state_handles_error_event(tmp_path) -> None:
    """_stream_to_state does not raise on ErrorEvent."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield ErrorEvent(error="server exploded", correlation_id="c1")

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("assistant", "")

        # Must not raise
        await app._stream_to_state("oops", None)


@pytest.mark.asyncio
async def test_stream_to_state_handles_progress_event(tmp_path) -> None:
    """_stream_to_state tolerates ProgressEvent without crashing."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield ProgressEvent(event="thinking", detail="…")
            yield TokenEvent(text="Done")
            yield DoneEvent(content="Done", correlation_id="c1", session_id="s1")

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("assistant", "")

        await app._stream_to_state("go", None)


@pytest.mark.asyncio
async def test_stream_to_state_connection_error_does_not_raise(tmp_path) -> None:
    """_stream_to_state catches network errors and does not propagate them."""
    import httpx
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _failing_stream(*args, **kwargs):
            raise httpx.ConnectError("unreachable")
            yield  # make it a generator

        mock_client = MagicMock()
        mock_client.stream_message = _failing_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("assistant", "")

        await app._stream_to_state("hello", None)  # should not raise


# ---------------------------------------------------------------------------
# _do_clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_clear_clears_chat_state(tmp_path) -> None:
    """_do_clear empties chat_state messages."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield DoneEvent(
                content="Cleared.", correlation_id="c1", session_id="fresh-id"
            )

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client
        app.chat_state.add_message("user", "old message")

        await app._do_clear()
        assert len(app.chat_state.messages) == 0


@pytest.mark.asyncio
async def test_do_clear_updates_session_id(tmp_path) -> None:
    """_do_clear sets a fresh session_id."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        async def _fake_stream(*args, **kwargs):
            yield DoneEvent(
                content="Cleared.", correlation_id="c1", session_id="brand-new"
            )

        mock_client = MagicMock()
        mock_client.stream_message = _fake_stream

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app._do_clear()
        assert app._session_id is not None
        assert isinstance(app._session_id, str)


# ---------------------------------------------------------------------------
# _show_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_history_populates_chat_state(tmp_path) -> None:
    """_show_history adds prior turns to chat_state."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        mock_client = AsyncMock()
        mock_client.fetch_history.return_value = [
            {"user_content": "prev user msg", "assistant_content": "prev reply"},
        ]

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app._show_history("some-session-id")
        messages = app.chat_state.messages
        assert any(m.content == "prev user msg" for m in messages)
        assert any(m.content == "prev reply" for m in messages)


@pytest.mark.asyncio
async def test_show_history_empty_does_nothing(tmp_path) -> None:
    """_show_history with no turns leaves chat_state empty."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = MagicMock()
        MockApp.return_value = mock_app_inst

        mock_client = AsyncMock()
        mock_client.fetch_history.return_value = []

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app._show_history("empty-session")
        assert app.chat_state.messages == []


# ---------------------------------------------------------------------------
# run — top-level async entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_performs_health_check(tmp_path) -> None:
    """run() calls client.healthz() at startup."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = AsyncMock()
        mock_app_inst.run_async = AsyncMock()
        MockApp.return_value = mock_app_inst

        mock_client = AsyncMock()
        mock_client.healthz.return_value = True
        mock_client.fetch_history.return_value = []
        mock_client.close = AsyncMock()

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app.run()

        mock_client.healthz.assert_called_once()


@pytest.mark.asyncio
async def test_run_calls_close_on_exit(tmp_path) -> None:
    """run() always calls client.close() after the app exits."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = AsyncMock()
        mock_app_inst.run_async = AsyncMock()
        MockApp.return_value = mock_app_inst

        mock_client = AsyncMock()
        mock_client.healthz.return_value = False
        mock_client.close = AsyncMock()

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app.run()

        mock_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_shows_history_when_session_exists(tmp_path) -> None:
    """run() loads history if last_session_id is set in config."""
    from relais_tui.app import RelaisApp

    config = _make_config(last_session_id="prev-session-abc")
    with patch("relais_tui.app.Application") as MockApp:
        mock_app_inst = AsyncMock()
        mock_app_inst.run_async = AsyncMock()
        MockApp.return_value = mock_app_inst

        mock_client = AsyncMock()
        mock_client.healthz.return_value = True
        mock_client.fetch_history.return_value = []
        mock_client.close = AsyncMock()

        app = RelaisApp(config, tmp_path / "cfg.yaml")
        app._client = mock_client

        await app.run()

        mock_client.fetch_history.assert_called_once()


# ---------------------------------------------------------------------------
# should_exit flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_should_exit_default_false() -> None:
    """_should_exit is False by default."""
    from relais_tui.app import RelaisApp

    config = _make_config()
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert app._should_exit is False


@pytest.mark.unit
def test_session_id_initialised_from_config() -> None:
    """_session_id is initialized from config.last_session_id."""
    from relais_tui.app import RelaisApp

    config = _make_config(last_session_id="abc-123")
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert app._session_id == "abc-123"


@pytest.mark.unit
def test_session_id_none_when_config_empty() -> None:
    """_session_id is None when last_session_id is empty string."""
    from relais_tui.app import RelaisApp

    config = _make_config(last_session_id="")
    with patch("relais_tui.app.Application"):
        app = RelaisApp(config, Path("/tmp/cfg.yaml"))
        assert app._session_id is None
