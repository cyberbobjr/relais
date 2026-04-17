"""Stateful SSE line-buffer parser for RELAIS REST API.

Feeds raw bytes from an HTTP response and yields typed event objects.
Handles partial chunks, multi-byte UTF-8 splits, and CR/LF line endings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Generator


@dataclass(frozen=True)
class TokenEvent:
    """A streamed token fragment.

    Attributes:
        text: The token text chunk.
    """

    text: str


@dataclass(frozen=True)
class DoneEvent:
    """Final response with full content.

    Attributes:
        content: The complete assistant reply.
        correlation_id: Request correlation identifier.
        session_id: Session identifier for continuity.
    """

    content: str
    correlation_id: str
    session_id: str


@dataclass(frozen=True)
class ProgressEvent:
    """Progress indicator from the agent pipeline.

    Attributes:
        event: The progress event type (e.g. ``tool_call``).
        detail: Additional detail string.
    """

    event: str
    detail: str


@dataclass(frozen=True)
class ErrorEvent:
    """Error event indicating stream failure.

    Attributes:
        error: Human-readable error reason.
        correlation_id: Request correlation identifier.
    """

    error: str
    correlation_id: str


@dataclass(frozen=True)
class Keepalive:
    """SSE comment used as heartbeat."""


SSEEvent = TokenEvent | DoneEvent | ProgressEvent | ErrorEvent | Keepalive


class SSEParser:
    """Stateful SSE line-buffer parser.

    Accumulates raw bytes from ``feed()`` calls, splits on line boundaries,
    and emits typed event objects when a complete SSE frame (terminated by
    a blank line) is detected.

    Handles:
    - Partial chunks (network fragmentation)
    - CR/LF and LF line endings
    - Multi-byte UTF-8 across chunk boundaries
    - SSE comments (``: keepalive``)
    """

    def __init__(self) -> None:
        self._buf: bytes = b""
        self._event_type: str = ""
        self._data: str = ""
        self._has_comment: bool = False

    def feed(self, chunk: bytes) -> Generator[
        TokenEvent | DoneEvent | ProgressEvent | ErrorEvent | Keepalive,
        None,
        None,
    ]:
        """Feed raw bytes and yield parsed SSE events.

        Bytes are buffered internally. Complete lines (terminated by ``\\n``
        or ``\\r\\n``) are processed. A blank line signals the end of an
        SSE frame and triggers event emission.

        Args:
            chunk: Raw bytes from the HTTP response stream.

        Yields:
            Typed event objects for each complete SSE frame.
        """
        self._buf += chunk

        while b"\n" in self._buf:
            line_bytes, self._buf = self._buf.split(b"\n", 1)

            # Strip trailing CR for CRLF support
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]

            # Blank line = end of SSE frame
            if not line_bytes:
                event = self._emit()
                if event is not None:
                    yield event
                continue

            line = line_bytes.decode("utf-8", errors="replace")

            if line.startswith(":"):
                # SSE comment (keepalive)
                self._has_comment = True
            elif line.startswith("event: "):
                self._event_type = line[7:]
            elif line.startswith("data: "):
                self._data = line[6:]

    def reset(self) -> None:
        """Reset all parser state.

        Discards any buffered bytes and partial event fields.
        """
        self._buf = b""
        self._event_type = ""
        self._data = ""
        self._has_comment = False

    def _emit(self) -> TokenEvent | DoneEvent | ProgressEvent | ErrorEvent | Keepalive | None:
        """Build an event from accumulated fields and reset.

        Returns:
            A typed event, or ``None`` if the frame was incomplete or
            contained an unknown event type.
        """
        event_type = self._event_type
        data = self._data
        has_comment = self._has_comment

        # Reset for next frame
        self._event_type = ""
        self._data = ""
        self._has_comment = False

        # Comment-only frame (keepalive)
        if has_comment and not event_type and not data:
            return Keepalive()

        # Need both event type and data for a real event
        if not event_type or not data:
            return None

        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return None

        if event_type == "token":
            return TokenEvent(text=payload.get("t", ""))
        elif event_type == "done":
            return DoneEvent(
                content=payload.get("content", ""),
                correlation_id=payload.get("correlation_id", ""),
                session_id=payload.get("session_id", ""),
            )
        elif event_type == "progress":
            return ProgressEvent(
                event=payload.get("event", ""),
                detail=payload.get("detail", ""),
            )
        elif event_type == "error":
            return ErrorEvent(
                error=payload.get("error", ""),
                correlation_id=payload.get("correlation_id", ""),
            )

        return None
