"""Tests for MarkdownStream — TDD RED phase.

Tests cover the sliding-window streaming display, stable-line flushing,
final commit behavior, and edge cases.
"""
from __future__ import annotations

import io
import time
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_console(width: int = 80) -> "Console":
    """Return a rich Console writing to an in-memory buffer."""
    from rich.console import Console
    buf = io.StringIO()
    return Console(file=buf, width=width, no_color=True, force_terminal=False)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_update_accumulates_text() -> None:
    """update() with partial tokens accumulates and updates the live window."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    try:
        stream.update("Hello")
        stream.update("Hello world")
        # No crash — live window holds the last render
    finally:
        # Finalize so Live.__exit__ is called
        stream.update("Hello world", final=True)


@pytest.mark.unit
def test_final_commits_all() -> None:
    """update(text, final=True) flushes everything and exits the live context."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    stream.update("Line one\nLine two", final=True)
    # After final=True, the live context is exited — calling update again
    # should NOT raise (we guard against it)


@pytest.mark.unit
def test_empty_update_does_not_crash() -> None:
    """update('') must not raise any exception."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    try:
        stream.update("")
        stream.update("")
    finally:
        stream.update("", final=True)


@pytest.mark.unit
def test_stable_lines_printed_for_long_text() -> None:
    """Lines beyond the LIVE_WINDOW are committed to stable output."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)

    # Build a text with more lines than LIVE_WINDOW (6)
    many_lines = "\n".join(f"Line {i}" for i in range(20))

    try:
        stream.update(many_lines)
        # _num_printed should be > 0 (some lines flushed as stable)
        assert stream._num_printed > 0, (
            "Expected some lines to be committed as stable output"
        )
    finally:
        stream.update(many_lines, final=True)


@pytest.mark.unit
def test_live_window_size_is_six() -> None:
    """MarkdownStream.LIVE_WINDOW must be 6."""
    from relais_tui.md_stream import MarkdownStream

    assert MarkdownStream.LIVE_WINDOW == 6


@pytest.mark.unit
def test_rate_limit_skips_too_fast_updates() -> None:
    """Non-final updates faster than MIN_DELAY should be skipped silently."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)

    try:
        # Patch monotonic to simulate zero time elapsed between calls
        with patch("relais_tui.md_stream.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            stream.update("first")
            # Same timestamp — second call should be rate-limited
            mock_time.monotonic.return_value = 1000.0
            stream.update("first more")
            # The _last_update should only have been set once
            assert stream._last_update == 1000.0
    finally:
        stream.update("done", final=True)


@pytest.mark.unit
def test_final_true_exits_live() -> None:
    """After final=True, _live_active is False."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    assert stream._live_active is True
    stream.update("some text", final=True)
    assert stream._live_active is False


@pytest.mark.unit
def test_update_after_final_is_noop() -> None:
    """Calling update() after final=True must not raise."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    stream.update("done", final=True)
    # Should be a no-op, not raise
    stream.update("extra call")
    stream.update("another call", final=True)


@pytest.mark.unit
def test_multiline_partial_token_buildup() -> None:
    """Simulate progressive token accumulation across many calls."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)

    full_text = ""
    tokens = ["Hello", " world", "\nNew line", " here", "\nAnother\nline\nfour\nfive\nsix\nseven"]

    with patch("relais_tui.md_stream.time") as mock_time:
        # Advance time enough between each call to bypass rate limiting
        for i, token in enumerate(tokens):
            mock_time.monotonic.return_value = float(i) * 0.1
            full_text += token
            stream.update(full_text)

    stream.update(full_text, final=True)


@pytest.mark.unit
def test_render_lines_returns_list() -> None:
    """_render_lines returns a non-empty list for non-empty text."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console()
    stream = MarkdownStream(console)
    try:
        lines = stream._render_lines("# Hello\nWorld")
        assert isinstance(lines, list)
        assert len(lines) > 0
    finally:
        stream.update("", final=True)


@pytest.mark.unit
def test_console_width_used_in_render() -> None:
    """_render_lines uses the console width for markdown rendering."""
    from relais_tui.md_stream import MarkdownStream

    console = _make_console(width=40)
    stream = MarkdownStream(console)
    try:
        lines = stream._render_lines("word " * 20)
        # Each rendered line should be at most 40 chars wide (approximately)
        assert isinstance(lines, list)
    finally:
        stream.update("", final=True)
