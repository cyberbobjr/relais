"""Tests for Phase 3: mirror outgoing replies into the user push stream,
and Phase 2 wiring: /v1/events registered in create_app + PushRegistry in adapter.

TDD: Tests written BEFORE implementation (RED phase).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user_record(user_id: str = "usr_test") -> MagicMock:
    record = MagicMock()
    record.user_id = user_id
    record.blocked = False
    record.display_name = "Test User"
    record.role = "user"
    record.actions = []
    record.skills_dirs = []
    record.allowed_mcp_tools = []
    record.allowed_subagents = []
    record.prompt_path = None
    record.role_prompt_path = None
    return record


def _make_registry(user_record=None):
    """Build a UserRegistry mock."""
    record = user_record or _make_user_record()
    registry = MagicMock()
    registry.resolve_rest_api_key.return_value = record
    return registry, record


@asynccontextmanager
async def _test_client(app: web.Application):
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _build_full_app(push_registry=None, extra_events_registry=None):
    """Build a full app via create_app() with an optional push_registry injected.

    Args:
        push_registry: If provided, injected as api_app["_push_registry"].

    Returns:
        web.Application ready for test.
    """
    from aiguilleur.channels.rest.server import create_app
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    registry_mock, record = _make_registry()
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

    if push_registry is not None:
        # Inject into the /v1 sub-application
        # create_app wires the sub-app last via add_subapp
        # We patch after-the-fact via frozen_app._subapp
        for resource in app.router.resources():
            pass
        # Set on the router's sub-apps through app's _subapp list
        # The easiest approach: monkey-patch after creation
        app["_test_push_registry"] = push_registry  # marker for verification

    return app, registry_mock, record, redis_conn


# ---------------------------------------------------------------------------
# Phase 2, Step 4 — Verify /v1/events is registered in create_app
# ---------------------------------------------------------------------------

class TestServerEventsRouteRegistered:
    @pytest.mark.asyncio
    async def test_events_endpoint_exists_and_returns_401_without_token(self):
        """GET /v1/events is a registered route; without auth it returns 401."""
        app, _, _, _ = _build_full_app()

        async with _test_client(app) as client:
            resp = await client.get("/v1/events")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_events_endpoint_returns_200_with_valid_token(self):
        """GET /v1/events with a valid token returns 200 (streaming starts)."""
        from aiguilleur.channels.rest.server import create_app
        from aiguilleur.channels.rest.correlator import ResponseCorrelator

        # Build a terminating push_registry to allow the handler to exit
        class _TermQueue(asyncio.Queue):
            call_count = 0

            async def get(self):  # type: ignore[override]
                self.call_count += 1
                raise asyncio.CancelledError()

        push_registry = AsyncMock()
        push_registry.subscribe = AsyncMock(return_value=_TermQueue(maxsize=256))
        push_registry.unsubscribe = AsyncMock()

        registry_mock, record = _make_registry()
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
                push_registry=push_registry,
            )

        async with _test_client(app) as client:
            resp = await client.get(
                "/v1/events",
                headers={"Authorization": "Bearer valid-token"},
            )
            assert resp.status == 200
            assert "text/event-stream" in resp.headers.get("Content-Type", "")


# ---------------------------------------------------------------------------
# Phase 3, Step 5 — _sender_to_user_id helper
# ---------------------------------------------------------------------------

class TestSenderToUserId:
    def test_strips_rest_prefix(self):
        """_sender_to_user_id('rest:usr_admin') returns 'usr_admin'."""
        from aiguilleur.channels.rest.adapter import _sender_to_user_id

        assert _sender_to_user_id("rest:usr_admin") == "usr_admin"

    def test_strips_rest_prefix_with_colon_in_id(self):
        """_sender_to_user_id handles IDs with additional colons."""
        from aiguilleur.channels.rest.adapter import _sender_to_user_id

        assert _sender_to_user_id("rest:usr_a:b") == "usr_a:b"

    def test_non_rest_prefix_returns_none(self):
        """_sender_to_user_id returns None for non-rest sender IDs."""
        from aiguilleur.channels.rest.adapter import _sender_to_user_id

        assert _sender_to_user_id("discord:usr_test") is None

    def test_bare_rest_prefix_returns_empty_string(self):
        """_sender_to_user_id('rest:') returns empty string (falsy but not None)."""
        from aiguilleur.channels.rest.adapter import _sender_to_user_id

        result = _sender_to_user_id("rest:")
        assert result == ""

    def test_empty_string_returns_none(self):
        """_sender_to_user_id('') returns None."""
        from aiguilleur.channels.rest.adapter import _sender_to_user_id

        assert _sender_to_user_id("") is None


# ---------------------------------------------------------------------------
# Phase 3, Step 5 — Mirror XADD in _handle_outgoing_message
# ---------------------------------------------------------------------------

class TestHandleOutgoingMessageMirror:
    @pytest.mark.asyncio
    async def test_xadd_mirror_called_for_rest_sender(self):
        """_handle_outgoing_message XADDs to user push stream for rest: sender."""
        from aiguilleur.channels.rest.adapter import RestAiguilleur
        from aiguilleur.channels.rest.correlator import ResponseCorrelator
        from common.envelope import Envelope
        from common.envelope_actions import ACTION_MESSAGE_OUTGOING
        from common.streams import stream_outgoing_user

        # Build a minimal envelope with a rest: sender_id
        envelope = Envelope(
            content="hello",
            sender_id="rest:usr_mirror",
            channel="rest",
            session_id="sess-1",
            correlation_id="corr-mirror-1",
            action=ACTION_MESSAGE_OUTGOING,
        )
        raw_json = envelope.to_json()

        redis_conn = AsyncMock()
        correlator = ResponseCorrelator()

        # Build a minimal RestAiguilleur without real config/redis
        config = MagicMock()
        config.extras = {}
        config.profile_ref = MagicMock()
        config.profile_ref.profile = "default"
        config.prompt_path = None

        with patch("aiguilleur.channels.rest.adapter.RedisClient"):
            adapter = RestAiguilleur.__new__(RestAiguilleur)
            adapter._bind = "127.0.0.1"
            adapter._port = 8080
            adapter._request_timeout = 30.0
            adapter._cors_origins = ["*"]
            adapter._include_traces = False
            adapter._stop_event = MagicMock()
            adapter.config = config

        data = {"payload": raw_json.encode()}
        message_id = "1-0"
        stream = "relais:messages:outgoing:rest"

        await adapter._handle_outgoing_message(
            data, message_id, redis_conn, correlator, stream
        )

        expected_stream = stream_outgoing_user("rest", "usr_mirror")

        # Verify XADD was called with the user push stream
        xadd_calls = redis_conn.xadd.call_args_list
        assert len(xadd_calls) >= 1
        call_args = xadd_calls[0]
        assert call_args[0][0] == expected_stream

    @pytest.mark.asyncio
    async def test_xadd_mirror_payload_contains_raw_json(self):
        """Mirror XADD payload field contains the original raw JSON."""
        from aiguilleur.channels.rest.adapter import RestAiguilleur
        from aiguilleur.channels.rest.correlator import ResponseCorrelator
        from common.envelope import Envelope
        from common.envelope_actions import ACTION_MESSAGE_OUTGOING

        envelope = Envelope(
            content="mirrored content",
            sender_id="rest:usr_payload_check",
            channel="rest",
            session_id="sess-2",
            correlation_id="corr-payload-1",
            action=ACTION_MESSAGE_OUTGOING,
        )
        raw_json = envelope.to_json()

        redis_conn = AsyncMock()
        correlator = ResponseCorrelator()

        config = MagicMock()
        config.extras = {}
        config.profile_ref = MagicMock()
        config.profile_ref.profile = "default"
        config.prompt_path = None

        with patch("aiguilleur.channels.rest.adapter.RedisClient"):
            adapter = RestAiguilleur.__new__(RestAiguilleur)
            adapter._bind = "127.0.0.1"
            adapter._port = 8080
            adapter._request_timeout = 30.0
            adapter._cors_origins = ["*"]
            adapter._include_traces = False
            adapter._stop_event = MagicMock()
            adapter.config = config

        data = {"payload": raw_json.encode()}
        await adapter._handle_outgoing_message(
            data, "1-0", redis_conn, correlator, "relais:messages:outgoing:rest"
        )

        xadd_calls = redis_conn.xadd.call_args_list
        assert len(xadd_calls) >= 1
        call_fields = xadd_calls[0][0][1]  # second positional arg = fields dict
        assert "payload" in call_fields
        assert call_fields["payload"] == raw_json

    @pytest.mark.asyncio
    async def test_xadd_mirror_uses_maxlen(self):
        """Mirror XADD uses MAXLEN ~100 to cap stream size."""
        from aiguilleur.channels.rest.adapter import RestAiguilleur
        from aiguilleur.channels.rest.correlator import ResponseCorrelator
        from common.envelope import Envelope
        from common.envelope_actions import ACTION_MESSAGE_OUTGOING

        envelope = Envelope(
            content="maxlen check",
            sender_id="rest:usr_maxlen",
            channel="rest",
            session_id="sess-3",
            correlation_id="corr-maxlen-1",
            action=ACTION_MESSAGE_OUTGOING,
        )
        raw_json = envelope.to_json()
        redis_conn = AsyncMock()
        correlator = ResponseCorrelator()

        config = MagicMock()
        config.extras = {}
        config.profile_ref = MagicMock()
        config.profile_ref.profile = "default"
        config.prompt_path = None

        with patch("aiguilleur.channels.rest.adapter.RedisClient"):
            adapter = RestAiguilleur.__new__(RestAiguilleur)
            adapter._bind = "127.0.0.1"
            adapter._port = 8080
            adapter._request_timeout = 30.0
            adapter._cors_origins = ["*"]
            adapter._include_traces = False
            adapter._stop_event = MagicMock()
            adapter.config = config

        data = {"payload": raw_json.encode()}
        await adapter._handle_outgoing_message(
            data, "1-0", redis_conn, correlator, "relais:messages:outgoing:rest"
        )

        xadd_calls = redis_conn.xadd.call_args_list
        assert len(xadd_calls) >= 1
        kwargs = xadd_calls[0][1]  # keyword arguments
        # maxlen should be specified (approx ~100)
        assert "maxlen" in kwargs

    @pytest.mark.asyncio
    async def test_xadd_mirror_not_called_for_non_rest_sender(self):
        """Mirror XADD is NOT called for non-rest sender IDs (e.g. discord:)."""
        from aiguilleur.channels.rest.adapter import RestAiguilleur
        from aiguilleur.channels.rest.correlator import ResponseCorrelator
        from common.envelope import Envelope
        from common.envelope_actions import ACTION_MESSAGE_OUTGOING

        envelope = Envelope(
            content="discord message",
            sender_id="discord:user123",
            channel="discord",
            session_id="sess-4",
            correlation_id="corr-discord-1",
            action=ACTION_MESSAGE_OUTGOING,
        )
        raw_json = envelope.to_json()
        redis_conn = AsyncMock()
        correlator = ResponseCorrelator()

        config = MagicMock()
        config.extras = {}
        config.profile_ref = MagicMock()
        config.profile_ref.profile = "default"
        config.prompt_path = None

        with patch("aiguilleur.channels.rest.adapter.RedisClient"):
            adapter = RestAiguilleur.__new__(RestAiguilleur)
            adapter._bind = "127.0.0.1"
            adapter._port = 8080
            adapter._request_timeout = 30.0
            adapter._cors_origins = ["*"]
            adapter._include_traces = False
            adapter._stop_event = MagicMock()
            adapter.config = config

        data = {"payload": raw_json.encode()}
        await adapter._handle_outgoing_message(
            data, "1-0", redis_conn, correlator, "relais:messages:outgoing:rest"
        )

        # xadd should NOT have been called (no mirror for non-rest)
        redis_conn.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_xadd_mirror_not_called_for_progress_action(self):
        """Mirror XADD is NOT called for ACTION_MESSAGE_PROGRESS envelopes."""
        from aiguilleur.channels.rest.adapter import RestAiguilleur
        from aiguilleur.channels.rest.correlator import ResponseCorrelator
        from common.envelope import Envelope
        from common.envelope_actions import ACTION_MESSAGE_PROGRESS

        envelope = Envelope(
            content="progress...",
            sender_id="rest:usr_progress",
            channel="rest",
            session_id="sess-5",
            correlation_id="corr-progress-1",
            action=ACTION_MESSAGE_PROGRESS,
        )
        raw_json = envelope.to_json()
        redis_conn = AsyncMock()
        correlator = ResponseCorrelator()

        config = MagicMock()
        config.extras = {}

        with patch("aiguilleur.channels.rest.adapter.RedisClient"):
            adapter = RestAiguilleur.__new__(RestAiguilleur)
            adapter._bind = "127.0.0.1"
            adapter._port = 8080
            adapter._request_timeout = 30.0
            adapter._cors_origins = ["*"]
            adapter._include_traces = False
            adapter._stop_event = MagicMock()
            adapter.config = config

        data = {"payload": raw_json.encode()}
        await adapter._handle_outgoing_message(
            data, "1-0", redis_conn, correlator, "relais:messages:outgoing:rest"
        )

        # Progress events are early-returned before mirror
        redis_conn.xadd.assert_not_called()
