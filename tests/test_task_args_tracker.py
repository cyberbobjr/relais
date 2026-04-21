"""Unit tests for TaskArgsTracker in atelier.streaming — TDD RED first.

Tests validate:
- TaskArgsTracker is importable from atelier.streaming
- Initial state is empty/reset
- reset() clears the buffer and resets name_logged flag
- accumulate() appends args fragments to the buffer
- try_parse_name() returns None when buffer has no valid JSON
- try_parse_name() returns None when JSON has no name/subagent_type
- try_parse_name() returns name from 'name' field
- try_parse_name() falls back to 'subagent_type' when 'name' is empty
- try_parse_name() returns None on partial (invalid) JSON
- register_ns() maps a namespace ID to a resolved name
- get_name_for_ns() returns the registered name or falls back to ns_id
- get_name_for_ns() returns ns_id when no name is registered
- has_ns() returns True/False correctly
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_args_tracker_importable() -> None:
    """TaskArgsTracker must be importable from atelier.streaming."""
    from atelier.streaming import TaskArgsTracker  # noqa: F401


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_args_tracker_initial_state() -> None:
    """Fresh TaskArgsTracker must have empty buffer, name_logged=False, empty ns_to_name."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    assert tracker.buf == ""
    assert tracker.name_logged is False
    assert tracker.ns_to_name == {}


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_args_tracker_reset_clears_state() -> None:
    """reset() must clear buf and set name_logged=False."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = '{"name": "my-agent"}'
    tracker.name_logged = True
    tracker.reset()
    assert tracker.buf == ""
    assert tracker.name_logged is False


@pytest.mark.unit
def test_task_args_tracker_reset_does_not_clear_ns_to_name() -> None:
    """reset() must NOT clear the ns_to_name mapping (it persists across calls)."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.ns_to_name["ns1"] = "agent-one"
    tracker.reset()
    assert tracker.ns_to_name == {"ns1": "agent-one"}


# ---------------------------------------------------------------------------
# accumulate()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_args_tracker_accumulate_appends() -> None:
    """accumulate() must append fragments in order."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.accumulate('{"name":')
    tracker.accumulate(' "my-agent"}')
    assert tracker.buf == '{"name": "my-agent"}'


# ---------------------------------------------------------------------------
# try_parse_name()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_try_parse_name_returns_none_when_buffer_empty() -> None:
    """Returns None when buffer is empty."""
    from atelier.streaming import TaskArgsTracker

    assert TaskArgsTracker().try_parse_name() is None


@pytest.mark.unit
def test_try_parse_name_returns_none_on_partial_json() -> None:
    """Returns None when buffer contains partial (invalid) JSON."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = '{"name": "my-age'
    assert tracker.try_parse_name() is None


@pytest.mark.unit
def test_try_parse_name_returns_none_when_no_name_fields() -> None:
    """Returns None when JSON has no 'name' or 'subagent_type' keys."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = json.dumps({"prompt": "do something"})
    assert tracker.try_parse_name() is None


@pytest.mark.unit
def test_try_parse_name_returns_name_field() -> None:
    """Returns value of 'name' field when present."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = json.dumps({"name": "my-agent", "prompt": "hello"})
    assert tracker.try_parse_name() == "my-agent"


@pytest.mark.unit
def test_try_parse_name_falls_back_to_subagent_type() -> None:
    """When 'name' is empty, falls back to 'subagent_type'."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = json.dumps({"name": "", "subagent_type": "specialist"})
    assert tracker.try_parse_name() == "specialist"


@pytest.mark.unit
def test_try_parse_name_returns_none_when_both_empty() -> None:
    """Returns None when both 'name' and 'subagent_type' are empty strings."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.buf = json.dumps({"name": "", "subagent_type": ""})
    assert tracker.try_parse_name() is None


# ---------------------------------------------------------------------------
# register_ns() / get_name_for_ns() / has_ns()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_ns_and_get_name() -> None:
    """register_ns() stores the name; get_name_for_ns() retrieves it."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.register_ns("ns-abc", "my-agent")
    assert tracker.get_name_for_ns("ns-abc") == "my-agent"


@pytest.mark.unit
def test_get_name_for_ns_fallback_to_ns_id() -> None:
    """get_name_for_ns() returns ns_id when no mapping exists."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    assert tracker.get_name_for_ns("unknown-ns") == "unknown-ns"


@pytest.mark.unit
def test_has_ns_returns_false_initially() -> None:
    """has_ns() returns False for an unknown namespace."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    assert tracker.has_ns("ns-xyz") is False


@pytest.mark.unit
def test_has_ns_returns_true_after_register() -> None:
    """has_ns() returns True after register_ns() is called."""
    from atelier.streaming import TaskArgsTracker

    tracker = TaskArgsTracker()
    tracker.register_ns("ns-xyz", "some-agent")
    assert tracker.has_ns("ns-xyz") is True
