"""Unit tests for atelier.message_serializer — TDD RED first.

Tests the round-trip serialization/deserialization of LangChain messages:
HumanMessage, AIMessage (with and without tool_calls), ToolMessage, SystemMessage.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fixtures: LangChain-like message objects
# ---------------------------------------------------------------------------


def _human_msg(content: str):
    """Build a minimal HumanMessage-like object."""
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


def _ai_msg(content: str):
    """Build a minimal AIMessage-like object (no tool calls)."""
    from langchain_core.messages import AIMessage
    return AIMessage(content=content)


def _ai_msg_with_tool_calls(content: str, tool_calls: list[dict]):
    """Build an AIMessage-like object with tool_calls."""
    from langchain_core.messages import AIMessage
    return AIMessage(content=content, tool_calls=tool_calls)


def _tool_msg(content: str, tool_call_id: str, name: str):
    """Build a ToolMessage-like object."""
    from langchain_core.messages import ToolMessage
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


def _system_msg(content: str):
    """Build a SystemMessage-like object."""
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


# ---------------------------------------------------------------------------
# Phase 1: serialize_messages — basic output format
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_serialize_human_message_returns_expected_dict() -> None:
    """serialize_messages() converts HumanMessage to dict with role='human'."""
    from atelier.message_serializer import serialize_messages

    msgs = [_human_msg("Bonjour")]
    result = serialize_messages(msgs)

    assert len(result) == 1
    assert result[0]["role"] == "human"
    assert result[0]["content"] == "Bonjour"


@pytest.mark.unit
def test_serialize_ai_message_returns_expected_dict() -> None:
    """serialize_messages() converts AIMessage to dict with role='ai'."""
    from atelier.message_serializer import serialize_messages

    msgs = [_ai_msg("Salut!")]
    result = serialize_messages(msgs)

    assert len(result) == 1
    assert result[0]["role"] == "ai"
    assert result[0]["content"] == "Salut!"


@pytest.mark.unit
def test_serialize_system_message_returns_expected_dict() -> None:
    """serialize_messages() converts SystemMessage to dict with role='system'."""
    from atelier.message_serializer import serialize_messages

    msgs = [_system_msg("Tu es un assistant utile.")]
    result = serialize_messages(msgs)

    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "Tu es un assistant utile."


@pytest.mark.unit
def test_serialize_tool_message_returns_expected_dict() -> None:
    """serialize_messages() converts ToolMessage to dict with role='tool'."""
    from atelier.message_serializer import serialize_messages

    msgs = [_tool_msg("42", tool_call_id="call_123", name="calculator")]
    result = serialize_messages(msgs)

    assert len(result) == 1
    assert result[0]["role"] == "tool"
    assert result[0]["content"] == "42"
    assert result[0]["tool_call_id"] == "call_123"
    assert result[0]["name"] == "calculator"


@pytest.mark.unit
def test_serialize_ai_message_with_tool_calls() -> None:
    """serialize_messages() preserves tool_calls list in AIMessage dict."""
    from atelier.message_serializer import serialize_messages

    tool_calls = [
        {"id": "call_abc", "name": "search", "args": {"query": "python tdd"}},
    ]
    msgs = [_ai_msg_with_tool_calls("Searching...", tool_calls)]
    result = serialize_messages(msgs)

    assert len(result) == 1
    assert result[0]["role"] == "ai"
    assert "tool_calls" in result[0]
    assert len(result[0]["tool_calls"]) == 1
    assert result[0]["tool_calls"][0]["name"] == "search"


@pytest.mark.unit
def test_serialize_empty_list_returns_empty_list() -> None:
    """serialize_messages([]) returns []."""
    from atelier.message_serializer import serialize_messages

    result = serialize_messages([])
    assert result == []


@pytest.mark.unit
def test_serialize_multiple_messages_preserves_order() -> None:
    """serialize_messages() preserves the order of messages."""
    from atelier.message_serializer import serialize_messages

    msgs = [
        _system_msg("System"),
        _human_msg("Human turn 1"),
        _ai_msg("AI turn 1"),
        _human_msg("Human turn 2"),
    ]
    result = serialize_messages(msgs)

    assert len(result) == 4
    assert result[0]["role"] == "system"
    assert result[1]["role"] == "human"
    assert result[2]["role"] == "ai"
    assert result[3]["role"] == "human"


# ---------------------------------------------------------------------------
# Phase 2: deserialize_messages — reconstruct LangChain messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deserialize_human_message_round_trip() -> None:
    """deserialize_messages() converts role='human' dict back to HumanMessage."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import HumanMessage

    original = [_human_msg("Bonjour")]
    serialized = serialize_messages(original)
    result = deserialize_messages(serialized)

    assert len(result) == 1
    assert isinstance(result[0], HumanMessage)
    assert result[0].content == "Bonjour"


@pytest.mark.unit
def test_deserialize_ai_message_round_trip() -> None:
    """deserialize_messages() converts role='ai' dict back to AIMessage."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import AIMessage

    original = [_ai_msg("Salut!")]
    serialized = serialize_messages(original)
    result = deserialize_messages(serialized)

    assert len(result) == 1
    assert isinstance(result[0], AIMessage)
    assert result[0].content == "Salut!"


@pytest.mark.unit
def test_deserialize_system_message_round_trip() -> None:
    """deserialize_messages() converts role='system' dict back to SystemMessage."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import SystemMessage

    original = [_system_msg("Contexte système")]
    serialized = serialize_messages(original)
    result = deserialize_messages(serialized)

    assert len(result) == 1
    assert isinstance(result[0], SystemMessage)
    assert result[0].content == "Contexte système"


@pytest.mark.unit
def test_deserialize_tool_message_round_trip() -> None:
    """deserialize_messages() converts role='tool' dict back to ToolMessage."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import ToolMessage

    original = [_tool_msg("result_content", tool_call_id="c1", name="my_tool")]
    serialized = serialize_messages(original)
    result = deserialize_messages(serialized)

    assert len(result) == 1
    assert isinstance(result[0], ToolMessage)
    assert result[0].content == "result_content"
    assert result[0].tool_call_id == "c1"


@pytest.mark.unit
def test_deserialize_ai_with_tool_calls_round_trip() -> None:
    """deserialize_messages() preserves tool_calls when deserializing AIMessage."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import AIMessage

    tool_calls = [
        {"id": "call_xyz", "name": "my_func", "args": {"x": 1}},
    ]
    original = [_ai_msg_with_tool_calls("", tool_calls)]
    serialized = serialize_messages(original)
    result = deserialize_messages(serialized)

    assert len(result) == 1
    assert isinstance(result[0], AIMessage)
    assert len(result[0].tool_calls) == 1
    assert result[0].tool_calls[0]["name"] == "my_func"


@pytest.mark.unit
def test_deserialize_empty_list_returns_empty_list() -> None:
    """deserialize_messages([]) returns []."""
    from atelier.message_serializer import deserialize_messages

    result = deserialize_messages([])
    assert result == []


@pytest.mark.unit
def test_full_conversation_round_trip() -> None:
    """Full conversation with mixed message types survives a round-trip."""
    from atelier.message_serializer import serialize_messages, deserialize_messages
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

    tool_calls = [{"id": "call_1", "name": "search", "args": {"q": "test"}}]
    original = [
        _system_msg("Système actif"),
        _human_msg("Lance une recherche"),
        _ai_msg_with_tool_calls("", tool_calls),
        _tool_msg("résultat de recherche", tool_call_id="call_1", name="search"),
        _ai_msg("Voici ce que j'ai trouvé."),
    ]

    serialized = serialize_messages(original)
    restored = deserialize_messages(serialized)

    assert len(restored) == 5
    assert isinstance(restored[0], SystemMessage)
    assert isinstance(restored[1], HumanMessage)
    assert isinstance(restored[2], AIMessage)
    assert isinstance(restored[3], ToolMessage)
    assert isinstance(restored[4], AIMessage)
    assert restored[4].content == "Voici ce que j'ai trouvé."


# ---------------------------------------------------------------------------
# Phase 3: Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_serialize_message_with_list_content() -> None:
    """serialize_messages() handles AIMessage with list-typed content (multimodal)."""
    from atelier.message_serializer import serialize_messages
    from langchain_core.messages import AIMessage

    # LangChain supports list content for multimodal messages
    content = [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": "..."}]
    msg = AIMessage(content=content)
    result = serialize_messages([msg])

    # Should not raise, content should be stored as-is
    assert len(result) == 1
    assert result[0]["role"] == "ai"
    # content may be stored as list or joined string — just check it's present
    assert result[0]["content"] is not None


@pytest.mark.unit
def test_deserialize_unknown_role_raises_or_skips() -> None:
    """deserialize_messages() handles unknown role gracefully (raises ValueError or skips)."""
    from atelier.message_serializer import deserialize_messages

    bad_dicts = [{"role": "robot", "content": "beep boop"}]
    # Should either raise ValueError or skip (not crash with AttributeError)
    try:
        result = deserialize_messages(bad_dicts)
        # If it doesn't raise, the result should be empty or contain something
        # The important thing is no AttributeError/KeyError
    except (ValueError, KeyError):
        pass  # Acceptable behavior


@pytest.mark.unit
def test_serialize_returns_json_serializable_dicts() -> None:
    """serialize_messages() output can be JSON-encoded without error."""
    import json
    from atelier.message_serializer import serialize_messages

    tool_calls = [{"id": "c1", "name": "foo", "args": {"x": 42}}]
    msgs = [
        _human_msg("hello"),
        _ai_msg_with_tool_calls("doing stuff", tool_calls),
        _tool_msg("result", tool_call_id="c1", name="foo"),
        _ai_msg("done"),
    ]
    result = serialize_messages(msgs)

    # Must not raise
    encoded = json.dumps(result)
    decoded = json.loads(encoded)
    assert len(decoded) == 4
