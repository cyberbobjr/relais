"""Integration tests for the prompt_toolkit + rich REPL loop.

Tests verify that run_app / _stream_response / _do_clear behave correctly
with a mocked RelaisClient, without starting a real network connection.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from relais_tui.config import Config, ThemeConfig
from relais_tui.sse_parser import DoneEvent, ErrorEvent, ProgressEvent, TokenEvent


def _make_config(**kwargs) -> Config:
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


def _make_console() -> Console:
    buf = io.StringIO()
    return Console(file=buf, width=80, no_color=True, force_terminal=False)


# ---------------------------------------------------------------------------
# _make_key_bindings
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_key_bindings_returned() -> None:
    """_make_key_bindings returns a non-None KeyBindings instance."""
    from relais_tui.__main__ import _make_key_bindings
    from prompt_toolkit.key_binding import KeyBindings

    kb = _make_key_bindings()
    assert isinstance(kb, KeyBindings)


# ---------------------------------------------------------------------------
# _make_prompt_message
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_prompt_message_contains_you() -> None:
    """_make_prompt_message includes 'you' in the formatted text."""
    from relais_tui.__main__ import _make_prompt_message
    from prompt_toolkit.formatted_text import HTML

    config = _make_config()
    msg = _make_prompt_message(config.theme)
    assert isinstance(msg, HTML)
    assert "you" in str(msg)


# ---------------------------------------------------------------------------
# _make_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_make_session_returns_prompt_session(tmp_path) -> None:
    """_make_session returns a PromptSession."""
    from relais_tui.__main__ import _make_session
    from prompt_toolkit import PromptSession

    config = _make_config(history_path=str(tmp_path / "history"))
    sess = _make_session(config)
    assert isinstance(sess, PromptSession)


# ---------------------------------------------------------------------------
# _stream_response — token streaming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_response_accumulates_tokens(tmp_path) -> None:
    """_stream_response concatenates token events and returns the full reply."""
    from relais_tui.__main__ import _stream_response

    config = _make_config()
    console = _make_console()

    async def _fake_stream(*args, **kwargs):
        yield TokenEvent(text="Hello")
        yield TokenEvent(text=" world")
        yield DoneEvent(content="Hello world", correlation_id="c1", session_id="s1")

    mock_client = MagicMock()
    mock_client.stream_message = _fake_stream

    new_sid, reply = await _stream_response(
        mock_client, "hi", None, console, config, tmp_path / "config.yaml"
    )
    assert "Hello world" in reply or reply == "Hello world"


@pytest.mark.asyncio
async def test_stream_response_updates_session_id(tmp_path) -> None:
    """_stream_response returns the new session_id from DoneEvent."""
    from relais_tui.__main__ import _stream_response

    config = _make_config()
    console = _make_console()

    async def _fake_stream(*args, **kwargs):
        yield TokenEvent(text="Hi")
        yield DoneEvent(content="Hi", correlation_id="c1", session_id="new-sess-123")

    mock_client = MagicMock()
    mock_client.stream_message = _fake_stream

    new_sid, _ = await _stream_response(
        mock_client, "hello", "old-sess", console, config, tmp_path / "config.yaml"
    )
    assert new_sid == "new-sess-123"


@pytest.mark.asyncio
async def test_stream_response_handles_error_event(tmp_path) -> None:
    """_stream_response does not raise on ErrorEvent; error is printed."""
    from relais_tui.__main__ import _stream_response

    config = _make_config()
    console = _make_console()

    async def _fake_stream(*args, **kwargs):
        yield ErrorEvent(error="Something went wrong", correlation_id="c1")

    mock_client = MagicMock()
    mock_client.stream_message = _fake_stream

    new_sid, reply = await _stream_response(
        mock_client, "fail me", None, console, config, tmp_path / "config.yaml"
    )
    # Should not raise; error output goes to console
    assert new_sid is None or isinstance(new_sid, str)


@pytest.mark.asyncio
async def test_stream_response_handles_progress_event(tmp_path) -> None:
    """_stream_response prints progress events without raising."""
    from relais_tui.__main__ import _stream_response

    config = _make_config()
    console = _make_console()

    async def _fake_stream(*args, **kwargs):
        yield ProgressEvent(event="thinking", detail="…")
        yield TokenEvent(text="Done")
        yield DoneEvent(content="Done", correlation_id="c1", session_id="s1")

    mock_client = MagicMock()
    mock_client.stream_message = _fake_stream

    new_sid, reply = await _stream_response(
        mock_client, "go", None, console, config, tmp_path / "config.yaml"
    )
    assert "Done" in reply


# ---------------------------------------------------------------------------
# _do_clear
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_do_clear_returns_new_session_id(tmp_path) -> None:
    """_do_clear returns a fresh session ID string."""
    from relais_tui.__main__ import _do_clear

    config = _make_config()
    console = _make_console()
    cfg_path = tmp_path / "config.yaml"

    async def _fake_stream(*args, **kwargs):
        yield DoneEvent(
            content="Conversation history cleared.",
            correlation_id="c1",
            session_id="server-new-id",
        )

    mock_client = MagicMock()
    mock_client.stream_message = _fake_stream

    new_sid = await _do_clear(mock_client, "old-sid", console, config, cfg_path)
    assert isinstance(new_sid, str)
    assert len(new_sid) > 0


# ---------------------------------------------------------------------------
# run_app exit path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_app_exits_on_eof(tmp_path) -> None:
    """run_app exits cleanly when prompt raises EOFError (Ctrl+D)."""
    from relais_tui.__main__ import run_app

    config = _make_config(history_path=str(tmp_path / "history"))
    cfg_path = tmp_path / "config.yaml"
    log_path = tmp_path / "tui.log"

    with (
        patch("relais_tui.__main__.RelaisClient") as MockClient,
        patch("relais_tui.__main__._make_session") as mock_make_session,
        patch("relais_tui.__main__.Console") as MockConsole,
    ):
        mock_client_inst = AsyncMock()
        mock_client_inst.healthz.return_value = True
        mock_client_inst.close = AsyncMock()
        MockClient.return_value = mock_client_inst

        mock_session = AsyncMock()
        mock_session.prompt_async.side_effect = EOFError()
        mock_make_session.return_value = mock_session

        mock_console_inst = MagicMock()
        mock_console_inst.print = MagicMock()
        MockConsole.return_value = mock_console_inst

        await run_app(config, cfg_path, log_path)

    mock_client_inst.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_app_exits_on_slash_exit(tmp_path) -> None:
    """run_app exits cleanly when user types /exit."""
    from relais_tui.__main__ import run_app

    config = _make_config(history_path=str(tmp_path / "history"))
    cfg_path = tmp_path / "config.yaml"
    log_path = tmp_path / "tui.log"

    with (
        patch("relais_tui.__main__.RelaisClient") as MockClient,
        patch("relais_tui.__main__._make_session") as mock_make_session,
        patch("relais_tui.__main__.Console") as MockConsole,
    ):
        mock_client_inst = AsyncMock()
        mock_client_inst.healthz.return_value = False
        mock_client_inst.close = AsyncMock()
        MockClient.return_value = mock_client_inst

        mock_session = AsyncMock()
        mock_session.prompt_async.return_value = "/exit"
        mock_make_session.return_value = mock_session

        mock_console_inst = MagicMock()
        mock_console_inst.print = MagicMock()
        MockConsole.return_value = mock_console_inst

        await run_app(config, cfg_path, log_path)

    mock_client_inst.close.assert_called_once()
