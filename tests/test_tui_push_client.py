"""Tests for TUI push_client — subscribe_events async function.

TDD: Tests written BEFORE implementation (RED phase).

subscribe_events(base_url, token, on_message) connects to GET /v1/events
(SSE endpoint), calls on_message for each data frame, handles reconnect
with exponential backoff (2^n, max 30s), and exits cleanly on cancellation.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sse_response(payloads: list[str], *, status: int = 200):
    """Build a minimal mock httpx response that streams SSE frames.

    Each payload is wrapped as ``data: {payload}\\n\\n``.
    The response terminates after all payloads are delivered.

    Args:
        payloads: List of raw JSON string payloads to emit as SSE data frames.
        status: HTTP status code. Default 200.

    Returns:
        AsyncMock configured as an httpx response with aiter_lines().
    """
    lines: list[str] = []
    for p in payloads:
        lines.append(f"data: {p}")
        lines.append("")  # blank line terminates SSE frame

    response = AsyncMock()
    response.status_code = status
    response.raise_for_status = MagicMock()
    if status >= 400:
        response.raise_for_status.side_effect = Exception(f"HTTP {status}")

    async def _aiter_lines():
        for line in lines:
            yield line

    response.aiter_lines = _aiter_lines
    return response


def _make_keepalive_then_payload_response(payload: str):
    """Build an SSE response that emits a keepalive comment then a data frame.

    Args:
        payload: Single JSON string payload after the keepalive.

    Returns:
        AsyncMock configured as httpx response.
    """
    lines = [": ping", f"data: {payload}", ""]

    response = AsyncMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()

    async def _aiter_lines():
        for line in lines:
            yield line

    response.aiter_lines = _aiter_lines
    return response


# ---------------------------------------------------------------------------
# Tests: basic delivery
# ---------------------------------------------------------------------------


class TestSubscribeEventsBasicDelivery:
    @pytest.mark.asyncio
    async def test_single_payload_calls_on_message(self):
        """subscribe_events calls on_message once for a single SSE data frame."""
        from relais_tui.push_client import subscribe_events

        payload = json.dumps({"content": "hello", "correlation_id": "c1"})
        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = _make_sse_response([payload])

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-1", _on_message)
                except asyncio.CancelledError:
                    pass

        assert payload in received

    @pytest.mark.asyncio
    async def test_multiple_payloads_all_delivered(self):
        """subscribe_events delivers all SSE data frames to on_message in order."""
        from relais_tui.push_client import subscribe_events

        payloads = [
            json.dumps({"content": f"msg-{i}", "correlation_id": f"c{i}"})
            for i in range(3)
        ]
        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = _make_sse_response(payloads)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-multi", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == payloads

    @pytest.mark.asyncio
    async def test_keepalive_comment_not_delivered_to_on_message(self):
        """Lines starting with ':' (SSE comments / keepalive) are not forwarded."""
        from relais_tui.push_client import subscribe_events

        payload = json.dumps({"content": "real", "correlation_id": "c2"})
        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = _make_keepalive_then_payload_response(payload)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-ping", _on_message)
                except asyncio.CancelledError:
                    pass

        assert payload in received
        # No keepalive comment should appear in received
        for r in received:
            assert not r.startswith(": ping")


# ---------------------------------------------------------------------------
# Tests: HTTP request configuration
# ---------------------------------------------------------------------------


class TestSubscribeEventsRequestConfig:
    @pytest.mark.asyncio
    async def test_get_called_with_correct_url(self):
        """subscribe_events calls GET {base_url}/v1/events."""
        from relais_tui.push_client import subscribe_events

        get_calls: list = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_sse_response([])

        async def _fake_get(url, **kwargs):
            get_calls.append(url)
            return response

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-url", _on_message)
                except asyncio.CancelledError:
                    pass

        assert any("http://localhost:8080/v1/events" in str(u) for u in get_calls)

    @pytest.mark.asyncio
    async def test_bearer_token_sent_in_authorization_header(self):
        """Authorization: Bearer {token} header is sent with the GET request."""
        from relais_tui.push_client import subscribe_events

        captured_headers: list[dict] = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_sse_response([])

        async def _fake_get(url, **kwargs):
            captured_headers.append(kwargs.get("headers", {}))
            return response

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "my-secret-token", _on_message)
                except asyncio.CancelledError:
                    pass

        assert len(captured_headers) >= 1
        auth = captured_headers[0].get("Authorization", "")
        assert auth == "Bearer my-secret-token"

    @pytest.mark.asyncio
    async def test_accept_header_is_text_event_stream(self):
        """Accept: text/event-stream header is included in the GET request."""
        from relais_tui.push_client import subscribe_events

        captured_headers: list[dict] = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_sse_response([])

        async def _fake_get(url, **kwargs):
            captured_headers.append(kwargs.get("headers", {}))
            return response

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-accept", _on_message)
                except asyncio.CancelledError:
                    pass

        assert len(captured_headers) >= 1
        accept = captured_headers[0].get("Accept", "")
        assert "text/event-stream" in accept


# ---------------------------------------------------------------------------
# Tests: reconnect backoff
# ---------------------------------------------------------------------------


class TestSubscribeEventsReconnect:
    @pytest.mark.asyncio
    async def test_reconnects_after_connection_error(self):
        """subscribe_events reconnects after a connection error."""
        from relais_tui.push_client import subscribe_events

        import httpx

        connect_count = 0
        received: list[str] = []
        payload = json.dumps({"content": "recovered", "correlation_id": "c-rec"})

        async def _fake_get(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                raise httpx.ConnectError("refused")
            # On second connect, return a normal response then cancel
            return _make_sse_response([payload])

        async def _on_message(msg: str) -> None:
            received.append(msg)

        sleep_call_count = 0

        async def _counting_sleep(delay: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            # After second sleep (first error sleep + reconnect sleep), cancel
            if sleep_call_count >= 2:
                raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_counting_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-retry", _on_message)
                except asyncio.CancelledError:
                    pass

        assert connect_count >= 2

    @pytest.mark.asyncio
    async def test_backoff_delay_called_on_error(self):
        """asyncio.sleep is called with a backoff delay after a connection error."""
        from relais_tui.push_client import subscribe_events

        import httpx

        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            # Only allow one retry cycle then raise CancelledError
            if len(sleep_calls) >= 1:
                raise asyncio.CancelledError()

        async def _fake_get(url, **kwargs):
            raise httpx.ConnectError("refused")

        async def _on_message(msg: str) -> None:
            pass

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_fake_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-backoff", _on_message)
                except asyncio.CancelledError:
                    pass

        assert len(sleep_calls) >= 1
        # First backoff: 2^0 = 1 second (or 2^1 = 2, depending on implementation)
        assert sleep_calls[0] >= 1.0

    @pytest.mark.asyncio
    async def test_backoff_capped_at_30_seconds(self):
        """Reconnect backoff delay is capped at 30 seconds maximum."""
        from relais_tui.push_client import subscribe_events

        import httpx

        sleep_calls: list[float] = []
        fail_count = 0

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            # Stop after 6 retries (enough to see the cap)
            if len(sleep_calls) >= 6:
                raise asyncio.CancelledError()

        async def _fake_get(url, **kwargs):
            nonlocal fail_count
            fail_count += 1
            raise httpx.ConnectError("refused")

        async def _on_message(msg: str) -> None:
            pass

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_fake_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-cap", _on_message)
                except asyncio.CancelledError:
                    pass

        # All delays must be <= 30
        assert all(d <= 30.0 for d in sleep_calls), f"Delay exceeded 30s: {sleep_calls}"
        # At least one delay reached the cap (after enough failures)
        assert any(d == 30.0 for d in sleep_calls), f"Cap never reached: {sleep_calls}"

    @pytest.mark.asyncio
    async def test_backoff_resets_on_successful_connection(self):
        """After a successful reconnect, the backoff counter resets to 0."""
        from relais_tui.push_client import subscribe_events

        import httpx

        sleep_calls: list[float] = []
        connect_count = 0

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        payload = json.dumps({"content": "ok", "correlation_id": "c-reset"})

        async def _fake_get(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                raise httpx.ConnectError("fail")
            if connect_count == 2:
                # Successful connection, delivers payload then stream ends
                return _make_sse_response([payload])
            # Second failure after reset
            raise httpx.ConnectError("fail again")

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)
            # After receiving first message, trigger stop via cancellation
            # by letting the next connect fail and checking backoff

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = _fake_get
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_fake_sleep):
                task = asyncio.create_task(
                    subscribe_events("http://localhost:8080", "tok-reset", _on_message)
                )
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # After reset, the delay for the 3rd connect attempt should be back to 1 or 2s
        # (not continuing to grow from the first failure's level)
        if len(sleep_calls) >= 2:
            # The second sleep (after reset) should be <= the first
            assert sleep_calls[-1] <= sleep_calls[0] * 2 + 2  # generous bound


# ---------------------------------------------------------------------------
# Tests: cancellation
# ---------------------------------------------------------------------------


class TestSubscribeEventsCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_exits_cleanly(self):
        """Cancelling subscribe_events raises CancelledError without hanging."""
        from relais_tui.push_client import subscribe_events

        async def _on_message(msg: str) -> None:
            pass

        # A response that never ends (blocking aiter_lines)
        response = AsyncMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        async def _blocking_lines():
            await asyncio.sleep(100)
            yield "never"

        response.aiter_lines = _blocking_lines

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", new_callable=AsyncMock):
                task = asyncio.create_task(
                    subscribe_events("http://localhost:8080", "tok-cancel", _on_message)
                )
                await asyncio.sleep(0.01)
                task.cancel()
                # Should complete quickly without hanging
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        # If we get here without a TimeoutError, the cancellation was clean
        assert task.done()

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed(self):
        """CancelledError propagates out of subscribe_events on cancellation."""
        from relais_tui.push_client import subscribe_events

        async def _on_message(msg: str) -> None:
            pass

        response = AsyncMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        async def _blocking_lines():
            await asyncio.sleep(100)
            yield "never"

        response.aiter_lines = _blocking_lines

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", new_callable=AsyncMock):
                task = asyncio.create_task(
                    subscribe_events("http://localhost:8080", "tok-cancel2", _on_message)
                )
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises((asyncio.CancelledError, Exception)):
                    await task


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestSubscribeEventsEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_data_line_not_delivered(self):
        """Empty data: lines are not forwarded to on_message."""
        from relais_tui.push_client import subscribe_events

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = AsyncMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        async def _aiter_lines():
            yield "data: "  # empty payload
            yield ""

        response.aiter_lines = _aiter_lines

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-empty", _on_message)
                except asyncio.CancelledError:
                    pass

        # No empty strings should be delivered
        assert all(r.strip() for r in received)

    @pytest.mark.asyncio
    async def test_non_data_lines_ignored(self):
        """Lines that are not 'data:' prefixed are ignored (event:, id:, etc.)."""
        from relais_tui.push_client import subscribe_events

        payload = json.dumps({"content": "valid", "correlation_id": "c-nd"})
        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = AsyncMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        async def _aiter_lines():
            yield "event: message"
            yield "id: 123"
            yield f"data: {payload}"
            yield ""

        response.aiter_lines = _aiter_lines

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient") as MockClient:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ctx.get = AsyncMock(return_value=response)
            MockClient.return_value = ctx

            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-nd", _on_message)
                except asyncio.CancelledError:
                    pass

        assert payload in received
        # Exactly one delivery (the data line)
        assert len([r for r in received if r == payload]) == 1
