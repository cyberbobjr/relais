"""HTTP client for the RELAIS REST SSE API.

Wraps ``httpx.AsyncClient`` to provide typed methods for sending messages
(JSON mode) and streaming responses (SSE mode) against ``POST /v1/messages``.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

import httpx

from relais_tui.config import Config
from relais_tui.sse_parser import (
    DoneEvent,
    Keepalive,
    SSEParser,
    TokenEvent,
    ProgressEvent,
    ErrorEvent,
)

logger = logging.getLogger(__name__)

_MESSAGES_PATH = "/v1/messages"
_HEALTHZ_PATH = "/healthz"

SSEEvent = TokenEvent | DoneEvent | ProgressEvent | ErrorEvent


class RelaisClient:
    """Async HTTP client for the RELAIS REST API.

    Provides ``healthz()``, ``send_message()`` (JSON), and
    ``stream_message()`` (SSE) methods. Keepalive events are filtered
    out of the stream automatically.

    Args:
        config: TUI configuration with ``api_url``, ``api_key``, and
            ``request_timeout``.
    """

    def __init__(self, config: Config) -> None:
        self.base_url: str = config.api_url
        self.api_key: str = config.api_key
        self._timeout = config.request_timeout
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(config.request_timeout),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client.

        Safe to call multiple times.
        """
        if not self._http.is_closed:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # healthz
    # ------------------------------------------------------------------

    async def healthz(self) -> bool:
        """Check if the RELAIS REST server is reachable and healthy.

        Returns:
            ``True`` if the server responds with 2xx, ``False`` otherwise.
        """
        try:
            logger.debug("healthz → GET %s%s", self.base_url, _HEALTHZ_PATH)
            resp = await self._http.get(_HEALTHZ_PATH)
            logger.debug("healthz ← %d", resp.status_code)
            return resp.is_success
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            logger.warning("healthz failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # send_message (JSON)
    # ------------------------------------------------------------------

    async def send_message(
        self, content: str, *, session_id: str | None = None
    ) -> DoneEvent:
        """Send a message and receive a JSON response.

        Args:
            content: The user message text.
            session_id: Optional session ID for conversation continuity.

        Returns:
            A ``DoneEvent`` built from the JSON response.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """
        body: dict = {"content": content}
        if session_id is not None:
            body["session_id"] = session_id

        logger.debug(
            "send_message → POST %s%s session=%s api_key_set=%s",
            self.base_url, _MESSAGES_PATH, session_id, bool(self.api_key),
        )
        resp = await self._http.post(
            _MESSAGES_PATH,
            json=body,
            headers=self._auth_headers(),
        )
        logger.debug("send_message ← %d", resp.status_code)
        if not resp.is_success:
            logger.error("send_message error %d: %s", resp.status_code, resp.text)
        resp.raise_for_status()

        data = resp.json()
        return DoneEvent(
            content=data.get("content", ""),
            correlation_id=data.get("correlation_id", ""),
            session_id=data.get("session_id", ""),
        )

    # ------------------------------------------------------------------
    # stream_message (SSE)
    # ------------------------------------------------------------------

    async def stream_message(
        self, content: str, *, session_id: str | None = None
    ) -> AsyncGenerator[SSEEvent, None]:
        """Send a message and stream SSE events.

        Keepalive events are silently filtered out.  If the server falls
        back to a plain JSON response (``Content-Type: application/json``),
        a synthetic ``DoneEvent`` is emitted.

        Args:
            content: The user message text.
            session_id: Optional session ID for conversation continuity.

        Yields:
            Typed SSE events (``TokenEvent``, ``ProgressEvent``,
            ``DoneEvent``, ``ErrorEvent``).
        """
        body: dict = {"content": content}
        if session_id is not None:
            body["session_id"] = session_id

        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"

        logger.debug(
            "stream_message → POST %s%s session=%s api_key_set=%s",
            self.base_url, _MESSAGES_PATH, session_id, bool(self.api_key),
        )
        async with self._http.stream(
            "POST", _MESSAGES_PATH, json=body, headers=headers
        ) as resp:
            logger.debug("stream_message ← %d content-type=%s", resp.status_code, resp.headers.get("content-type", ""))
            if not resp.is_success:
                body_bytes = await resp.aread()
                logger.error("stream_message error %d: %s", resp.status_code, body_bytes.decode(errors="replace"))
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")

            # JSON fallback — server did not stream
            if "application/json" in content_type:
                raw = await resp.aread()
                data = json.loads(raw)
                yield DoneEvent(
                    content=data.get("content", ""),
                    correlation_id=data.get("correlation_id", ""),
                    session_id=data.get("session_id", ""),
                )
                return

            # SSE streaming
            parser = SSEParser()
            async for chunk in resp.aiter_bytes():
                for event in parser.feed(chunk):
                    if not isinstance(event, Keepalive):
                        yield event

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build authorization headers.

        Returns:
            Dict with ``Authorization: Bearer <key>`` if api_key is set.
        """
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
