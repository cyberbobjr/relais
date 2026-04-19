"""Async SSE push client for the RELAIS GET /v1/events endpoint.

Connects to the persistent SSE push stream exposed by the REST adapter and
calls ``on_message`` for every data frame received.  Automatically reconnects
with exponential backoff (2^n seconds, capped at 30 s) on connection errors.
Exits cleanly when cancelled.

Usage::

    import asyncio
    from relais_tui.push_client import subscribe_events

    async def handle(payload: str) -> None:
        print("push:", payload)

    asyncio.run(subscribe_events("http://localhost:8080", "my-api-key", handle))
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from relais_tui.sse_parser import Keepalive, SSEParser

_log = logging.getLogger(__name__)

_MAX_BACKOFF: float = 30.0
_EVENTS_PATH: str = "/v1/events"


async def subscribe_events(
    base_url: str,
    token: str,
    on_message: Callable[[str], Awaitable[None]],
) -> None:
    """Subscribe to the RELAIS push event stream.

    Opens a persistent SSE connection to ``{base_url}/v1/events`` and calls
    ``on_message`` for each ``data:`` frame received.  Reconnects with
    exponential backoff on connection or HTTP errors.  Exits cleanly when
    cancelled.

    Args:
        base_url: Base URL of the RELAIS REST adapter (e.g. ``"http://localhost:8080"``).
        token: Bearer API key for authentication.
        on_message: Async callable invoked with each raw payload string from
            a ``data:`` frame.
    """
    url = base_url.rstrip("/") + _EVENTS_PATH
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    retry_count: int = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    retry_count = 0  # successful connection — reset backoff

                    parser = SSEParser()
                    async for chunk in response.aiter_bytes():
                        for event in parser.feed(chunk):
                            if isinstance(event, Keepalive):
                                continue
                            if hasattr(event, "text"):
                                # TokenEvent from push stream — deliver raw
                                await on_message(event.text)
                            elif hasattr(event, "content"):
                                await on_message(event.content)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                retry_count += 1
                delay = min(2 ** retry_count, _MAX_BACKOFF)
                _log.warning(
                    "SSE push connection failed (attempt %d), retrying in %.0fs: %s",
                    retry_count,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue

            # Stream ended cleanly — apply progressive backoff in case server is shedding
            retry_count += 1
            delay = min(2 ** retry_count, _MAX_BACKOFF)
            _log.debug("SSE push stream ended cleanly, reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
