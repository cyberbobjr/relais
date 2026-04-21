"""ChatState — in-memory conversation state for the windowed TUI.

Holds an ordered list of ChatMessage objects and notifies registered
listeners whenever the state changes.  No I/O, no rendering — purely
a data model consumed by RelaisApp.

Usage::

    state = ChatState()
    state.add_listener(lambda: app.invalidate())

    user_msg = state.add_message("user", "hello")
    asst_msg = state.add_message("assistant", "")
    state.update_last_message(" token by token…")
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ChatMessage
# ---------------------------------------------------------------------------


@dataclass
class ChatMessage:
    """A single turn in the conversation.

    Unlike most dataclasses in this project, ChatMessage is intentionally
    *mutable* because streaming updates append tokens to ``content`` in
    place — avoiding object churn on every token.

    Args:
        role: Either ``"user"`` or ``"assistant"``.
        content: The accumulated text content.
    """

    role: str
    content: str = ""

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def append_token(self, token: str) -> None:
        """Append *token* to content in place.

        Args:
            token: The new text fragment to add.
        """
        self.content += token

    def set_content(self, text: str) -> None:
        """Replace content with *text*.

        Args:
            text: The complete replacement content.
        """
        self.content = text


# ---------------------------------------------------------------------------
# ChatState
# ---------------------------------------------------------------------------


class ChatState:
    """Ordered list of chat messages with change-notification support.

    Listeners are zero-argument callables.  If a listener raises an
    exception it is logged and swallowed so that one bad listener cannot
    break state mutations.

    Usage::

        state = ChatState()
        state.add_listener(lambda: app.invalidate())
        msg = state.add_message("user", "ping")
    """

    def __init__(self) -> None:
        self._messages: list[ChatMessage] = []
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Read-only access
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[ChatMessage]:
        """Return a view of the current message list.

        Returns:
            The internal list (not a copy — do not mutate directly).
        """
        return self._messages

    def last_message(self) -> ChatMessage | None:
        """Return the last message, or None if the list is empty.

        Returns:
            The most recently added ChatMessage, or None.
        """
        return self._messages[-1] if self._messages else None

    # ------------------------------------------------------------------
    # State mutations
    # ------------------------------------------------------------------

    def add_message(self, role: str, content: str = "") -> ChatMessage:
        """Append a new ChatMessage and notify listeners.

        Args:
            role: Message role — ``"user"`` or ``"assistant"``.
            content: Initial content (default empty string).

        Returns:
            The newly created ChatMessage.
        """
        msg = ChatMessage(role=role, content=content)
        self._messages.append(msg)
        self._notify()
        return msg

    def update_last_message(self, token: str) -> None:
        """Append *token* to the last message's content and notify listeners.

        If the message list is empty this is a no-op (no error raised).

        Args:
            token: Text fragment to append.
        """
        if not self._messages:
            return
        self._messages[-1].append_token(token)
        self._notify()

    def set_last_message_content(self, text: str) -> None:
        """Replace the last message's content with *text* and notify listeners.

        If the message list is empty this is a no-op.

        Args:
            text: Complete replacement content for the last message.
        """
        if not self._messages:
            return
        self._messages[-1].set_content(text)
        self._notify()

    def clear(self) -> None:
        """Remove all messages and notify listeners."""
        self._messages = []
        self._notify()

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Register *listener* to be called on every state change.

        Args:
            listener: Zero-argument callable invoked after each mutation.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        """Unregister *listener*. No-op if it was never registered.

        Args:
            listener: The callable to remove.
        """
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _notify(self) -> None:
        """Call all registered listeners, swallowing any exceptions.

        If a listener returns a coroutine it is scheduled via
        ``asyncio.ensure_future`` so that async listeners (e.g. in tests)
        do not produce "coroutine was never awaited" warnings.
        """
        import inspect

        for listener in list(self._listeners):
            try:
                result = listener()
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_event_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        pass
            except Exception:
                _log.exception("ChatState listener raised an exception")
