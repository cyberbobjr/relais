"""Unit tests for atelier.streaming — TDD RED first.

Tests StreamBuffer, _extract_block_type, _extract_thinking, and _has_tool_use_block.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# StreamBuffer
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_buffer_flushes_when_threshold_reached() -> None:
    """StreamBuffer.add() calls callback when buffer reaches threshold."""
    received: list[str] = []

    async def cb(chunk: str) -> None:
        received.append(chunk)

    from atelier.streaming import StreamBuffer

    buf = StreamBuffer(flush_threshold=5, callback=cb)
    await buf.add("hello")  # len == 5, threshold reached
    assert received == ["hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_buffer_does_not_flush_below_threshold() -> None:
    """StreamBuffer.add() does not flush when buffer is below threshold."""
    received: list[str] = []

    async def cb(chunk: str) -> None:
        received.append(chunk)

    from atelier.streaming import StreamBuffer

    buf = StreamBuffer(flush_threshold=10, callback=cb)
    await buf.add("hi")  # len == 2, below threshold
    assert received == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_buffer_flush_empties_buffer() -> None:
    """StreamBuffer.flush() drains the buffer exactly once."""
    received: list[str] = []

    async def cb(chunk: str) -> None:
        received.append(chunk)

    from atelier.streaming import StreamBuffer

    buf = StreamBuffer(flush_threshold=100, callback=cb)
    await buf.add("abc")
    await buf.flush()
    assert received == ["abc"]
    # Second flush should not emit empty string
    await buf.flush()
    assert received == ["abc"]


# ---------------------------------------------------------------------------
# _extract_block_type
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_block_type_returns_thinking_texts() -> None:
    """_extract_block_type(raw, 'thinking') collects all 'thinking' field values."""
    from atelier.streaming import _extract_block_type

    raw = [
        {"type": "thinking", "thinking": "step one"},
        {"type": "text", "text": "hello"},
        {"type": "thinking", "thinking": "step two"},
    ]
    result = _extract_block_type(raw, "thinking")
    assert result == ["step one", "step two"]


@pytest.mark.unit
def test_extract_block_type_returns_tool_use_names() -> None:
    """_extract_block_type(raw, 'tool_use') collects all 'name' field values."""
    from atelier.streaming import _extract_block_type

    raw = [
        {"type": "tool_use", "name": "search", "id": "c1"},
        {"type": "text", "text": "calling tool"},
        {"type": "tool_use", "name": "calculator", "id": "c2"},
    ]
    result = _extract_block_type(raw, "tool_use")
    assert result == ["search", "calculator"]


@pytest.mark.unit
def test_extract_block_type_non_list_returns_empty() -> None:
    """_extract_block_type returns [] for non-list raw input."""
    from atelier.streaming import _extract_block_type

    assert _extract_block_type("some string", "thinking") == []
    assert _extract_block_type(None, "thinking") == []
    assert _extract_block_type(42, "tool_use") == []


@pytest.mark.unit
def test_extract_block_type_no_matching_blocks_returns_empty() -> None:
    """_extract_block_type returns [] when no block of the given type exists."""
    from atelier.streaming import _extract_block_type

    raw = [{"type": "text", "text": "hi"}]
    assert _extract_block_type(raw, "thinking") == []


@pytest.mark.unit
def test_extract_block_type_skips_non_dict_items() -> None:
    """_extract_block_type skips list items that are not dicts."""
    from atelier.streaming import _extract_block_type

    raw = [
        "not a dict",
        42,
        {"type": "thinking", "thinking": "valid"},
        None,
    ]
    result = _extract_block_type(raw, "thinking")
    assert result == ["valid"]


# ---------------------------------------------------------------------------
# _extract_thinking (delegates to _extract_block_type)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_thinking_returns_concatenated_text() -> None:
    """_extract_thinking() returns all thinking blocks joined."""
    from atelier.streaming import _extract_thinking

    raw = [
        {"type": "thinking", "thinking": "first thought"},
        {"type": "text", "text": "response"},
        {"type": "thinking", "thinking": " second thought"},
    ]
    result = _extract_thinking(raw)
    assert result == "first thought second thought"


@pytest.mark.unit
def test_extract_thinking_non_list_returns_empty_string() -> None:
    """_extract_thinking() returns '' for non-list input."""
    from atelier.streaming import _extract_thinking

    assert _extract_thinking("plain string") == ""
    assert _extract_thinking(None) == ""


@pytest.mark.unit
def test_extract_thinking_no_thinking_blocks_returns_empty_string() -> None:
    """_extract_thinking() returns '' when no thinking blocks present."""
    from atelier.streaming import _extract_thinking

    raw = [{"type": "text", "text": "hello"}]
    assert _extract_thinking(raw) == ""


# ---------------------------------------------------------------------------
# _has_tool_use_block (delegates to _extract_block_type)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_has_tool_use_block_returns_tool_name() -> None:
    """_has_tool_use_block() returns the name of the first tool_use block."""
    from atelier.streaming import _has_tool_use_block

    raw = [
        {"type": "text", "text": "thinking"},
        {"type": "tool_use", "name": "my_tool", "id": "c1"},
    ]
    result = _has_tool_use_block(raw)
    assert result == "my_tool"


@pytest.mark.unit
def test_has_tool_use_block_no_block_returns_none() -> None:
    """_has_tool_use_block() returns None when no tool_use block is present."""
    from atelier.streaming import _has_tool_use_block

    raw = [{"type": "text", "text": "no tools here"}]
    assert _has_tool_use_block(raw) is None


@pytest.mark.unit
def test_has_tool_use_block_non_list_returns_none() -> None:
    """_has_tool_use_block() returns None for non-list input."""
    from atelier.streaming import _has_tool_use_block

    assert _has_tool_use_block("not a list") is None
    assert _has_tool_use_block(None) is None


@pytest.mark.unit
def test_has_tool_use_block_empty_name_returns_none() -> None:
    """_has_tool_use_block() returns None when name field is empty string."""
    from atelier.streaming import _has_tool_use_block

    raw = [{"type": "tool_use", "name": "", "id": "c1"}]
    assert _has_tool_use_block(raw) is None
