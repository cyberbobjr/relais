"""Tests for relais_tui.client — TDD RED phase."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from relais_tui.client import RelaisClient
from relais_tui.config import Config
from relais_tui.sse_parser import DoneEvent, ErrorEvent, Keepalive, ProgressEvent, TokenEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> Config:
    """Build a Config with test defaults."""
    defaults = {
        "api_url": "http://localhost:8080",
        "api_key": "test-key-123",
        "request_timeout": 10,
    }
    defaults.update(overrides)
    return Config(**defaults)


def _mock_response(
    *,
    status_code: int = 200,
    json_data: dict | None = None,
    content_type: str = "application/json",
    stream_chunks: list[bytes] | None = None,
) -> httpx.Response:
    """Build a mock httpx.Response.

    Args:
        status_code: HTTP status code.
        json_data: JSON body for non-streaming responses.
        content_type: Content-Type header value.
        stream_chunks: If set, used for async byte iteration (SSE mode).

    Returns:
        A configured httpx.Response mock.
    """
    headers = {"content-type": content_type}
    if json_data is not None:
        body = json.dumps(json_data).encode()
    else:
        body = b""
    request = httpx.Request("POST", "http://test/v1/messages")
    resp = httpx.Response(status_code, headers=headers, content=body, request=request)
    return resp


# ---------------------------------------------------------------------------
# Construction & lifecycle
# ---------------------------------------------------------------------------


class TestClientConstruction:
    """Client instantiation and resource management."""

    def test_creates_from_config(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        assert client.base_url == "http://localhost:8080"

    def test_auth_header_set(self) -> None:
        cfg = _config(api_key="sk-secret")
        client = RelaisClient(cfg)
        assert client.api_key == "sk-secret"

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        await client.close()
        # Should not raise on double close
        await client.close()


# ---------------------------------------------------------------------------
# healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    """Tests for healthz() endpoint check."""

    @pytest.mark.asyncio
    async def test_healthy(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        mock_resp = _mock_response(status_code=200, json_data={"status": "ok"})

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            ok = await client.healthz()

        assert ok is True
        await client.close()

    @pytest.mark.asyncio
    async def test_unhealthy(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        mock_resp = _mock_response(status_code=503, json_data={"status": "error"})

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            ok = await client.healthz()

        assert ok is False
        await client.close()

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            ok = await client.healthz()

        assert ok is False
        await client.close()


# ---------------------------------------------------------------------------
# send_message (JSON mode)
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Tests for send_message() — synchronous JSON response."""

    @pytest.mark.asyncio
    async def test_returns_done_event(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        resp_data = {
            "content": "Hello!",
            "correlation_id": "c-1",
            "session_id": "s-1",
        }
        mock_resp = _mock_response(status_code=200, json_data=resp_data)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await client.send_message("Hi", session_id="s-1")

        assert isinstance(result, DoneEvent)
        assert result.content == "Hello!"
        assert result.correlation_id == "c-1"
        assert result.session_id == "s-1"
        await client.close()

    @pytest.mark.asyncio
    async def test_sends_correct_body(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        mock_resp = _mock_response(
            status_code=200,
            json_data={"content": "ok", "correlation_id": "c", "session_id": "s"},
        )

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await client.send_message("test content", session_id="my-session")

            call_kwargs = mock_http.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["content"] == "test content"
            assert body["session_id"] == "my-session"

        await client.close()

    @pytest.mark.asyncio
    async def test_no_session_id_omits_field(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        mock_resp = _mock_response(
            status_code=200,
            json_data={"content": "ok", "correlation_id": "c", "session_id": "s"},
        )

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await client.send_message("hello")

            call_kwargs = mock_http.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "session_id" not in body

        await client.close()

    @pytest.mark.asyncio
    async def test_auth_header_sent(self) -> None:
        cfg = _config(api_key="sk-test")
        client = RelaisClient(cfg)
        mock_resp = _mock_response(
            status_code=200,
            json_data={"content": "ok", "correlation_id": "c", "session_id": "s"},
        )

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await client.send_message("hi")

            call_kwargs = mock_http.post.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers") or {}
            assert headers.get("Authorization") == "Bearer sk-test"

        await client.close()

    @pytest.mark.asyncio
    async def test_error_status_raises(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)
        mock_resp = _mock_response(
            status_code=400,
            json_data={"error": "Bad Request", "detail": "missing content"},
        )

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            with pytest.raises(httpx.HTTPStatusError):
                await client.send_message("hi")

        await client.close()


# ---------------------------------------------------------------------------
# stream_message (SSE mode)
# ---------------------------------------------------------------------------


async def _async_iter(chunks: list[bytes]):
    """Create an async iterator from a list of byte chunks."""
    for chunk in chunks:
        yield chunk


class TestStreamMessage:
    """Tests for stream_message() — SSE streaming mode."""

    @pytest.mark.asyncio
    async def test_streams_token_events(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        sse_frames = [
            b'event: token\ndata: {"t": "Hello"}\n\n',
            b'event: token\ndata: {"t": " world"}\n\n',
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter(sse_frames)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        tokens = [e for e in events if isinstance(e, TokenEvent)]
        assert len(tokens) == 2
        assert tokens[0].text == "Hello"
        assert tokens[1].text == " world"
        await client.close()

    @pytest.mark.asyncio
    async def test_streams_done_event(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        done_data = json.dumps({
            "content": "Full reply",
            "correlation_id": "c-1",
            "session_id": "s-1",
        })
        sse_frames = [
            b'event: token\ndata: {"t": "Full"}\n\n',
            f"event: done\ndata: {done_data}\n\n".encode(),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter(sse_frames)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        done_events = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done_events) == 1
        assert done_events[0].content == "Full reply"
        assert done_events[0].session_id == "s-1"
        await client.close()

    @pytest.mark.asyncio
    async def test_streams_progress_events(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        progress_data = json.dumps({"event": "tool_call", "detail": "search"})
        sse_frames = [
            f"event: progress\ndata: {progress_data}\n\n".encode(),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter(sse_frames)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        progress = [e for e in events if isinstance(e, ProgressEvent)]
        assert len(progress) == 1
        assert progress[0].event == "tool_call"
        assert progress[0].detail == "search"
        await client.close()

    @pytest.mark.asyncio
    async def test_streams_error_event(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        error_data = json.dumps({"error": "Request timed out", "correlation_id": "c-2"})
        sse_frames = [
            f"event: error\ndata: {error_data}\n\n".encode(),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter(sse_frames)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(errors) == 1
        assert errors[0].error == "Request timed out"
        await client.close()

    @pytest.mark.asyncio
    async def test_keepalive_filtered_out(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        sse_frames = [
            b": keepalive\n\n",
            b'event: token\ndata: {"t": "ok"}\n\n',
            b": keepalive\n\n",
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter(sse_frames)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        # Keepalives should be filtered — only the token event remains
        assert len(events) == 1
        assert isinstance(events[0], TokenEvent)
        await client.close()

    @pytest.mark.asyncio
    async def test_json_fallback(self) -> None:
        """If server returns JSON instead of SSE, emit a synthetic DoneEvent."""
        cfg = _config()
        client = RelaisClient(cfg)

        json_body = {
            "content": "Sync reply",
            "correlation_id": "c-3",
            "session_id": "s-3",
        }
        json_bytes = json.dumps(json_body).encode()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aread = AsyncMock(return_value=json_bytes)
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            events = [ev async for ev in client.stream_message("Hi")]

        assert len(events) == 1
        assert isinstance(events[0], DoneEvent)
        assert events[0].content == "Sync reply"
        assert events[0].session_id == "s-3"
        await client.close()

    @pytest.mark.asyncio
    async def test_sends_sse_accept_header(self) -> None:
        cfg = _config(api_key="sk-sse")
        client = RelaisClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter([])
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            _ = [ev async for ev in client.stream_message("Hi", session_id="s")]

            call_args = mock_http.stream.call_args
            headers = call_args.kwargs.get("headers", {})
            assert headers.get("Accept") == "text/event-stream"
            assert headers.get("Authorization") == "Bearer sk-sse"

        await client.close()

    @pytest.mark.asyncio
    async def test_session_id_in_body(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter([])
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            _ = [ev async for ev in client.stream_message("test", session_id="sess-42")]

            call_args = mock_http.stream.call_args
            body = call_args.kwargs.get("json", {})
            assert body["content"] == "test"
            assert body["session_id"] == "sess-42"

        await client.close()

    @pytest.mark.asyncio
    async def test_no_session_id_omits_field(self) -> None:
        cfg = _config()
        client = RelaisClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_bytes = lambda: _async_iter([])
        mock_resp.aclose = AsyncMock()

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "_http", new_callable=lambda: MagicMock()) as mock_http:
            mock_http.stream = MagicMock(return_value=mock_stream)
            _ = [ev async for ev in client.stream_message("hello")]

            call_args = mock_http.stream.call_args
            body = call_args.kwargs.get("json", {})
            assert "session_id" not in body

        await client.close()
