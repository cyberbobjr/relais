"""Integration tests for GET /v1/events SSE push endpoint.

TDD: Tests written BEFORE implementation (RED phase).
Uses aiohttp.test_utils.TestClient with mock PushRegistry.

Because the SSE endpoint is an infinite streaming loop, tests use one of two
strategies to avoid hanging:
  1. Short-circuit: inject a ``_STOP`` sentinel into the queue that makes the
     handler raise CancelledError / break, ending the stream naturally.
  2. Partial read: open the connection, read the first chunk, then close.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Sentinel value that terminates the events_handler loop in tests
_STOP = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user_record(user_id: str = "usr_test") -> MagicMock:
    """Build a minimal UserRecord mock.

    Args:
        user_id: The stable user ID.

    Returns:
        MagicMock with .user_id attribute set.
    """
    record = MagicMock()
    record.user_id = user_id
    return record


def _make_terminating_registry(payloads: list[str]) -> MagicMock:
    """Build a PushRegistry mock whose queue terminates after delivering payloads.

    Delivers all payloads then raises asyncio.CancelledError on the next
    get(), which causes the handler loop to exit cleanly (as if disconnected).

    Args:
        payloads: List of JSON strings to deliver before terminating.

    Returns:
        AsyncMock behaving like PushRegistry.
    """
    registry = AsyncMock()

    async def _subscribe(user_id: str) -> asyncio.Queue:
        items = list(payloads)
        call_count = 0

        class _TermQueue(asyncio.Queue):
            """Queue that raises CancelledError after items are exhausted."""

            async def get(self):  # type: ignore[override]
                nonlocal call_count
                if not self.empty():
                    return await super().get()
                call_count += 1
                if call_count >= 1:
                    raise asyncio.CancelledError()
                return await super().get()

        q: asyncio.Queue = _TermQueue(maxsize=256)
        for item in items:
            await q.put(item)
        return q

    registry.subscribe = _subscribe
    registry.unsubscribe = AsyncMock()
    return registry


def _make_ping_then_terminate_registry() -> MagicMock:
    """Build a PushRegistry mock that triggers one TimeoutError then terminates.

    Sequence: first get() raises TimeoutError (→ keepalive), second raises
    CancelledError (→ loop exit).
    """
    registry = AsyncMock()

    async def _subscribe(user_id: str) -> asyncio.Queue:
        call_count = 0

        class _PingQueue(asyncio.Queue):
            async def get(self):  # type: ignore[override]
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise asyncio.TimeoutError()
                raise asyncio.CancelledError()

        return _PingQueue(maxsize=256)

    registry.subscribe = _subscribe
    registry.unsubscribe = AsyncMock()
    return registry


@asynccontextmanager
async def _test_client(app: web.Application):
    """Async context manager wrapping aiohttp TestClient."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _build_app_with_events(user_record=None, push_registry=None):
    """Build a minimal aiohttp app with the events_handler wired.

    Auth middleware is bypassed: user_record is injected directly via
    a simple @web.middleware that sets request["user_record"].

    Args:
        user_record: MagicMock to inject as request["user_record"].
        push_registry: PushRegistry mock to inject as app["_push_registry"].

    Returns:
        Configured web.Application.
    """
    from aiguilleur.channels.rest.events_handler import events_handler

    if user_record is None:
        user_record = _make_user_record()
    if push_registry is None:
        push_registry = _make_terminating_registry([])

    @web.middleware
    async def inject_user(request, handler):
        request["user_record"] = user_record
        return await handler(request)

    api_app = web.Application(middlewares=[inject_user])
    api_app["_push_registry"] = push_registry
    api_app.router.add_get("/events", events_handler)

    root = web.Application()
    root.add_subapp("/v1", api_app)
    return root


# ---------------------------------------------------------------------------
# Tests: response headers
# ---------------------------------------------------------------------------

class TestEventsHandlerHeaders:
    @pytest.mark.asyncio
    async def test_response_content_type_is_text_event_stream(self):
        """GET /v1/events returns Content-Type: text/event-stream."""
        payload = json.dumps({"content": "hello", "correlation_id": "c1"})
        registry = _make_terminating_registry([payload])
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            assert resp.status == 200
            assert "text/event-stream" in resp.headers.get("Content-Type", "")

    @pytest.mark.asyncio
    async def test_response_cache_control_is_no_cache(self):
        """GET /v1/events returns Cache-Control: no-cache."""
        payload = json.dumps({"content": "hello", "correlation_id": "c2"})
        registry = _make_terminating_registry([payload])
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            assert resp.headers.get("Cache-Control") == "no-cache"

    @pytest.mark.asyncio
    async def test_response_x_accel_buffering_is_no(self):
        """GET /v1/events returns X-Accel-Buffering: no."""
        payload = json.dumps({"content": "hello", "correlation_id": "c3"})
        registry = _make_terminating_registry([payload])
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            assert resp.headers.get("X-Accel-Buffering") == "no"


# ---------------------------------------------------------------------------
# Tests: data frames
# ---------------------------------------------------------------------------

class TestEventsHandlerData:
    @pytest.mark.asyncio
    async def test_payload_is_emitted_as_sse_data_frame(self):
        """Payloads from the queue are emitted as 'data: {payload}\\n\\n' frames."""
        payload = json.dumps({"content": "hello world", "correlation_id": "c4"})
        registry = _make_terminating_registry([payload])
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            body = await resp.text()
            assert f"data: {payload}" in body

    @pytest.mark.asyncio
    async def test_multiple_payloads_all_emitted(self):
        """Multiple queued payloads are all emitted in order."""
        payloads = [
            json.dumps({"content": f"msg-{i}", "correlation_id": f"c{i}"})
            for i in range(3)
        ]
        registry = _make_terminating_registry(payloads)
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            body = await resp.text()
            for p in payloads:
                assert f"data: {p}" in body

    @pytest.mark.asyncio
    async def test_sse_frame_ends_with_double_newline(self):
        """Each SSE data frame ends with \\n\\n."""
        payload = json.dumps({"content": "test"})
        registry = _make_terminating_registry([payload])
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            body = await resp.text()
            # The frame for this payload ends with \n\n
            assert f"data: {payload}\n\n" in body


# ---------------------------------------------------------------------------
# Tests: keepalive
# ---------------------------------------------------------------------------

class TestEventsHandlerKeepalive:
    @pytest.mark.asyncio
    async def test_timeout_emits_ping_keepalive(self):
        """Queue.get() timeout emits keepalive comment frame."""
        registry = _make_ping_then_terminate_registry()
        app = _build_app_with_events(push_registry=registry)

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            body = await resp.text()
            assert ": keepalive" in body


# ---------------------------------------------------------------------------
# Tests: subscribe / unsubscribe lifecycle
# ---------------------------------------------------------------------------

class TestEventsHandlerSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_called_with_user_id(self):
        """events_handler calls push_registry.subscribe(user_id)."""
        subscribe_calls: list = []
        payload = json.dumps({"content": "x"})

        registry = AsyncMock()
        call_count = 0

        async def _sub(uid):
            subscribe_calls.append(uid)
            call_count_inner = 0

            class _Q(asyncio.Queue):
                async def get(self):  # type: ignore[override]
                    nonlocal call_count_inner
                    call_count_inner += 1
                    if call_count_inner == 1:
                        return payload
                    raise asyncio.CancelledError()

            q = _Q(maxsize=256)
            return q

        registry.subscribe = _sub
        registry.unsubscribe = AsyncMock()

        user_record = _make_user_record("usr_events_test")
        app = _build_app_with_events(user_record=user_record, push_registry=registry)

        async with _test_client(app) as client:
            await client.get("/v1/events")

        assert "usr_events_test" in subscribe_calls

    @pytest.mark.asyncio
    async def test_unsubscribe_called_on_disconnect(self):
        """events_handler calls push_registry.unsubscribe() in finally block."""
        unsubscribe_calls: list = []
        payload = json.dumps({"content": "x"})

        registry = AsyncMock()

        async def _sub(uid):
            class _Q(asyncio.Queue):
                call_count = 0

                async def get(self):  # type: ignore[override]
                    self.call_count += 1
                    if self.call_count == 1:
                        return payload
                    raise asyncio.CancelledError()

            return _Q(maxsize=256)

        async def _unsub(uid, q):
            unsubscribe_calls.append((uid, q))

        registry.subscribe = _sub
        registry.unsubscribe = _unsub

        user_record = _make_user_record("usr_unsub_test")
        app = _build_app_with_events(user_record=user_record, push_registry=registry)

        async with _test_client(app) as client:
            await client.get("/v1/events")

        assert len(unsubscribe_calls) >= 1
        assert unsubscribe_calls[0][0] == "usr_unsub_test"


# ---------------------------------------------------------------------------
# Tests: auth integration
# ---------------------------------------------------------------------------

class TestEventsHandlerRequiresAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_request_returns_401(self):
        """GET /v1/events without a Bearer token returns 401.

        This test wires the events endpoint into create_app() which includes
        the real auth middleware, verifying end-to-end auth enforcement.
        """
        from aiguilleur.channels.rest.server import create_app
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        registry_mock = MagicMock()
        registry_mock.resolve_rest_api_key.return_value = None

        correlator = ResponseCorrelator()
        redis_conn = AsyncMock()
        config = {
            "cors_origins": ["*"],
            "request_timeout": 5,
            "include_traces": False,
        }
        adapter = MagicMock()
        adapter.config = MagicMock()
        adapter.config.profile_ref.profile = "default"
        adapter.config.prompt_path = None

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "aiguilleur.channels.rest.server.LongTermStore",
                MagicMock(return_value=AsyncMock()),
            )
            app = create_app(
                adapter=adapter,
                redis_conn=redis_conn,
                correlator=correlator,
                registry=registry_mock,
                config=config,
            )

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            assert resp.status == 401
