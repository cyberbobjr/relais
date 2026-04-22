"""Tests for TUI push_client — subscribe_events async function.

subscribe_events(base_url, token, on_message) connects to GET /v1/events
(SSE endpoint), calls on_message for each data frame, handles reconnect
with exponential backoff (2^n, max 30s), and exits cleanly on cancellation.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_done_event_bytes(content: str, corr_id: str = "c1", session_id: str = "") -> bytes:
    """Build a valid SSE 'done' event as bytes."""
    data = json.dumps({"content": content, "correlation_id": corr_id, "session_id": session_id})
    return f"event: done\ndata: {data}\n\n".encode()


def _make_token_event_bytes(text: str) -> bytes:
    """Build a valid SSE 'token' event as bytes."""
    data = json.dumps({"t": text})
    return f"event: token\ndata: {data}\n\n".encode()


def _make_bytes_response(byte_chunks: list[bytes], *, status: int = 200) -> AsyncMock:
    """Build a minimal mock httpx response that streams bytes chunks."""
    response = AsyncMock()
    response.status_code = status
    response.raise_for_status = MagicMock()
    if status >= 400:
        response.raise_for_status.side_effect = Exception(f"HTTP {status}")

    async def _aiter_bytes():
        for chunk in byte_chunks:
            yield chunk

    response.aiter_bytes = _aiter_bytes
    return response


def _make_stream_ctx(response: AsyncMock) -> MagicMock:
    """Build an async context manager mock that yields response."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_outer_ctx(stream_ctx: MagicMock) -> MagicMock:
    """Build the httpx.AsyncClient() outer context manager mock."""
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=outer)
    outer.__aexit__ = AsyncMock(return_value=False)
    outer.stream = MagicMock(return_value=stream_ctx)
    return outer


# ---------------------------------------------------------------------------
# Tests: basic delivery
# ---------------------------------------------------------------------------


class TestSubscribeEventsBasicDelivery:
    @pytest.mark.asyncio
    async def test_single_payload_calls_on_message(self):
        """subscribe_events calls on_message once for a single SSE done event."""
        from relais_tui.push_client import subscribe_events

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        response = _make_bytes_response([_make_done_event_bytes("hello", "c1")])
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-1", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == ["hello"]

    @pytest.mark.asyncio
    async def test_multiple_payloads_all_delivered(self):
        """subscribe_events delivers all SSE done events to on_message in order."""
        from relais_tui.push_client import subscribe_events

        contents = [f"msg-{i}" for i in range(3)]
        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        chunks = [_make_done_event_bytes(c, f"c{i}") for i, c in enumerate(contents)]
        response = _make_bytes_response(chunks)
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-multi", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == contents

    @pytest.mark.asyncio
    async def test_keepalive_comment_not_delivered_to_on_message(self):
        """SSE comment frames (keepalive) are not forwarded to on_message."""
        from relais_tui.push_client import subscribe_events

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        keepalive_bytes = b": ping\n\n"
        real_bytes = _make_done_event_bytes("real", "c2")
        response = _make_bytes_response([keepalive_bytes, real_bytes])
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-ping", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == ["real"]


# ---------------------------------------------------------------------------
# Tests: HTTP request configuration
# ---------------------------------------------------------------------------


class TestSubscribeEventsRequestConfig:
    @pytest.mark.asyncio
    async def test_get_called_with_correct_url(self):
        """subscribe_events calls stream("GET", {base_url}/v1/events, ...)."""
        from relais_tui.push_client import subscribe_events

        stream_calls: list = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_bytes_response([])
        stream_ctx = _make_stream_ctx(response)
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _capturing_stream(method, url, **kwargs):
            stream_calls.append((method, url))
            return stream_ctx

        outer.stream = _capturing_stream

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-url", _on_message)
                except asyncio.CancelledError:
                    pass

        assert any("http://localhost:8080/v1/events" in str(url) for _, url in stream_calls)

    @pytest.mark.asyncio
    async def test_bearer_token_sent_in_authorization_header(self):
        """Authorization: Bearer {token} header is sent with the request."""
        from relais_tui.push_client import subscribe_events

        captured_headers: list[dict] = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_bytes_response([])
        stream_ctx = _make_stream_ctx(response)
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _capturing_stream(method, url, **kwargs):
            captured_headers.append(kwargs.get("headers", {}))
            return stream_ctx

        outer.stream = _capturing_stream

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
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
        """Accept: text/event-stream header is included in the request."""
        from relais_tui.push_client import subscribe_events

        captured_headers: list[dict] = []

        async def _on_message(msg: str) -> None:
            pass

        response = _make_bytes_response([])
        stream_ctx = _make_stream_ctx(response)
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _capturing_stream(method, url, **kwargs):
            captured_headers.append(kwargs.get("headers", {}))
            return stream_ctx

        outer.stream = _capturing_stream

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
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
        import httpx
        from relais_tui.push_client import subscribe_events

        connect_count = 0
        received: list[str] = []

        response_ok = _make_bytes_response([_make_done_event_bytes("recovered", "c-rec")])

        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _stream_side_effect(method, url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                err_ctx = MagicMock()
                err_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
                err_ctx.__aexit__ = AsyncMock(return_value=False)
                return err_ctx
            return _make_stream_ctx(response_ok)

        outer.stream = _stream_side_effect

        async def _on_message(msg: str) -> None:
            received.append(msg)

        sleep_call_count = 0

        async def _counting_sleep(delay: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_counting_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-retry", _on_message)
                except asyncio.CancelledError:
                    pass

        assert connect_count >= 2

    @pytest.mark.asyncio
    async def test_backoff_delay_called_on_error(self):
        """asyncio.sleep is called with a backoff delay after a connection error."""
        import httpx
        from relais_tui.push_client import subscribe_events

        sleep_calls: list[float] = []

        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _failing_stream(method, url, **kwargs):
            err_ctx = MagicMock()
            err_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
            err_ctx.__aexit__ = AsyncMock(return_value=False)
            return err_ctx

        outer.stream = _failing_stream

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            raise asyncio.CancelledError()

        async def _on_message(msg: str) -> None:
            pass

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_fake_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-backoff", _on_message)
                except asyncio.CancelledError:
                    pass

        assert len(sleep_calls) >= 1
        assert sleep_calls[0] >= 1.0

    @pytest.mark.asyncio
    async def test_backoff_capped_at_30_seconds(self):
        """Reconnect backoff delay is capped at 30 seconds maximum."""
        import httpx
        from relais_tui.push_client import subscribe_events

        sleep_calls: list[float] = []

        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _failing_stream(method, url, **kwargs):
            err_ctx = MagicMock()
            err_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
            err_ctx.__aexit__ = AsyncMock(return_value=False)
            return err_ctx

        outer.stream = _failing_stream

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            if len(sleep_calls) >= 6:
                raise asyncio.CancelledError()

        async def _on_message(msg: str) -> None:
            pass

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_fake_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-cap", _on_message)
                except asyncio.CancelledError:
                    pass

        assert all(d <= 30.0 for d in sleep_calls), f"Delay exceeded 30s: {sleep_calls}"
        assert any(d == 30.0 for d in sleep_calls), f"Cap never reached: {sleep_calls}"

    @pytest.mark.asyncio
    async def test_backoff_resets_on_successful_connection(self):
        """After a successful reconnect, the backoff counter resets to 0."""
        import httpx
        from relais_tui.push_client import subscribe_events

        sleep_calls: list[float] = []
        connect_count = 0

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=outer)
        outer.__aexit__ = AsyncMock(return_value=False)

        def _stream_side_effect(method, url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                err_ctx = MagicMock()
                err_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("fail"))
                err_ctx.__aexit__ = AsyncMock(return_value=False)
                return err_ctx
            if connect_count == 2:
                response = _make_bytes_response([_make_done_event_bytes("ok", "c-reset")])
                return _make_stream_ctx(response)
            err_ctx = MagicMock()
            err_ctx.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("fail again"))
            err_ctx.__aexit__ = AsyncMock(return_value=False)
            return err_ctx

        outer.stream = _stream_side_effect

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
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

        if len(sleep_calls) >= 2:
            assert sleep_calls[-1] <= sleep_calls[0] * 2 + 2


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

        response = AsyncMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()

        async def _blocking_bytes():
            await asyncio.sleep(100)
            yield b"never"

        response.aiter_bytes = _blocking_bytes
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", new_callable=AsyncMock):
                task = asyncio.create_task(
                    subscribe_events("http://localhost:8080", "tok-cancel", _on_message)
                )
                await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

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

        async def _blocking_bytes():
            await asyncio.sleep(100)
            yield b"never"

        response.aiter_bytes = _blocking_bytes
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
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
        """Empty data field in done event causes SSEParser to ignore the frame."""
        from relais_tui.push_client import subscribe_events

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        # event: done with empty data → SSEParser sees empty data → ignores
        empty_frame = b"event: done\ndata: \n\n"
        response = _make_bytes_response([empty_frame])
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-empty", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == []

    @pytest.mark.asyncio
    async def test_non_data_lines_ignored(self):
        """Only done/token events are forwarded; unknown event types are ignored."""
        from relais_tui.push_client import subscribe_events

        received: list[str] = []

        async def _on_message(msg: str) -> None:
            received.append(msg)

        # An unknown event type followed by a valid done event
        unknown_frame = b"event: message\nid: 123\ndata: ignored\n\n"
        valid_frame = _make_done_event_bytes("valid", "c-nd")
        response = _make_bytes_response([unknown_frame, valid_frame])
        stream_ctx = _make_stream_ctx(response)
        outer = _make_outer_ctx(stream_ctx)

        async def _cancelling_sleep(delay):
            raise asyncio.CancelledError()

        with patch("relais_tui.push_client.httpx.AsyncClient", return_value=outer):
            with patch("relais_tui.push_client.asyncio.sleep", side_effect=_cancelling_sleep):
                try:
                    await subscribe_events("http://localhost:8080", "tok-nd", _on_message)
                except asyncio.CancelledError:
                    pass

        assert received == ["valid"]
