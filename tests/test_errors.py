"""Unit tests for atelier.errors — AgentExecutionError, ExhaustedRetriesError, ToolErrorGuard.

Tests validate:
- ToolErrorGuard._check_total_limit raises AgentExecutionError when total limit is reached
- ToolErrorGuard._check_consecutive_limit raises AgentExecutionError when consecutive limit is hit
- Both private methods do nothing when their respective limits are not exceeded
- record() orchestrates _check_total_limit then _check_consecutive_limit in that order
- record() resets consecutive tracking on success
- Overall external behaviour of ToolErrorGuard is unchanged
"""

from __future__ import annotations

import pytest

from atelier.errors import AgentExecutionError, ExhaustedRetriesError, ToolErrorGuard


# ---------------------------------------------------------------------------
# _check_total_limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_total_limit_raises_when_limit_reached() -> None:
    """_check_total_limit raises AgentExecutionError exactly when _total_errors >= _max_total."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=3)
    guard._total_errors = 3
    guard._total_calls = 3

    with pytest.raises(AgentExecutionError, match="total tool errors"):
        guard._check_total_limit("some_tool")


@pytest.mark.unit
def test_check_total_limit_raises_carries_counters() -> None:
    """_check_total_limit embeds tool_call_count and tool_error_count in the exception."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=2)
    guard._total_errors = 2
    guard._total_calls = 5

    with pytest.raises(AgentExecutionError) as exc_info:
        guard._check_total_limit("bad_tool")

    assert exc_info.value.tool_error_count == 2
    assert exc_info.value.tool_call_count == 5


@pytest.mark.unit
def test_check_total_limit_does_nothing_below_limit() -> None:
    """_check_total_limit is a no-op when _total_errors < _max_total."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=3)
    guard._total_errors = 2  # below limit

    # Should not raise
    guard._check_total_limit("some_tool")


@pytest.mark.unit
def test_check_total_limit_does_nothing_at_zero() -> None:
    """_check_total_limit is a no-op when no errors have been recorded."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=8)
    guard._check_total_limit("any_tool")  # must not raise


# ---------------------------------------------------------------------------
# _check_consecutive_limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_consecutive_limit_raises_when_limit_reached() -> None:
    """_check_consecutive_limit raises AgentExecutionError when _consecutive_count >= max."""
    guard = ToolErrorGuard(max_consecutive=3, max_total=10)
    guard._consecutive_name = "bad_tool"
    guard._consecutive_count = 3
    guard._total_calls = 3
    guard._total_errors = 3

    with pytest.raises(AgentExecutionError, match="bad_tool"):
        guard._check_consecutive_limit("bad_tool")


@pytest.mark.unit
def test_check_consecutive_limit_raises_carries_counters() -> None:
    """_check_consecutive_limit embeds counters in the raised exception."""
    guard = ToolErrorGuard(max_consecutive=2, max_total=10)
    guard._consecutive_name = "tool_x"
    guard._consecutive_count = 2
    guard._total_calls = 4
    guard._total_errors = 2

    with pytest.raises(AgentExecutionError) as exc_info:
        guard._check_consecutive_limit("tool_x")

    assert exc_info.value.tool_call_count == 4
    assert exc_info.value.tool_error_count == 2


@pytest.mark.unit
def test_check_consecutive_limit_does_nothing_below_limit() -> None:
    """_check_consecutive_limit is a no-op when the post-increment count is still below the limit.

    The method increments the counter before checking, so we set the counter
    to one less than the threshold *minus one* (i.e. count will go to limit-1 after increment).
    """
    guard = ToolErrorGuard(max_consecutive=5, max_total=10)
    guard._consecutive_name = "tool"
    guard._consecutive_count = 3  # after increment → 4, still below limit of 5

    guard._check_consecutive_limit("tool")  # must not raise


@pytest.mark.unit
def test_check_consecutive_limit_ignores_unnamed_tool() -> None:
    """_check_consecutive_limit skips the check entirely for '?' (unnamed tool)."""
    guard = ToolErrorGuard(max_consecutive=1, max_total=10)
    guard._consecutive_name = "?"
    guard._consecutive_count = 100  # way over limit — must be ignored for '?'

    guard._check_consecutive_limit("?")  # must not raise


@pytest.mark.unit
def test_check_consecutive_limit_ignores_different_tool_name() -> None:
    """_check_consecutive_limit does not raise when tool_name differs from _consecutive_name."""
    guard = ToolErrorGuard(max_consecutive=2, max_total=10)
    guard._consecutive_name = "other_tool"
    guard._consecutive_count = 100  # irrelevant

    guard._check_consecutive_limit("different_tool")  # must not raise


# ---------------------------------------------------------------------------
# record() — orchestration behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_record_success_resets_consecutive_tracking() -> None:
    """record() with is_error=False must reset consecutive name and count."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=10)
    guard._consecutive_name = "some_tool"
    guard._consecutive_count = 3

    guard.record("some_tool", is_error=False)

    assert guard._consecutive_name == ""
    assert guard._consecutive_count == 0
    assert guard.total_calls == 1
    assert guard.total_errors == 0


@pytest.mark.unit
def test_record_error_increments_total_errors() -> None:
    """record() with is_error=True increments _total_errors."""
    guard = ToolErrorGuard(max_consecutive=5, max_total=10)
    guard.record("tool", is_error=True)
    assert guard.total_errors == 1


@pytest.mark.unit
def test_record_raises_on_total_limit() -> None:
    """record() raises AgentExecutionError when total error limit is hit."""
    guard = ToolErrorGuard(max_consecutive=10, max_total=3)
    # Record 2 errors from different tools (no consecutive trigger)
    guard.record("tool_a", is_error=True)
    guard.record("tool_b", is_error=True)
    # Third error hits the total limit
    with pytest.raises(AgentExecutionError, match="total tool errors"):
        guard.record("tool_c", is_error=True)


@pytest.mark.unit
def test_record_raises_on_consecutive_limit() -> None:
    """record() raises AgentExecutionError when consecutive error limit is hit for one tool."""
    guard = ToolErrorGuard(max_consecutive=3, max_total=100)
    guard.record("bad_tool", is_error=True)
    guard.record("bad_tool", is_error=True)
    with pytest.raises(AgentExecutionError, match="bad_tool"):
        guard.record("bad_tool", is_error=True)


@pytest.mark.unit
def test_record_total_checked_before_consecutive() -> None:
    """_check_total_limit fires before _check_consecutive_limit when both limits are hit.

    When total >= max_total AND consecutive >= max_consecutive simultaneously,
    the total-limit error message should be what's raised (total checked first).
    """
    guard = ToolErrorGuard(max_consecutive=2, max_total=2)
    # After 1 error with the same tool: total=1, consecutive=1 — no raise yet
    guard.record("tool", is_error=True)
    # 2nd error: total becomes 2 (= max_total) and consecutive becomes 2 (= max_consecutive).
    # Total check fires first → message must mention "total tool errors".
    with pytest.raises(AgentExecutionError, match="total tool errors"):
        guard.record("tool", is_error=True)


@pytest.mark.unit
def test_record_consecutive_resets_on_different_tool() -> None:
    """Errors from different tools do not accumulate in the consecutive counter."""
    guard = ToolErrorGuard(max_consecutive=3, max_total=100)
    guard.record("tool_a", is_error=True)
    guard.record("tool_b", is_error=True)  # different tool — resets consecutive
    guard.record("tool_a", is_error=True)  # back to tool_a — count is 1, not raise
    # No exception should have been raised


@pytest.mark.unit
def test_record_unnamed_tool_skips_consecutive_check() -> None:
    """record() with tool_name='?' never triggers the consecutive error check."""
    guard = ToolErrorGuard(max_consecutive=1, max_total=100)
    # Even 10 errors for '?' — consecutive check must be skipped
    for _ in range(10):
        guard.record("?", is_error=True)
    assert guard.total_errors == 10


# ---------------------------------------------------------------------------
# ExhaustedRetriesError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exhausted_retries_error_is_subclass_of_agent_execution_error() -> None:
    """ExhaustedRetriesError must be a subclass of AgentExecutionError."""
    assert issubclass(ExhaustedRetriesError, AgentExecutionError)


@pytest.mark.unit
def test_exhausted_retries_error_is_catchable_as_agent_execution_error() -> None:
    """Raising ExhaustedRetriesError must be catchable via AgentExecutionError."""
    with pytest.raises(AgentExecutionError):
        raise ExhaustedRetriesError("all retries exhausted")
