"""Unit tests for the REST channel adapter components.

TDD: Tests written BEFORE the implementation (RED phase).
Covers: ResponseCorrelator, SSE helpers, BearerAuthMiddleware.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _test_client(app):
    """Async context manager wrapping aiohttp TestClient."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _make_user_registry(valid_key: str = "valid-token"):
    """Build a UserRegistry-like mock that resolves one REST key."""
    from common.user_record import UserRecord

    record = UserRecord(
        user_id="usr_test",
        display_name="Test User",
        role="user",
        blocked=False,
        actions=[],
        skills_dirs=[],
        allowed_mcp_tools=[],
        allowed_subagents=[],
        prompt_path=None,
        role_prompt_path=None,
    )
    registry = MagicMock()
    registry.resolve_user.side_effect = lambda sender_id, channel: (
        record if sender_id == f"rest:{valid_key}" and channel == "rest" else None
    )
    return registry, record


# ---------------------------------------------------------------------------
# ResponseCorrelator
# ---------------------------------------------------------------------------

class TestResponseCorrelator:
    @pytest.mark.asyncio
    async def test_register_and_resolve(self):
        """register() returns a Future that resolve() fulfils with the envelope."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        envelope = MagicMock()
        fut = await correlator.register("corr-1")
        await correlator.resolve("corr-1", envelope)
        assert fut.done()
        assert fut.result() is envelope

    @pytest.mark.asyncio
    async def test_cancel_cleans_up(self):
        """cancel() cancels the Future and removes the key."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        fut = await correlator.register("corr-2")
        await correlator.cancel("corr-2")
        assert fut.cancelled()

    @pytest.mark.asyncio
    async def test_cancel_twice_no_exception(self):
        """Second cancel on same ID must not raise."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        await correlator.register("corr-3")
        await correlator.cancel("corr-3")
        await correlator.cancel("corr-3")  # no-op

    @pytest.mark.asyncio
    async def test_resolve_unknown_corr_id_no_exception(self):
        """resolve() on an unknown correlation_id must not raise."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        await correlator.resolve("nonexistent", MagicMock())

    @pytest.mark.asyncio
    async def test_cancel_unknown_corr_id_no_exception(self):
        """cancel() on an unknown correlation_id must not raise."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        await correlator.cancel("nonexistent")

    @pytest.mark.asyncio
    async def test_multiple_concurrent_registrations(self):
        """Multiple correlation IDs can be registered and resolved independently."""
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        correlator = ResponseCorrelator()
        env_a, env_b = MagicMock(), MagicMock()
        fut_a = await correlator.register("a")
        fut_b = await correlator.register("b")
        await correlator.resolve("b", env_b)
        await correlator.resolve("a", env_a)
        assert fut_a.result() is env_a
        assert fut_b.result() is env_b


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

class TestSSEHelpers:
    def test_format_sse_framing(self):
        """format_sse produces the correct SSE wire format."""
        from aiguilleur.channels.rest.sse import format_sse

        result = format_sse("token", "hello world")
        assert result == b"event: token\ndata: hello world\n\n"

    def test_format_sse_with_json_data(self):
        """format_sse handles JSON data strings correctly."""
        from aiguilleur.channels.rest.sse import format_sse

        data = json.dumps({"text": "hi"})
        result = format_sse("token", data)
        assert b"event: token\n" in result
        assert b'"text": "hi"' in result
        assert result.endswith(b"\n\n")

    def test_heartbeat_constant(self):
        """HEARTBEAT constant matches SSE keepalive spec."""
        from aiguilleur.channels.rest.sse import HEARTBEAT

        assert HEARTBEAT == b": keepalive\n\n"

    def test_format_sse_done_event(self):
        """format_sse handles the 'done' event type with empty data."""
        from aiguilleur.channels.rest.sse import format_sse

        result = format_sse("done", "")
        assert result == b"event: done\ndata: \n\n"

    def test_format_sse_error_event(self):
        """format_sse handles the 'error' event type."""
        from aiguilleur.channels.rest.sse import format_sse

        result = format_sse("error", "timeout")
        assert result == b"event: error\ndata: timeout\n\n"


# ---------------------------------------------------------------------------
# BearerAuthMiddleware
# ---------------------------------------------------------------------------

class TestBearerAuthMiddleware:
    @pytest.mark.asyncio
    async def test_missing_authorization_header_returns_401(self):
        """Request without Authorization header → 401."""
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiohttp import web

        registry, _ = _make_user_registry()
        middleware = make_bearer_auth_middleware(registry)

        async def handler(request):
            return web.Response(text="ok")

        app = web.Application(middlewares=[middleware])
        app.router.add_post("/v1/messages", handler)

        async with _test_client(app) as client:
            resp = await client.post("/v1/messages", json={"content": "hi"})
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_malformed_authorization_header_returns_401(self):
        """Authorization header without 'Bearer ' prefix → 401."""
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiohttp import web

        registry, _ = _make_user_registry()
        middleware = make_bearer_auth_middleware(registry)

        async def handler(request):
            return web.Response(text="ok")

        app = web.Application(middlewares=[middleware])
        app.router.add_post("/v1/messages", handler)

        async with _test_client(app) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"Authorization": "Basic abc123"},
                json={"content": "hi"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_unknown_token_returns_401(self):
        """Unknown Bearer token → 401."""
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiohttp import web

        registry, _ = _make_user_registry("valid-token")
        middleware = make_bearer_auth_middleware(registry)

        async def handler(request):
            return web.Response(text="ok")

        app = web.Application(middlewares=[middleware])
        app.router.add_post("/v1/messages", handler)

        async with _test_client(app) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer wrong-token"},
                json={"content": "hi"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_valid_token_sets_user_record_and_sender_id(self):
        """Valid Bearer token → handler sees request['user_record'] and request['sender_id']."""
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiohttp import web

        registry, record = _make_user_registry("valid-token")
        middleware = make_bearer_auth_middleware(registry)

        captured = {}

        async def handler(request):
            captured["user_record"] = request["user_record"]
            captured["sender_id"] = request["sender_id"]
            return web.Response(text="ok")

        app = web.Application(middlewares=[middleware])
        app.router.add_post("/v1/messages", handler)

        async with _test_client(app) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer valid-token"},
                json={"content": "hi"},
            )
            assert resp.status == 200
            assert captured["user_record"] is record
            assert captured["sender_id"] == "rest:usr_test"  # stable user_id, never the raw token

    @pytest.mark.asyncio
    async def test_bearer_token_never_logged(self, caplog):
        """The raw Bearer token must never appear in logs."""
        import logging
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiohttp import web

        registry, _ = _make_user_registry()
        middleware = make_bearer_auth_middleware(registry)

        async def handler(request):
            return web.Response(text="ok")

        app = web.Application(middlewares=[middleware])
        app.router.add_post("/v1/messages", handler)

        secret = "my-super-secret-token"
        with caplog.at_level(logging.DEBUG):
            async with _test_client(app) as client:
                await client.post(
                    "/v1/messages",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={"content": "hi"},
                )

        for record in caplog.records:
            assert secret not in record.getMessage()

    @pytest.mark.asyncio
    async def test_healthz_not_behind_auth_middleware(self):
        """/healthz must be on the root app (no auth middleware), not on /v1 sub-app.

        In create_app(), healthz_handler is registered directly on the root app.
        The auth middleware is only applied to the /v1 sub-app. This test
        verifies the correct architecture: healthz is accessible without a token.
        """
        from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
        from aiguilleur.channels.rest.server import healthz_handler
        from aiohttp import web

        registry, _ = _make_user_registry()
        auth_middleware = make_bearer_auth_middleware(registry)

        # Reflect real create_app() structure: root app has no auth,
        # auth middleware only on the /v1 sub-app.
        root = web.Application()
        root.router.add_get("/healthz", healthz_handler)
        api = web.Application(middlewares=[auth_middleware])
        root.add_subapp("/v1", api)

        async with _test_client(root) as client:
            resp = await client.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["channel"] == "rest"
