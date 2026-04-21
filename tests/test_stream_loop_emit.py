"""Unit tests for emit_text and emit_thinking helpers in atelier.stream_loop — TDD RED first.

Tests validate:
- emit_text is importable from atelier.stream_loop
- emit_thinking is importable from atelier.stream_loop
- emit_text returns current_section + text when final_only=True
- emit_text calls buf.add and returns current_section unchanged when final_only=False
- emit_text does not call buf.add when final_only=True
- emit_thinking returns current_section unchanged when thinking event is disabled
- emit_thinking returns current_section unchanged when raw has no thinking blocks
- emit_thinking returns current_section + wrapped when final_only=True and thinking enabled
- emit_thinking calls buf.add when final_only=False and thinking enabled
- emit_thinking does not modify current_section when final_only=False
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    """When final_only=True, returns current_section + text without touching buf."""
    from atelier.stream_loop import emit_text

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_text(text="hello", buf=buf, current_section="prev ", final_only=True)
    assert result == "prev hello"
    buf.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_calls_buf_add_when_not_final_only() -> None:
    """When final_only=False, calls buf.add(text) and returns current_section unchanged."""
    from atelier.stream_loop import emit_text

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_text(text="world", buf=buf, current_section="section", final_only=False)
    buf.add.assert_called_once_with("world")
    assert result == "section"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_text_empty_text_when_final_only() -> None:
    """Returns current_section unchanged when text is empty and final_only=True."""
    from atelier.stream_loop import emit_text

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_text(text="", buf=buf, current_section="existing", final_only=True)
    assert result == "existing"
    buf.add.assert_not_called()


# ---------------------------------------------------------------------------
# emit_thinking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_returns_unchanged_when_disabled() -> None:
    """Returns current_section unchanged when thinking event is disabled."""
    from atelier.stream_loop import emit_thinking

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "deep thoughts"}],
        buf=buf,
        current_section="section",
        thinking_enabled=False,
        final_only=False,
    )
    assert result == "section"
    buf.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_returns_unchanged_when_no_thinking_blocks() -> None:
    """Returns current_section unchanged when raw has no thinking blocks."""
    from atelier.stream_loop import emit_thinking

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_thinking(
        raw="plain text content",
        buf=buf,
        current_section="section",
        thinking_enabled=True,
        final_only=False,
    )
    assert result == "section"
    buf.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_appends_to_current_section_when_final_only() -> None:
    """When final_only=True and thinking enabled, appends wrapped thinking to current_section."""
    from atelier.stream_loop import emit_thinking

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "my thought"}],
        buf=buf,
        current_section="text",
        thinking_enabled=True,
        final_only=True,
    )
    assert "my thought" in result
    assert result.startswith("text")
    buf.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_emit_thinking_calls_buf_add_when_not_final_only() -> None:
    """When final_only=False and thinking enabled, calls buf.add with wrapped thinking."""
    from atelier.stream_loop import emit_thinking

    buf = MagicMock()
    buf.add = AsyncMock()
    result = await emit_thinking(
        raw=[{"type": "thinking", "thinking": "some thought"}],
        buf=buf,
        current_section="unchanged",
        thinking_enabled=True,
        final_only=False,
    )
    buf.add.assert_called_once()
    called_arg = buf.add.call_args[0][0]
    assert "some thought" in called_arg
    # current_section should not be modified when not final_only
    assert result == "unchanged"
