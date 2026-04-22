"""Unit tests for content normalisation symbols in atelier.streaming — TDD RED first.

Tests validate that after the Commit 5 extraction:
- REPLY_PLACEHOLDER is importable from atelier.streaming
- _EXECUTE_FAILURE_MARKER is importable from atelier.streaming
- _normalise_content is importable from atelier.streaming
- _normalise_content returns str input unchanged
- _normalise_content extracts text blocks from a list
- _normalise_content joins multiple text blocks
- _normalise_content skips non-text block types (thinking, tool_use, image_url)
- _normalise_content handles an empty list
- _normalise_content handles non-str, non-list input via str()
- _normalise_content handles list containing raw strings (non-dict items)
- REPLY_PLACEHOLDER is a non-empty string
- _EXECUTE_FAILURE_MARKER is a non-empty string
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reply_placeholder_importable_from_streaming() -> None:
    """REPLY_PLACEHOLDER must be importable from atelier.streaming."""
    from atelier.streaming import REPLY_PLACEHOLDER  # noqa: F401


@pytest.mark.unit
def test_execute_failure_marker_importable_from_streaming() -> None:
    """_EXECUTE_FAILURE_MARKER must be importable from atelier.streaming."""
    from atelier.streaming import _EXECUTE_FAILURE_MARKER  # noqa: F401


@pytest.mark.unit
def test_normalise_content_importable_from_streaming() -> None:
    """_normalise_content must be importable from atelier.streaming."""
    from atelier.streaming import _normalise_content  # noqa: F401


# ---------------------------------------------------------------------------
# Constant values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reply_placeholder_is_non_empty_string() -> None:
    """REPLY_PLACEHOLDER must be a non-empty string."""
    from atelier.streaming import REPLY_PLACEHOLDER

    assert isinstance(REPLY_PLACEHOLDER, str)
    assert len(REPLY_PLACEHOLDER) > 0


@pytest.mark.unit
def test_execute_failure_marker_is_non_empty_string() -> None:
    """_EXECUTE_FAILURE_MARKER must be a non-empty string."""
    from atelier.streaming import _EXECUTE_FAILURE_MARKER

    assert isinstance(_EXECUTE_FAILURE_MARKER, str)
    assert len(_EXECUTE_FAILURE_MARKER) > 0


# ---------------------------------------------------------------------------
# _normalise_content
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalise_content_str_passthrough() -> None:
    """Plain string input must be returned unchanged."""
    from atelier.streaming import _normalise_content

    assert _normalise_content("hello world") == "hello world"


@pytest.mark.unit
def test_normalise_content_empty_string() -> None:
    """Empty string must be returned as-is."""
    from atelier.streaming import _normalise_content

    assert _normalise_content("") == ""


@pytest.mark.unit
def test_normalise_content_list_single_text_block() -> None:
    """Single text block in a list must be extracted."""
    from atelier.streaming import _normalise_content

    result = _normalise_content([{"type": "text", "text": "hello"}])
    assert result == "hello"


@pytest.mark.unit
def test_normalise_content_list_multiple_text_blocks_joined() -> None:
    """Multiple text blocks must be joined (no separator)."""
    from atelier.streaming import _normalise_content

    result = _normalise_content([
        {"type": "text", "text": "foo"},
        {"type": "text", "text": "bar"},
    ])
    assert result == "foobar"


@pytest.mark.unit
def test_normalise_content_list_skips_thinking_blocks() -> None:
    """Thinking blocks must be skipped."""
    from atelier.streaming import _normalise_content

    result = _normalise_content([
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "answer"},
    ])
    assert result == "answer"


@pytest.mark.unit
def test_normalise_content_list_skips_tool_use_blocks() -> None:
    """Tool-use blocks must be skipped."""
    from atelier.streaming import _normalise_content

    result = _normalise_content([
        {"type": "tool_use", "name": "bash", "input": {}},
        {"type": "text", "text": "done"},
    ])
    assert result == "done"


@pytest.mark.unit
def test_normalise_content_empty_list_returns_empty_string() -> None:
    """Empty list must return an empty string."""
    from atelier.streaming import _normalise_content

    assert _normalise_content([]) == ""


@pytest.mark.unit
def test_normalise_content_list_with_raw_string_items() -> None:
    """List containing plain strings (not dicts) must include them."""
    from atelier.streaming import _normalise_content

    result = _normalise_content(["hello", " world"])
    assert result == "hello world"


@pytest.mark.unit
def test_normalise_content_non_str_non_list_uses_str() -> None:
    """Non-str, non-list input must be converted via str()."""
    from atelier.streaming import _normalise_content

    assert _normalise_content(42) == "42"
    assert _normalise_content(None) == "None"
