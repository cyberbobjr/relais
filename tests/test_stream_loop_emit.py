"""Unit tests for emit_text and emit_thinking helpers in atelier.stream_loop.

Tests validate:
- emit_text is importable from atelier.stream_loop
- emit_thinking is importable from atelier.stream_loop
- emit_text returns current_section + text when final_only=True
- emit_text calls callback directly and returns current_section unchanged when final_only=False
- emit_text does not call callback when final_only=True
- emit_thinking returns current_section unchanged when thinking event is disabled
- emit_thinking returns current_section unchanged when raw has no thinking blocks
- emit_thinking returns current_section + wrapped when final_only=True and thinking enabled
- emit_thinking calls callback when final_only=False and thinking enabled
- emit_thinking does not modify current_section when final_only=False
- emit_text(callback=AsyncMock) calls callback(text) once when not final_only
- emit_text(callback=None) does not raise when not final_only
- emit_thinking(callback=AsyncMock) calls callback once when not final_only and thinking enabled
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_text_importable() -> None:
    """emit_text must be importable from atelier.stream_loop."""
    from atelier.stream_loop import emit_text  # noqa: F401


@pytest.mark.unit
def test_emit_thinking_importable() -> None:
    """emit_thinking must be importable from atelier.stream_loop."""
    from atelier.stream_loop import emit_thinking  # noqa: F401


# ---------------------------------------------------------------------------
# emit_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_appends_to_current_section_when_final_only() -> None:
    """When final_only=True, returns current_section + text without calling callback."""
    from atelier.stream_loop import emit_text

    cb = AsyncMock()
    result = await emit_text(text="hello", callback=cb, current_section="prev ", final_only=True)
    assert result == "prev hello"
    cb.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_calls_callback_when_not_final_only() -> None:
    """When final_only=False, calls callback(text) and returns current_section unchanged."""
    from atelier.stream_loop import emit_text

    cb = AsyncMock()
    result = await emit_text(text="world", callback=cb, current_section="section", final_only=False)
    cb.assert_called_once_with("world")
    assert result == "section"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_empty_text_when_final_only() -> None:
    """Returns current_section unchanged when text is empty and final_only=True."""
    from atelier.stream_loop import emit_text

    cb = AsyncMock()
    result = await emit_text(text="", callback=cb, current_section="existing", final_only=True)
    assert result == "existing"
    cb.assert_not_called()


# ---------------------------------------------------------------------------
# emit_thinking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_returns_unchanged_when_disabled() -> None:
    """Returns current_section unchanged when thinking event is disabled."""
    from atelier.stream_loop import emit_thinking

    cb = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "deep thoughts"}],
        callback=cb,
        current_section="section",
        thinking_enabled=False,
        final_only=False,
    )
    assert result == "section"
    cb.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_returns_unchanged_when_no_thinking_blocks() -> None:
    """Returns current_section unchanged when raw has no thinking blocks."""
    from atelier.stream_loop import emit_thinking

    cb = AsyncMock()
    result = await emit_thinking(
        raw="plain text content",
        callback=cb,
        current_section="section",
        thinking_enabled=True,
        final_only=False,
    )
    assert result == "section"
    cb.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_appends_to_current_section_when_final_only() -> None:
    """When final_only=True and thinking enabled, appends wrapped thinking to current_section."""
    from atelier.stream_loop import emit_thinking

    cb = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "my thought"}],
        callback=cb,
        current_section="text",
        thinking_enabled=True,
        final_only=True,
    )
    assert "my thought" in result
    assert result.startswith("text")
    cb.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_calls_callback_when_not_final_only() -> None:
    """When final_only=False and thinking enabled, calls callback with wrapped thinking."""
    from atelier.stream_loop import emit_thinking

    cb = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "some thought"}],
        callback=cb,
        current_section="unchanged",
        thinking_enabled=True,
        final_only=False,
    )
    cb.assert_called_once()
    called_arg = cb.call_args[0][0]
    assert "some thought" in called_arg
    # current_section should not be modified when not final_only
    assert result == "unchanged"


# ---------------------------------------------------------------------------
# Callback-based API (direct, no intermediate buffer).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_callback_called_directly_when_not_final_only() -> None:
    """emit_text with callback=AsyncMock calls callback(text) once when not final_only."""
    from atelier.stream_loop import emit_text

    cb = AsyncMock()
    result = await emit_text(text="hello", callback=cb, current_section="", final_only=False)
    cb.assert_called_once_with("hello")
    assert result == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_callback_none_does_not_raise_when_not_final_only() -> None:
    """emit_text with callback=None does not raise when final_only=False."""
    from atelier.stream_loop import emit_text

    result = await emit_text(text="x", callback=None, current_section="", final_only=False)
    assert result == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_callback_not_called_when_final_only() -> None:
    """emit_text with callback never calls it when final_only=True."""
    from atelier.stream_loop import emit_text

    cb = AsyncMock()
    result = await emit_text(text="hi", callback=cb, current_section="prev ", final_only=True)
    cb.assert_not_called()
    assert result == "prev hi"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_callback_called_directly_when_not_final_only() -> None:
    """emit_thinking with callback=AsyncMock calls callback once when thinking enabled, not final_only."""
    from atelier.stream_loop import emit_thinking

    cb = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "deep thought"}],
        callback=cb,
        current_section="",
        thinking_enabled=True,
        final_only=False,
    )
    cb.assert_called_once()
    called_arg = cb.call_args[0][0]
    assert "deep thought" in called_arg
    assert result == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_callback_none_does_not_raise() -> None:
    """emit_thinking with callback=None does not raise."""
    from atelier.stream_loop import emit_thinking

    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "thought"}],
        callback=None,
        current_section="",
        thinking_enabled=True,
        final_only=False,
    )
    assert result == ""
