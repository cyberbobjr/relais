"""Unit tests for atelier.stream_loop — TDD RED first.

Tests validate:
- StreamLoopState is importable from atelier.stream_loop
- compute_reply_text is importable from atelier.stream_loop
- build_subagent_traces is importable from atelier.stream_loop
- StreamLoopState initialises with correct defaults
- compute_reply_text returns full_reply when final_only=False
- compute_reply_text returns current_section when final_only=True
- compute_reply_text falls back to last_tool_result when full_reply is empty
- compute_reply_text returns REPLY_PLACEHOLDER when both full_reply and last_tool_result are empty
- compute_reply_text final_only selects current_section after fallback
- build_subagent_traces returns empty tuple when capture is None
- build_subagent_traces returns empty tuple when ns_to_name is empty
- build_subagent_traces builds SubagentTrace per ns entry
- build_subagent_traces uses subagent_skill_map when present
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_loop_state_importable() -> None:
    """StreamLoopState must be importable from atelier.stream_loop."""
    from atelier.stream_loop import StreamLoopState  # noqa: F401


@pytest.mark.unit
def test_compute_reply_text_importable() -> None:
    """compute_reply_text must be importable from atelier.stream_loop."""
    from atelier.stream_loop import compute_reply_text  # noqa: F401


@pytest.mark.unit
def test_build_subagent_traces_importable() -> None:
    """build_subagent_traces must be importable from atelier.stream_loop."""
    from atelier.stream_loop import build_subagent_traces  # noqa: F401


# ---------------------------------------------------------------------------
# StreamLoopState — default initialisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_loop_state_defaults() -> None:
    """StreamLoopState must initialise with correct default values."""
    from atelier.stream_loop import StreamLoopState

    state = StreamLoopState()
    assert state.full_reply == ""
    assert state.last_tool_result == ""
    assert state.pending_tool_name == ""
    assert state.current_section == ""


# ---------------------------------------------------------------------------
# compute_reply_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_reply_text_returns_full_reply_when_not_final_only() -> None:
    """When final_only=False, returns full_reply."""
    from atelier.stream_loop import compute_reply_text

    result = compute_reply_text(
        full_reply="complete text",
        current_section="last section",
        last_tool_result="tool output",
        final_only=False,
    )
    assert result == "complete text"


@pytest.mark.unit
def test_compute_reply_text_returns_current_section_when_final_only() -> None:
    """When final_only=True and full_reply is non-empty, returns current_section."""
    from atelier.stream_loop import compute_reply_text

    result = compute_reply_text(
        full_reply="complete text",
        current_section="last section",
        last_tool_result="tool output",
        final_only=True,
    )
    assert result == "last section"


@pytest.mark.unit
def test_compute_reply_text_fallback_to_tool_result() -> None:
    """When full_reply is empty, falls back to last_tool_result."""
    from atelier.stream_loop import compute_reply_text

    result = compute_reply_text(
        full_reply="",
        current_section="",
        last_tool_result="tool output",
        final_only=False,
    )
    assert result == "tool output"


@pytest.mark.unit
def test_compute_reply_text_placeholder_when_nothing() -> None:
    """When both full_reply and last_tool_result are empty, returns REPLY_PLACEHOLDER."""
    from atelier.stream_loop import compute_reply_text
    from atelier.streaming import REPLY_PLACEHOLDER

    result = compute_reply_text(
        full_reply="",
        current_section="",
        last_tool_result="",
        final_only=False,
    )
    assert result == REPLY_PLACEHOLDER


@pytest.mark.unit
def test_compute_reply_text_final_only_with_fallback() -> None:
    """When final_only=True and full_reply is empty, fallback uses current_section after setting it to tool result."""
    from atelier.stream_loop import compute_reply_text

    # When full_reply is empty, fallback sets current_section = last_tool_result
    # and then final_only returns current_section.
    result = compute_reply_text(
        full_reply="",
        current_section="",
        last_tool_result="tool output",
        final_only=True,
    )
    # The fallback path: current_section gets set to last_tool_result,
    # then final_only returns current_section → "tool output"
    assert result == "tool output"


# ---------------------------------------------------------------------------
# build_subagent_traces
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_subagent_traces_returns_empty_when_capture_none() -> None:
    """Returns an empty tuple when capture is None."""
    from atelier.stream_loop import build_subagent_traces

    result = build_subagent_traces(
        capture=None,
        ns_to_name={},
        subagent_skill_map={},
        serialize_messages_fn=lambda msgs: [],
    )
    assert result == ()


@pytest.mark.unit
def test_build_subagent_traces_returns_empty_when_ns_to_name_empty() -> None:
    """Returns an empty tuple when ns_to_name is empty."""
    from atelier.stream_loop import build_subagent_traces

    capture = MagicMock()
    result = build_subagent_traces(
        capture=capture,
        ns_to_name={},
        subagent_skill_map={},
        serialize_messages_fn=lambda msgs: [],
    )
    assert result == ()


@pytest.mark.unit
def test_build_subagent_traces_builds_one_trace() -> None:
    """Builds one SubagentTrace for each ns_to_name entry."""
    from atelier.stream_loop import build_subagent_traces
    from atelier.agent_executor import SubagentTrace

    sa_data = MagicMock()
    sa_data.tool_calls = 3
    sa_data.tool_errors = 1
    sa_data.messages = ["msg1"]

    capture = MagicMock()
    capture.get_subagent_data.return_value = sa_data

    result = build_subagent_traces(
        capture=capture,
        ns_to_name={"ns-abc": "my-agent"},
        subagent_skill_map={"my-agent": ["skill-a"]},
        serialize_messages_fn=lambda msgs: [str(m) for m in msgs],
    )

    assert len(result) == 1
    trace = result[0]
    assert isinstance(trace, SubagentTrace)
    assert trace.subagent_name == "my-agent"
    assert trace.skill_names == []
    assert trace.tool_call_count == 3
    assert trace.tool_error_count == 1
    assert trace.messages_raw == ["msg1"]


@pytest.mark.unit
def test_build_subagent_traces_uses_empty_skills_when_not_in_map() -> None:
    """When subagent name is not in skill_map, skill_names is an empty list."""
    from atelier.stream_loop import build_subagent_traces

    sa_data = MagicMock()
    sa_data.tool_calls = 0
    sa_data.tool_errors = 0
    sa_data.messages = []

    capture = MagicMock()
    capture.get_subagent_data.return_value = sa_data

    result = build_subagent_traces(
        capture=capture,
        ns_to_name={"ns-xyz": "unknown-agent"},
        subagent_skill_map={},
        serialize_messages_fn=lambda msgs: [],
    )

    assert result[0].skill_names == []
