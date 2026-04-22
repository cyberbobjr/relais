"""Unit tests for async handler helpers in atelier.stream_loop — TDD RED first.

Tests validate:
- handle_updates_chunk is importable from atelier.stream_loop
- handle_tool_call_chunks is importable from atelier.stream_loop
- handle_tool_result is importable from atelier.stream_loop
- handle_updates_chunk does nothing when ns is empty
- handle_updates_chunk logs subagent start when ns is non-empty and node_name is 'model'
- handle_updates_chunk calls progress_callback with 'subagent_start' when ns is non-empty
- handle_updates_chunk registers ns when tracker has parseable name
- handle_updates_chunk does not double-register an already known ns
- handle_tool_call_chunks returns same state when token has no tool_call_chunks
- handle_tool_call_chunks updates pending_tool_name from tool_call_chunk name
- handle_tool_call_chunks resets tracker when tool name is 'task'
- handle_tool_call_chunks calls progress_callback with 'tool_call' event
- handle_tool_call_chunks resets current_section when final_only is True
- handle_tool_call_chunks accumulates args for 'task' tool
- handle_tool_call_chunks detects tool_use block via content fallback
- handle_tool_result updates last_tool_result in state
- handle_tool_result calls progress_callback with 'tool_result' event
- handle_tool_result calls guard.record with is_logical_error=True on status='error'
- handle_tool_result calls guard.record with is_logical_error=False on status='success'
- handle_tool_result detects execute failure marker as logical error
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_updates_chunk_importable() -> None:
    """handle_updates_chunk must be importable from atelier.stream_loop."""
    from atelier.stream_loop import handle_updates_chunk  # noqa: F401


@pytest.mark.unit
def test_handle_tool_call_chunks_importable() -> None:
    """handle_tool_call_chunks must be importable from atelier.stream_loop."""
    from atelier.stream_loop import handle_tool_call_chunks  # noqa: F401


@pytest.mark.unit
def test_handle_tool_result_importable() -> None:
    """handle_tool_result must be importable from atelier.stream_loop."""
    from atelier.stream_loop import handle_tool_result  # noqa: F401


# ---------------------------------------------------------------------------
# handle_updates_chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_updates_chunk_no_op_when_ns_empty() -> None:
    """No callback is fired when ns is empty."""
    from atelier.stream_loop import handle_updates_chunk
    from atelier.streaming import TaskArgsTracker

    callback = AsyncMock()
    tracker = TaskArgsTracker()
    await handle_updates_chunk(
        ns=[],
        data={"model": {}},
        source="agent",
        tracker=tracker,
        progress_callback=callback,
    )
    callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_updates_chunk_fires_progress_when_ns_non_empty_and_model() -> None:
    """Fires progress_callback('subagent_start', ...) when ns is non-empty and node is 'model'."""
    from atelier.stream_loop import handle_updates_chunk
    from atelier.streaming import TaskArgsTracker

    callback = AsyncMock()
    tracker = TaskArgsTracker()
    await handle_updates_chunk(
        ns=["ns-abc"],
        data={"model": {}},
        source="subagent:ns-abc",
        tracker=tracker,
        progress_callback=callback,
    )
    callback.assert_called_once()
    event_name, _ = callback.call_args[0]
    assert event_name == "subagent_start"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_updates_chunk_registers_ns_from_tracker() -> None:
    """Registers the ns when tracker has a parseable subagent name."""
    from atelier.stream_loop import handle_updates_chunk
    from atelier.streaming import TaskArgsTracker
    import json

    tracker = TaskArgsTracker()
    tracker.accumulate(json.dumps({"name": "my-agent"}))

    await handle_updates_chunk(
        ns=["ns-xyz"],
        data={"model": {}},
        source="subagent:ns-xyz",
        tracker=tracker,
        progress_callback=None,
    )
    assert tracker.has_ns("ns-xyz")
    assert tracker.get_name_for_ns("ns-xyz") == "my-agent"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_updates_chunk_does_not_double_register() -> None:
    """Does not overwrite an already registered ns."""
    from atelier.stream_loop import handle_updates_chunk
    from atelier.streaming import TaskArgsTracker
    import json

    tracker = TaskArgsTracker()
    tracker.register_ns("ns-xyz", "first-agent")
    tracker.accumulate(json.dumps({"name": "second-agent"}))

    await handle_updates_chunk(
        ns=["ns-xyz"],
        data={"model": {}},
        source="subagent:ns-xyz",
        tracker=tracker,
        progress_callback=None,
    )
    # Should still be the first registration
    assert tracker.get_name_for_ns("ns-xyz") == "first-agent"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_updates_chunk_no_callback_when_progress_callback_none() -> None:
    """No exception raised when progress_callback is None."""
    from atelier.stream_loop import handle_updates_chunk
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    # Must not raise
    await handle_updates_chunk(
        ns=["ns-abc"],
        data={"model": {}},
        source="subagent:ns-abc",
        tracker=tracker,
        progress_callback=None,
    )


# ---------------------------------------------------------------------------
# handle_tool_call_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_returns_same_state_when_no_chunks() -> None:
    """Returns the same state when the token has no tool_call_chunks."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    token.tool_call_chunks = []
    token.content = "hello"

    state = StreamLoopState(full_reply="abc", pending_tool_name="prev")
    tracker = TaskArgsTracker()

    result = await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=None,
    )
    assert result.pending_tool_name == "prev"
    assert result.full_reply == "abc"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_updates_pending_tool_name() -> None:
    """Updates pending_tool_name from the tool_call_chunk name."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    token.tool_call_chunks = [{"name": "read_file", "args": ""}]
    token.content = []

    state = StreamLoopState()
    tracker = TaskArgsTracker()

    result = await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=None,
    )
    assert result.pending_tool_name == "read_file"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_resets_tracker_for_task_tool() -> None:
    """Calls tracker.reset() when the tool name is 'task'."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    token.tool_call_chunks = [{"name": "task", "args": ""}]
    token.content = []

    state = StreamLoopState()
    tracker = TaskArgsTracker()
    tracker.accumulate('{"name": "old-name"}')

    await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=None,
    )
    assert tracker.buf == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_fires_progress_callback() -> None:
    """Fires progress_callback('tool_call', tool_name) when a named chunk is found."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    callback = AsyncMock()
    token = MagicMock()
    token.tool_call_chunks = [{"name": "my_tool", "args": ""}]
    token.content = []

    state = StreamLoopState()
    tracker = TaskArgsTracker()

    await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=callback,
    )
    callback.assert_called_once_with("tool_call", "my_tool")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_resets_current_section_when_final_only() -> None:
    """Resets current_section to '' when final_only=True and a named chunk is found."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    token.tool_call_chunks = [{"name": "some_tool", "args": ""}]
    token.content = []

    state = StreamLoopState(current_section="previous section text")
    tracker = TaskArgsTracker()

    result = await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=True,
        progress_callback=None,
    )
    assert result.current_section == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_accumulates_args_for_task() -> None:
    """Accumulates args fragments in the tracker when tool is 'task'."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    # First chunk: sets name
    token.tool_call_chunks = [
        {"name": "task", "args": ""},
        {"name": "", "args": '{"name": "my'},
    ]
    token.content = []

    state = StreamLoopState()
    tracker = TaskArgsTracker()

    await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=None,
    )
    assert '{"name": "my' in tracker.buf


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_call_chunks_detects_tool_use_via_content_fallback() -> None:
    """Detects tool name from content tool_use block when tool_call_chunks is empty."""
    from atelier.stream_loop import handle_tool_call_chunks, StreamLoopState
    from atelier.streaming import TaskArgsTracker

    token = MagicMock()
    token.tool_call_chunks = []
    token.content = [{"type": "tool_use", "name": "fallback_tool"}]

    state = StreamLoopState()
    tracker = TaskArgsTracker()

    result = await handle_tool_call_chunks(
        token=token,
        source="agent",
        state=state,
        tracker=tracker,
        final_only=False,
        progress_callback=None,
    )
    assert result.pending_tool_name == "fallback_tool"


# ---------------------------------------------------------------------------
# handle_tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_result_updates_last_tool_result() -> None:
    """Updates last_tool_result in the returned state."""
    from atelier.stream_loop import handle_tool_result, StreamLoopState

    token = MagicMock()
    token.name = "read_file"
    token.content = "file contents here"
    token.status = "success"

    guard = MagicMock()
    state = StreamLoopState()

    result = await handle_tool_result(
        token=token,
        source="agent",
        state=state,
        guard=guard,
        progress_callback=None,
    )
    assert result.last_tool_result == "file contents here"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_result_fires_progress_callback() -> None:
    """Fires progress_callback('tool_result', ...) with tool name and content preview."""
    from atelier.stream_loop import handle_tool_result, StreamLoopState

    callback = AsyncMock()
    token = MagicMock()
    token.name = "my_tool"
    token.content = "output data"
    token.status = "success"

    guard = MagicMock()
    state = StreamLoopState()

    await handle_tool_result(
        token=token,
        source="agent",
        state=state,
        guard=guard,
        progress_callback=callback,
    )
    callback.assert_called_once()
    event_name, detail = callback.call_args[0]
    assert event_name == "tool_result"
    assert "my_tool" in detail


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_result_records_logical_error_on_status_error() -> None:
    """Calls guard.record with is_logical_error=True when token.status is 'error'."""
    from atelier.stream_loop import handle_tool_result, StreamLoopState

    token = MagicMock()
    token.name = "bad_tool"
    token.content = "something went wrong"
    token.status = "error"

    guard = MagicMock()
    state = StreamLoopState()

    await handle_tool_result(
        token=token,
        source="agent",
        state=state,
        guard=guard,
        progress_callback=None,
    )
    guard.record.assert_called_once_with("bad_tool", True)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_result_no_logical_error_on_success() -> None:
    """Calls guard.record with is_logical_error=False when status is 'success'."""
    from atelier.stream_loop import handle_tool_result, StreamLoopState

    token = MagicMock()
    token.name = "ok_tool"
    token.content = "all good"
    token.status = "success"

    guard = MagicMock()
    state = StreamLoopState()

    await handle_tool_result(
        token=token,
        source="agent",
        state=state,
        guard=guard,
        progress_callback=None,
    )
    guard.record.assert_called_once_with("ok_tool", False)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_tool_result_detects_execute_failure_marker() -> None:
    """Detects execute failure via _EXECUTE_FAILURE_MARKER even when status is 'success'."""
    from atelier.stream_loop import handle_tool_result, StreamLoopState
    from atelier.streaming import _EXECUTE_FAILURE_MARKER

    token = MagicMock()
    token.name = "execute"
    token.content = f"{_EXECUTE_FAILURE_MARKER} 1]"
    token.status = "success"

    guard = MagicMock()
    state = StreamLoopState()

    await handle_tool_result(
        token=token,
        source="agent",
        state=state,
        guard=guard,
        progress_callback=None,
    )
    guard.record.assert_called_once_with("execute", True)
