"""Unit tests for atelier.diagnostic_trace — TDD RED first.

Tests validate:
- _DIAGNOSTIC_MAX_CHARS is importable and is an int
- format_diagnostic_trace is importable from atelier.diagnostic_trace
- _render_diagnostic_trace is importable from atelier.diagnostic_trace
- format_diagnostic_trace returns a DiagnosticTrace with correct fields
- format_diagnostic_trace with no tool errors sets last_tool/last_error to None
- format_diagnostic_trace counts messages_count correctly
- _render_diagnostic_trace produces a string prefixed with DIAGNOSTIC_MARKER
- _render_diagnostic_trace includes exception text
- _render_diagnostic_trace lists failing tools when present
- _render_diagnostic_trace truncates output to max_chars
- _render_diagnostic_trace handles empty tool_error_details
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_message(tool_name: str, content: str) -> dict:
    return {"type": "tool", "name": tool_name, "content": content, "status": "error"}


def _make_messages_with_errors(n: int = 2) -> list[dict]:
    return [_make_tool_message(f"tool_{i}", f"error msg {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diagnostic_trace_module_importable() -> None:
    """_DIAGNOSTIC_MAX_CHARS, format_diagnostic_trace, _render_diagnostic_trace must be importable."""
    from atelier.diagnostic_trace import (  # noqa: F401
        _DIAGNOSTIC_MAX_CHARS,
        _render_diagnostic_trace,
        format_diagnostic_trace,
    )


@pytest.mark.unit
def test_diagnostic_max_chars_is_int() -> None:
    """_DIAGNOSTIC_MAX_CHARS must be a positive integer."""
    from atelier.diagnostic_trace import _DIAGNOSTIC_MAX_CHARS

    assert isinstance(_DIAGNOSTIC_MAX_CHARS, int)
    assert _DIAGNOSTIC_MAX_CHARS > 0


# ---------------------------------------------------------------------------
# format_diagnostic_trace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_diagnostic_trace_returns_diagnostic_trace() -> None:
    """format_diagnostic_trace must return a DiagnosticTrace instance."""
    from atelier.diagnostic_trace import format_diagnostic_trace
    from atelier.errors import DiagnosticTrace

    result = format_diagnostic_trace("some error", [], tool_call_count=0, tool_error_count=0)
    assert isinstance(result, DiagnosticTrace)


@pytest.mark.unit
def test_format_diagnostic_trace_messages_count() -> None:
    """messages_count must equal the length of messages_raw."""
    from atelier.diagnostic_trace import format_diagnostic_trace

    messages = [{"type": "human", "content": "hi"}, {"type": "ai", "content": "hello"}]
    result = format_diagnostic_trace("err", messages)
    assert result.messages_count == 2


@pytest.mark.unit
def test_format_diagnostic_trace_no_tool_errors_gives_none() -> None:
    """With no tool errors in messages_raw, last_tool and last_error must be None."""
    from atelier.diagnostic_trace import format_diagnostic_trace

    result = format_diagnostic_trace("err", [])
    assert result.last_tool is None
    assert result.last_error is None


@pytest.mark.unit
def test_format_diagnostic_trace_tool_counts() -> None:
    """tool_count and tool_errors fields must match the passed counters."""
    from atelier.diagnostic_trace import format_diagnostic_trace

    result = format_diagnostic_trace("err", [], tool_call_count=5, tool_error_count=3)
    assert result.tool_count == 5
    assert result.tool_errors == 3


@pytest.mark.unit
def test_format_diagnostic_trace_empty_messages() -> None:
    """Empty messages_raw must produce messages_count=0 and empty tool_error_details."""
    from atelier.diagnostic_trace import format_diagnostic_trace

    result = format_diagnostic_trace("err", [])
    assert result.messages_count == 0
    assert result.tool_error_details == ()


# ---------------------------------------------------------------------------
# _render_diagnostic_trace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_diagnostic_trace_starts_with_marker() -> None:
    """Output must start with DIAGNOSTIC_MARKER."""
    from atelier.diagnostic_trace import _render_diagnostic_trace, format_diagnostic_trace
    from atelier.prompts import DIAGNOSTIC_MARKER

    trace = format_diagnostic_trace("some error", [])
    rendered = _render_diagnostic_trace(trace, "some error")
    assert rendered.startswith(DIAGNOSTIC_MARKER)


@pytest.mark.unit
def test_render_diagnostic_trace_includes_error() -> None:
    """Output must include the exception text."""
    from atelier.diagnostic_trace import _render_diagnostic_trace, format_diagnostic_trace

    trace = format_diagnostic_trace("critical failure", [])
    rendered = _render_diagnostic_trace(trace, "critical failure")
    assert "critical failure" in rendered


@pytest.mark.unit
def test_render_diagnostic_trace_includes_tool_counts() -> None:
    """Output must mention tool call and error counts."""
    from atelier.diagnostic_trace import _render_diagnostic_trace, format_diagnostic_trace

    trace = format_diagnostic_trace("err", [], tool_call_count=7, tool_error_count=2)
    rendered = _render_diagnostic_trace(trace, "err")
    assert "7" in rendered
    assert "2" in rendered


@pytest.mark.unit
def test_render_diagnostic_trace_truncates_to_max_chars() -> None:
    """Output must be truncated to max_chars."""
    from atelier.diagnostic_trace import _render_diagnostic_trace, format_diagnostic_trace

    trace = format_diagnostic_trace("x" * 5000, [])
    rendered = _render_diagnostic_trace(trace, "x" * 5000, max_chars=100)
    assert len(rendered) <= 100


@pytest.mark.unit
def test_render_diagnostic_trace_no_tool_errors_no_failing_tools_section() -> None:
    """When there are no tool errors, 'Failing tools:' must not appear."""
    from atelier.diagnostic_trace import _render_diagnostic_trace, format_diagnostic_trace

    trace = format_diagnostic_trace("err", [])
    rendered = _render_diagnostic_trace(trace, "err")
    assert "Failing tools:" not in rendered
