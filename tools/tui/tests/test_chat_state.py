"""Tests for ChatMessage and ChatState — TDD RED phase (Cycle 1).

Covers: creation, mutation helpers, listener notification, edge cases.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# ChatMessage tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chat_message_creation_user() -> None:
    """ChatMessage with role='user' stores content correctly."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


@pytest.mark.unit
def test_chat_message_creation_assistant() -> None:
    """ChatMessage with role='assistant' stores content correctly."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="assistant", content="world")
    assert msg.role == "assistant"
    assert msg.content == "world"


@pytest.mark.unit
def test_chat_message_default_content_empty() -> None:
    """ChatMessage content defaults to empty string."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="assistant")
    assert msg.content == ""


@pytest.mark.unit
def test_chat_message_append_token() -> None:
    """append_token adds text to content in place."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="assistant", content="Hello")
    msg.append_token(" world")
    assert msg.content == "Hello world"


@pytest.mark.unit
def test_chat_message_set_content() -> None:
    """set_content replaces the content entirely."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="assistant", content="old")
    msg.set_content("new content")
    assert msg.content == "new content"


@pytest.mark.unit
def test_chat_message_append_empty_token() -> None:
    """append_token('') leaves content unchanged."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="assistant", content="abc")
    msg.append_token("")
    assert msg.content == "abc"


@pytest.mark.unit
def test_chat_message_role_immutable_via_equality() -> None:
    """Two ChatMessages with same role/content are equal (dataclass equality)."""
    from relais_tui.chat_state import ChatMessage

    a = ChatMessage(role="user", content="hi")
    b = ChatMessage(role="user", content="hi")
    assert a == b


@pytest.mark.unit
def test_chat_message_unicode_content() -> None:
    """ChatMessage stores unicode and emoji content correctly."""
    from relais_tui.chat_state import ChatMessage

    msg = ChatMessage(role="user", content="Bonjour \U0001f44b こんにちは")
    assert "Bonjour" in msg.content
    assert "\U0001f44b" in msg.content


# ---------------------------------------------------------------------------
# ChatState tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chat_state_initially_empty() -> None:
    """ChatState starts with an empty message list."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    assert state.messages == []


@pytest.mark.unit
def test_chat_state_add_message() -> None:
    """add_message appends a ChatMessage and returns it."""
    from relais_tui.chat_state import ChatMessage, ChatState

    state = ChatState()
    msg = state.add_message("user", "hello")
    assert isinstance(msg, ChatMessage)
    assert len(state.messages) == 1
    assert state.messages[0].role == "user"
    assert state.messages[0].content == "hello"


@pytest.mark.unit
def test_chat_state_add_multiple_messages() -> None:
    """add_message preserves insertion order."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    state.add_message("user", "first")
    state.add_message("assistant", "second")
    state.add_message("user", "third")

    assert len(state.messages) == 3
    assert state.messages[0].role == "user"
    assert state.messages[1].role == "assistant"
    assert state.messages[2].content == "third"


@pytest.mark.unit
def test_chat_state_clear() -> None:
    """clear() removes all messages."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    state.add_message("user", "msg1")
    state.add_message("assistant", "msg2")
    state.clear()
    assert state.messages == []


@pytest.mark.unit
def test_chat_state_last_message_returns_last() -> None:
    """last_message returns the most recently added ChatMessage."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    state.add_message("user", "a")
    last = state.add_message("assistant", "b")
    assert state.last_message() is last


@pytest.mark.unit
def test_chat_state_last_message_empty_returns_none() -> None:
    """last_message returns None on an empty state."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    assert state.last_message() is None


@pytest.mark.unit
def test_chat_state_listener_called_on_add() -> None:
    """Registered listeners are called when a message is added."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    listener = MagicMock()
    state.add_listener(listener)

    state.add_message("user", "ping")
    listener.assert_called_once()


@pytest.mark.unit
def test_chat_state_listener_called_on_update() -> None:
    """Registered listeners are called when a message is updated."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    listener = MagicMock()
    state.add_listener(listener)

    msg = state.add_message("assistant", "")
    listener.reset_mock()

    state.update_last_message("partial token")
    listener.assert_called_once()


@pytest.mark.unit
def test_chat_state_listener_called_on_clear() -> None:
    """Registered listeners are called when state is cleared."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    listener = MagicMock()
    state.add_listener(listener)
    state.add_message("user", "hi")
    listener.reset_mock()

    state.clear()
    listener.assert_called_once()


@pytest.mark.unit
def test_chat_state_multiple_listeners() -> None:
    """All registered listeners are called on state change."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    l1 = MagicMock()
    l2 = MagicMock()
    state.add_listener(l1)
    state.add_listener(l2)

    state.add_message("user", "hello")
    l1.assert_called_once()
    l2.assert_called_once()


@pytest.mark.unit
def test_chat_state_update_last_message_appends_token() -> None:
    """update_last_message appends text to the last assistant message."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    msg = state.add_message("assistant", "Hello")
    state.update_last_message(" world")
    assert msg.content == "Hello world"


@pytest.mark.unit
def test_chat_state_update_last_message_when_empty_is_noop() -> None:
    """update_last_message on empty state does nothing (no crash)."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    # Must not raise
    state.update_last_message("some token")


@pytest.mark.unit
def test_chat_state_set_last_message_content() -> None:
    """set_last_message_content replaces the content of the last message."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    msg = state.add_message("assistant", "old")
    state.set_last_message_content("completely new content")
    assert msg.content == "completely new content"


@pytest.mark.unit
def test_chat_state_messages_are_independent() -> None:
    """Messages in the list are distinct objects."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    m1 = state.add_message("user", "a")
    m2 = state.add_message("assistant", "b")
    assert m1 is not m2


@pytest.mark.unit
def test_chat_state_add_empty_content() -> None:
    """add_message with empty content is valid."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    msg = state.add_message("assistant", "")
    assert msg.content == ""
    assert len(state.messages) == 1


@pytest.mark.unit
def test_chat_state_large_number_of_messages() -> None:
    """ChatState handles 1000 messages without degradation."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    for i in range(1000):
        state.add_message("user" if i % 2 == 0 else "assistant", f"msg {i}")
    assert len(state.messages) == 1000
    assert state.last_message().content == "msg 999"


@pytest.mark.unit
def test_chat_state_clear_then_add() -> None:
    """After clear, add_message works normally."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    state.add_message("user", "before")
    state.clear()
    msg = state.add_message("user", "after")
    assert len(state.messages) == 1
    assert msg.content == "after"


@pytest.mark.unit
def test_chat_state_remove_listener() -> None:
    """remove_listener stops the callback from being called."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    listener = MagicMock()
    state.add_listener(listener)
    state.remove_listener(listener)

    state.add_message("user", "silent")
    listener.assert_not_called()


@pytest.mark.unit
def test_chat_state_remove_nonexistent_listener_is_noop() -> None:
    """remove_listener for an unregistered callable must not raise."""
    from relais_tui.chat_state import ChatState

    state = ChatState()
    listener = MagicMock()
    # Not added — should not raise
    state.remove_listener(listener)


@pytest.mark.unit
def test_chat_state_listener_exception_does_not_break_state() -> None:
    """A listener that raises must not prevent the state from updating."""
    from relais_tui.chat_state import ChatState

    state = ChatState()

    def bad_listener():
        raise RuntimeError("boom")

    state.add_listener(bad_listener)
    # Should not raise, even though listener raises
    state.add_message("user", "safe")
    assert len(state.messages) == 1
