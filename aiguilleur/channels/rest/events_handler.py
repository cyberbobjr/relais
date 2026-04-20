"""SSE push endpoint handler for GET /v1/events.

Provides a persistent Server-Sent Events stream that delivers messages
pushed to the authenticated user's outgoing push stream in near real-time.

Each connected client gets its own asyncio.Queue (via PushRegistry.subscribe).
The handler loops, waiting for new items from the queue with a 15-second
timeout. On timeout it emits a keepalive comment (': ping\\n\\n') to prevent
proxy disconnects. On a real payload it emits a 'data:' SSE frame.

The PushRegistry reader task (created lazily per user) does the actual
Redis XREAD; this handler only drains the queue.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from aiguilleur.channels.rest.sse import HEARTBEAT

logger = logging.getLogger("aiguilleur.rest.events_handler")

_KEEPALIVE_TIMEOUT = 15.0  # seconds before emitting a keepalive ping


async def events_handler(request: web.Request) -> web.StreamResponse:
    """Handle GET /v1/events — persistent SSE push stream.

    Resolves the caller's identity from ``request["user_record"]`` (set by
    BearerAuthMiddleware). Subscribes to the PushRegistry to receive a
    dedicated asyncio.Queue for this connection. Loops indefinitely:

    - Waits up to 15 s for a new item on the queue.
    - On asyncio.TimeoutError: writes a keepalive comment frame.
    - On a real payload: writes a ``data: {payload}\\n\\n`` frame.
    - On disconnect (ConnectionResetError, asyncio.CancelledError):
      unsubscribes from the registry and returns.

    Args:
        request: Authenticated aiohttp request.  Must have:
            - ``request["user_record"]``: object with ``.user_id`` attribute.
            - ``request.app["_push_registry"]``: ``PushRegistry`` instance.

    Returns:
        An open ``web.StreamResponse`` sending SSE frames.
    """
    user_record = request["user_record"]
    user_id: str = user_record.user_id
    push_registry = request.app["_push_registry"]

    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    queue: asyncio.Queue = await push_registry.subscribe(user_id)
    logger.debug("SSE push client connected user=%s", user_id)

    try:
        while True:
            try:
                payload: str = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_TIMEOUT)
            except asyncio.TimeoutError:
                await response.write(HEARTBEAT)
                continue

            await response.write(f"data: {payload}\n\n".encode())

    except (ConnectionResetError, asyncio.CancelledError):
        logger.debug("SSE push client disconnected user=%s", user_id)
    except Exception as exc:
        logger.error("SSE push handler error user=%s: %s", user_id, exc)
    finally:
        await push_registry.unsubscribe(user_id, queue)

    return response
