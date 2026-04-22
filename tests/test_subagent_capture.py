"""Unit tests for atelier.subagent_capture — SubagentMessageCapture callback handler.

Tests validate (TDD RED phase):
1. LLM calls with no/empty langgraph_namespace are ignored (root agent)
2. Messages are captured for a non-root subagent namespace
3. Tool errors are counted when ToolMessage.status == "error"
4. Tool errors are counted when output is an error string
5. Successful tool calls increment count but not error count
6. get_subagent_data() returns a SubagentMetrics NamedTuple with named attributes
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult


def _run_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.mark.unit
def test_root_namespace_ignored() -> None:
    """LLM call with empty langgraph_namespace creates no captured data."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="hello")]],
        run_id=rid,
        metadata={"langgraph_namespace": []},
    )
    messages, tool_count, tool_errors = capture.get_subagent_data("some-ns")
    assert messages == []
    assert tool_count == 0
    assert tool_errors == 0


@pytest.mark.unit
def test_root_namespace_none_metadata_ignored() -> None:
    """LLM call with no metadata at all creates no captured data."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="hello")]],
        run_id=rid,
        metadata=None,
    )
    messages, tool_count, tool_errors = capture.get_subagent_data("some-ns")
    assert messages == []
    assert tool_count == 0
    assert tool_errors == 0


@pytest.mark.unit
def test_messages_captured_for_subagent_ns() -> None:
    """on_chat_model_start + on_llm_end capture input messages and AI response."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    rid = _run_id()
    input_messages = [HumanMessage(content="do something")]
    capture.on_chat_model_start(
        serialized={},
        messages=[input_messages],
        run_id=rid,
        metadata={"langgraph_namespace": ["ns1"]},
    )
    ai_msg = AIMessage(content="done")
    gen = ChatGeneration(message=ai_msg)
    result = LLMResult(generations=[[gen]])
    capture.on_llm_end(result, run_id=rid)

    messages, tool_count, tool_errors = capture.get_subagent_data("ns1")
    assert len(messages) == 2
    assert messages[0].content == "do something"
    assert messages[1].content == "done"
    assert tool_count == 0
    assert tool_errors == 0


@pytest.mark.unit
def test_tool_error_on_tool_message_status() -> None:
    """on_tool_end with ToolMessage(status='error') increments error counter."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    model_rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="hi")]],
        run_id=model_rid,
        metadata={"langgraph_namespace": ["ns2"]},
    )
    tool_rid = _run_id()
    capture.on_tool_start(
        serialized={},
        input_str="my_tool input",
        run_id=tool_rid,
        parent_run_id=model_rid,
        metadata={"langgraph_namespace": ["ns2"]},
    )
    error_msg = ToolMessage(content="Something broke", tool_call_id="tc1", status="error")
    capture.on_tool_end(error_msg, run_id=tool_rid)

    _, tool_count, tool_errors = capture.get_subagent_data("ns2")
    assert tool_count == 1
    assert tool_errors == 1


@pytest.mark.unit
def test_tool_error_on_error_string() -> None:
    """on_tool_end with 'Error: ...' string output increments error counter."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    model_rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="hi")]],
        run_id=model_rid,
        metadata={"langgraph_namespace": ["ns3"]},
    )
    tool_rid = _run_id()
    capture.on_tool_start(
        serialized={},
        input_str="cmd",
        run_id=tool_rid,
        parent_run_id=model_rid,
        metadata={"langgraph_namespace": ["ns3"]},
    )
    capture.on_tool_end("Error: command not found", run_id=tool_rid)

    _, tool_count, tool_errors = capture.get_subagent_data("ns3")
    assert tool_count == 1
    assert tool_errors == 1


@pytest.mark.unit
def test_no_error_on_successful_tool() -> None:
    """Successful tool output increments count but not error count."""
    from atelier.subagent_capture import SubagentMessageCapture

    capture = SubagentMessageCapture()
    model_rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="hi")]],
        run_id=model_rid,
        metadata={"langgraph_namespace": ["ns4"]},
    )
    tool_rid = _run_id()
    capture.on_tool_start(
        serialized={},
        input_str="cmd",
        run_id=tool_rid,
        parent_run_id=model_rid,
        metadata={"langgraph_namespace": ["ns4"]},
    )
    capture.on_tool_end("All done successfully.", run_id=tool_rid)

    _, tool_count, tool_errors = capture.get_subagent_data("ns4")
    assert tool_count == 1
    assert tool_errors == 0


@pytest.mark.unit
def test_get_subagent_data_returns_named_tuple() -> None:
    """get_subagent_data() returns a SubagentMetrics NamedTuple with named attributes."""
    from atelier.subagent_capture import SubagentMessageCapture, SubagentMetrics
    from langchain_core.outputs import ChatGeneration, LLMResult

    capture = SubagentMessageCapture()
    model_rid = _run_id()
    capture.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="ping")]],
        run_id=model_rid,
        metadata={"langgraph_namespace": ["ns5"]},
    )
    ai_msg = AIMessage(content="pong")
    gen = ChatGeneration(message=ai_msg)
    capture.on_llm_end(LLMResult(generations=[[gen]]), run_id=model_rid)

    result = capture.get_subagent_data("ns5")

    assert isinstance(result, SubagentMetrics)
    assert len(result.messages) == 2
    assert result.tool_calls == 0
    assert result.tool_errors == 0
    # Also verify positional unpack still works (NamedTuple is a tuple subclass)
    messages, tool_calls, tool_errors = result
    assert messages == result.messages
    assert tool_calls == 0
    assert tool_errors == 0
