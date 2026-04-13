"""SSE (Server-Sent Events) helper utilities for the REST adapter.

Provides low-level byte-framing functions for the SSE protocol as defined
in https://html.spec.whatwg.org/multipage/server-sent-events.html.
"""

from __future__ import annotations


def format_sse(event: str, data: str) -> bytes:
    """Format a single SSE frame.

    Produces the canonical two-field SSE frame::

        event: <name>\\n
        data: <payload>\\n
        \\n

    Args:
        event: SSE event name (e.g. ``"token"``, ``"done"``).
        data: Event payload (typically a JSON string).

    Returns:
        UTF-8–encoded bytes representing one complete SSE frame.
    """
    return f"event: {event}\ndata: {data}\n\n".encode()


#: Standard SSE keepalive comment. Send periodically to prevent proxy timeouts.
HEARTBEAT: bytes = b": keepalive\n\n"
