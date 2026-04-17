"""Integration tests for the REST channel adapter.

TDD: Tests written BEFORE the implementation (RED phase).
Uses aiohttp TestClient + fakeredis.aioredis to avoid real Redis.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from common.user_record import UserRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user_record(user_id: str = "usr_admin", blocked: bool = False) -> UserRecord:
    """Build a test UserRecord."""
    return UserRecord(
        user_id=user_id,
        display_name="Admin",
        role="admin",
        blocked=blocked,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        allowed_subagents=["*"],
        prompt_path=None,
    )


def _make_registry(user_record: UserRecord | None = None) -> MagicMock:
    """Build a mock UserRegistry."""
    registry = MagicMock()
    if user_record is None:
        user_record = _make_user_record()
    registry.resolve_rest_api_key.return_value = user_record
    return registry


@pytest.fixture
def valid_token():
    """A fixed bearer token used in integration tests."""
    return "test-api-key-integration"


@pytest.fixture
def user_record():
    """Default user record for integration tests."""
    return _make_user_record()


@pytest.fixture
def registry(user_record):
    """Mock registry returning the default user record."""
    return _make_registry(user_record)


@pytest.fixture
def correlator():
    """Real ResponseCorrelator for integration tests."""
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    return ResponseCorrelator()


@pytest.fixture
def fake_redis():
    """Fake async Redis connection."""
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def adapter_mock():
    """Mock RestAiguilleur adapter."""
    from aiguilleur.channel_config import ChannelConfig

    config = ChannelConfig(
        name="rest",
        enabled=True,
        streaming=True,
        extras={
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 5,
            "cors_origins": ["*"],
            "include_traces": False,
        },
    )
    adapter = MagicMock()
    adapter.config = config
    return adapter


@pytest_asyncio.fixture
async def test_client(adapter_mock, fake_redis, correlator, registry):
    """Build an aiohttp TestClient wrapping the REST app."""
    from aiohttp.test_utils import TestClient, TestServer
    from aiguilleur.channels.rest.server import create_app

    config = {
        "bind": "127.0.0.1",
        "port": 8080,
        "request_timeout": 2,
        "cors_origins": ["*"],
        "include_traces": False,
    }
    app = create_app(
        adapter=adapter_mock,
        redis_conn=fake_redis,
        correlator=correlator,
        registry=registry,
        config=config,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# /openapi.json and /docs
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOpenApi:
    @pytest.mark.asyncio
    async def test_openapi_json_returns_200(self, test_client):
        """GET /openapi.json → 200 without auth."""
        resp = await test_client.get("/openapi.json")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_openapi_json_content_type(self, test_client):
        """GET /openapi.json → Content-Type: application/json."""
        resp = await test_client.get("/openapi.json")
        assert "application/json" in resp.headers.get("Content-Type", "")

    @pytest.mark.asyncio
    async def test_openapi_json_structure(self, test_client):
        """GET /openapi.json → valid OpenAPI 3.0 envelope."""
        resp = await test_client.get("/openapi.json")
        data = await resp.json()
        assert data["openapi"].startswith("3.")
        assert "info" in data
        assert "paths" in data
        assert "/messages" in data["paths"]

    @pytest.mark.asyncio
    async def test_openapi_no_auth_required(self, test_client):
        """GET /openapi.json must not require Authorization header."""
        resp = await test_client.get("/openapi.json")
        assert resp.status != 401

    @pytest.mark.asyncio
    async def test_docs_returns_200(self, test_client):
        """GET /docs → 200 without auth."""
        resp = await test_client.get("/docs")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_docs_content_type_html(self, test_client):
        """GET /docs → Content-Type: text/html."""
        resp = await test_client.get("/docs")
        assert "text/html" in resp.headers.get("Content-Type", "")

    @pytest.mark.asyncio
    async def test_docs_contains_swagger_ui(self, test_client):
        """GET /docs → HTML page references swagger-ui."""
        resp = await test_client.get("/docs")
        text = await resp.text()
        assert "swagger-ui" in text.lower()
        assert "/openapi.json" in text


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestHealthz:
    @pytest.mark.asyncio
    async def test_healthz_returns_200(self, test_client):
        """GET /healthz → 200 {"status": "ok", "channel": "rest"} without auth."""
        resp = await test_client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["channel"] == "rest"

    @pytest.mark.asyncio
    async def test_healthz_no_auth_required(self, test_client):
        """Healthz must not require Authorization header."""
        resp = await test_client.get("/healthz")
        # If auth was required this would be 401
        assert resp.status != 401


# ---------------------------------------------------------------------------
# POST /v1/messages — auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMessageAuth:
    @pytest.mark.asyncio
    async def test_post_message_auth_fail_missing_token(self, test_client):
        """No Bearer token → 401."""
        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_post_message_auth_fail_bad_token(self, test_client, registry):
        """Unknown token → 401."""
        registry.resolve_rest_api_key.return_value = None
        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello"},
            headers={"Authorization": "Bearer bad-token"},
        )
        assert resp.status == 401


# ---------------------------------------------------------------------------
# POST /v1/messages — validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMessageValidation:
    @pytest.mark.asyncio
    async def test_post_message_missing_content(self, test_client, valid_token):
        """Body without 'content' field → 400."""
        resp = await test_client.post(
            "/v1/messages",
            json={"session_id": "some-session"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_empty_content(self, test_client, valid_token):
        """Body with empty content string → 400."""
        resp = await test_client.post(
            "/v1/messages",
            json={"content": ""},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_invalid_json(self, test_client, valid_token):
        """Malformed JSON body → 400."""
        from aiohttp import ClientSession

        resp = await test_client.post(
            "/v1/messages",
            data="not-json",
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_content_too_large(self, test_client, valid_token):
        """Content exceeding 32 KB → 413."""
        oversized = "x" * (32_768 + 1)
        resp = await test_client.post(
            "/v1/messages",
            json={"content": oversized},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 413

    @pytest.mark.asyncio
    async def test_post_message_content_exactly_32kb_accepted(self, test_client, valid_token, correlator, fake_redis):
        """Content at exactly 32 KB boundary is accepted (boundary is inclusive at limit)."""
        # 32768 ASCII bytes is exactly at the limit — must not 413
        boundary_content = "x" * 32_768
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    reply = Envelope.from_parent(env, "ok")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": boundary_content},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_post_message_invalid_session_id_rejected(self, test_client, valid_token):
        """session_id with invalid characters → 400."""
        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello", "session_id": "invalid session id!"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_session_id_too_long_rejected(self, test_client, valid_token):
        """session_id exceeding 64 characters → 400."""
        long_session_id = "a" * 65
        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello", "session_id": long_session_id},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_valid_session_id_with_dashes_and_underscores(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """session_id with dashes and underscores is valid."""
        valid_session_id = "my_session-id_123"
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    reply = Envelope.from_parent(env, "ok")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello", "session_id": valid_session_id},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /v1/messages — session_id handling
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMessageSessionId:
    @pytest.mark.asyncio
    async def test_post_message_session_id_provided(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """Client-provided session_id is used in the published envelope."""
        client_session_id = "client-session-abc-123"
        corr_id_holder: list[str] = []

        # Intercept the xadd to resolve the future ourselves
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    corr_id_holder.append(env.correlation_id)
                    assert env.session_id == client_session_id
                    # Simulate the outgoing reply
                    reply = Envelope.from_parent(env, "reply content")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello", "session_id": client_session_id},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["session_id"] == client_session_id

    @pytest.mark.asyncio
    async def test_post_message_session_id_auto_generated(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """When session_id is absent, an auto-generated UUID is used."""
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    # Must be a valid UUID4
                    try:
                        uuid.UUID(env.session_id, version=4)
                    except ValueError:
                        pytest.fail(f"auto-generated session_id is not a UUID4: {env.session_id}")
                    # Resolve future
                    reply = Envelope.from_parent(env, "reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        # session_id in response must be a valid UUID4
        try:
            uuid.UUID(data["session_id"], version=4)
        except (ValueError, KeyError):
            pytest.fail(f"Response session_id is not a UUID4: {data.get('session_id')}")


# ---------------------------------------------------------------------------
# POST /v1/messages — happy path & response shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMessageHappyPath:
    @pytest.mark.asyncio
    async def test_post_message_classic_happy_path(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """Full round-trip: client sends → adapter publishes → fake reply → 200."""
        reply_content = "Hello from RELAIS!"
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    reply = Envelope.from_parent(env, reply_content)
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "Hello RELAIS"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["content"] == reply_content
        assert "correlation_id" in data
        assert "session_id" in data

    @pytest.mark.asyncio
    async def test_post_message_include_traces_false(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """When include_traces=False, the response must NOT have a 'traces' key."""
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    reply = Envelope.from_parent(env, "reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "test traces"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "traces" not in data

    @pytest.mark.asyncio
    async def test_post_message_include_traces_true(
        self, adapter_mock, fake_redis, correlator, registry, valid_token
    ):
        """When include_traces=True, the response MUST include a 'traces' key."""
        from aiohttp.test_utils import TestClient, TestServer
        from aiguilleur.channels.rest.server import create_app
        from aiguilleur.channel_config import ChannelConfig

        config_with_traces = {
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 2,
            "cors_origins": ["*"],
            "include_traces": True,  # <-- traces enabled
        }

        app = create_app(
            adapter=adapter_mock,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config=config_with_traces,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            original_xadd = fake_redis.xadd

            async def capturing_xadd(stream, fields, *args, **kwargs):
                result = await original_xadd(stream, fields, *args, **kwargs)
                stream_str = stream if isinstance(stream, str) else stream.decode()
                if "relais:messages:incoming" in stream_str:
                    payload = fields.get(b"payload") or fields.get("payload")
                    if payload:
                        env = Envelope.from_json(
                            payload if isinstance(payload, str) else payload.decode()
                        )
                        reply = Envelope.from_parent(env, "reply with traces")
                        reply.add_trace("rest", "outgoing")
                        reply.action = ACTION_MESSAGE_OUTGOING
                        await correlator.resolve(env.correlation_id, reply)
                return result

            fake_redis.xadd = capturing_xadd

            resp = await client.post(
                "/v1/messages",
                json={"content": "traces test"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "traces" in data
            assert isinstance(data["traces"], list)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# POST /v1/messages — timeout
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMessageTimeout:
    @pytest.mark.asyncio
    async def test_post_message_classic_timeout(
        self, adapter_mock, fake_redis, correlator, registry, valid_token
    ):
        """When no outgoing reply arrives within request_timeout → 504."""
        from aiohttp.test_utils import TestClient, TestServer
        from aiguilleur.channels.rest.server import create_app

        config_short_timeout = {
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 0.1,  # 100ms — triggers timeout fast
            "cors_origins": ["*"],
            "include_traces": False,
        }

        app = create_app(
            adapter=adapter_mock,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config=config_short_timeout,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            # No one resolves the future → times out
            resp = await client.post(
                "/v1/messages",
                json={"content": "will timeout"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert resp.status == 504
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Envelope correctness
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEnvelopeCorrectness:
    @pytest.mark.asyncio
    async def test_envelope_channel_and_action(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """Published envelope must have channel='rest' and action=ACTION_MESSAGE_INCOMING."""
        from common.envelope_actions import ACTION_MESSAGE_INCOMING

        captured: list[Envelope] = []
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    captured.append(env)
                    reply = Envelope.from_parent(env, "reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        await test_client.post(
            "/v1/messages",
            json={"content": "correctness check"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )

        assert len(captured) == 1
        env = captured[0]
        assert env.channel == "rest"
        assert env.action == ACTION_MESSAGE_INCOMING

    @pytest.mark.asyncio
    async def test_envelope_aiguilleur_ctx_stamped(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """Published envelope must have aiguilleur context stamped correctly."""
        from common.contexts import CTX_AIGUILLEUR

        captured: list[Envelope] = []
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    captured.append(env)
                    reply = Envelope.from_parent(env, "reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        await test_client.post(
            "/v1/messages",
            json={"content": "ctx check"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )

        assert len(captured) == 1
        ctx = captured[0].context.get(CTX_AIGUILLEUR, {})
        assert ctx.get("streaming") is False
        assert ctx.get("content_type") == "text"
        assert "reply_to" in ctx
        assert "correlation_id" in ctx


# ---------------------------------------------------------------------------
# CORS / OPTIONS preflight
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCorsOptions:
    @pytest.mark.asyncio
    async def test_options_preflight_returns_204(self, test_client):
        """OPTIONS request → 204 with CORS headers (no auth required)."""
        resp = await test_client.options(
            "/v1/messages",
            headers={"Origin": "https://example.com"},
        )
        assert resp.status == 204
        assert "Access-Control-Allow-Origin" in resp.headers
        assert "Access-Control-Allow-Methods" in resp.headers

    @pytest.mark.asyncio
    async def test_options_preflight_cors_whitelist_allowed_origin(
        self, adapter_mock, fake_redis, correlator, registry
    ):
        """OPTIONS preflight with a whitelisted origin returns that origin, not '*'."""
        from aiohttp.test_utils import TestClient, TestServer
        from aiguilleur.channels.rest.server import create_app

        config_whitelist = {
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 2,
            "cors_origins": ["https://app.example.com", "https://other.example.com"],
            "include_traces": False,
        }
        app = create_app(
            adapter=adapter_mock,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config=config_whitelist,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.options(
                "/v1/messages",
                headers={"Origin": "https://app.example.com"},
            )
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://app.example.com"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_options_preflight_cors_whitelist_rejected_origin(
        self, adapter_mock, fake_redis, correlator, registry
    ):
        """OPTIONS preflight from an unlisted origin must NOT set Access-Control-Allow-Origin."""
        from aiohttp.test_utils import TestClient, TestServer
        from aiguilleur.channels.rest.server import create_app

        config_whitelist = {
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 2,
            "cors_origins": ["https://app.example.com"],
            "include_traces": False,
        }
        app = create_app(
            adapter=adapter_mock,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config=config_whitelist,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.options(
                "/v1/messages",
                headers={"Origin": "https://evil.com"},
            )
            assert resp.status == 204
            # The rejected origin must not appear in the header
            assert resp.headers.get("Access-Control-Allow-Origin") != "https://evil.com"
            assert resp.headers.get("Access-Control-Allow-Origin") is None
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_cors_header_on_response(self, test_client, valid_token, correlator, fake_redis):
        """Non-OPTIONS requests get Access-Control-Allow-Origin header in response."""
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    reply = Envelope.from_parent(env, "cors reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "cors test"},
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Origin": "https://example.com",
            },
        )
        assert resp.status == 200
        assert "Access-Control-Allow-Origin" in resp.headers


# ---------------------------------------------------------------------------
# SSE streaming mode
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSseStreaming:
    @pytest.mark.asyncio
    async def test_adapter_config_exception_falls_back_gracefully(
        self, fake_redis, correlator, registry, valid_token
    ):
        """If adapter.config raises on profile access, envelope still publishes correctly."""
        from aiohttp.test_utils import TestClient, TestServer
        from aiguilleur.channels.rest.server import create_app
        from aiguilleur.channel_config import ChannelConfig

        # Build an adapter mock whose .config.profile_ref raises AttributeError
        config = ChannelConfig(
            name="rest",
            enabled=True,
            streaming=True,
            extras={
                "bind": "127.0.0.1",
                "port": 8080,
                "request_timeout": 2,
                "cors_origins": ["*"],
                "include_traces": False,
            },
        )
        bad_adapter = MagicMock()
        bad_adapter.config = config  # real ChannelConfig without profile_ref attr

        app = create_app(
            adapter=bad_adapter,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config={
                "bind": "127.0.0.1",
                "port": 8080,
                "request_timeout": 2,
                "cors_origins": ["*"],
                "include_traces": False,
            },
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            original_xadd = fake_redis.xadd

            async def capturing_xadd(stream, fields, *args, **kwargs):
                result = await original_xadd(stream, fields, *args, **kwargs)
                stream_str = stream if isinstance(stream, str) else stream.decode()
                if "relais:messages:incoming" in stream_str:
                    payload = fields.get(b"payload") or fields.get("payload")
                    if payload:
                        env = Envelope.from_json(
                            payload if isinstance(payload, str) else payload.decode()
                        )
                        reply = Envelope.from_parent(env, "fallback reply")
                        reply.action = ACTION_MESSAGE_OUTGOING
                        await correlator.resolve(env.correlation_id, reply)
                return result

            fake_redis.xadd = capturing_xadd

            resp = await client.post(
                "/v1/messages",
                json={"content": "config exception test"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            # Should still succeed — config access exception is silently caught
            assert resp.status == 200
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_sse_envelope_has_streaming_true(
        self, test_client, valid_token, correlator, fake_redis
    ):
        """When Accept: text/event-stream, the published envelope has streaming=True."""
        from common.contexts import CTX_AIGUILLEUR

        captured: list[Envelope] = []
        original_xadd = fake_redis.xadd

        async def capturing_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    captured.append(env)
                    # Resolve so the SSE handler can finish
                    reply = Envelope.from_parent(env, "sse reply")
                    reply.action = ACTION_MESSAGE_OUTGOING
                    await correlator.resolve(env.correlation_id, reply)
            return result

        fake_redis.xadd = capturing_xadd

        # SSE mode is triggered by Accept: text/event-stream
        resp = await test_client.post(
            "/v1/messages",
            json={"content": "sse check"},
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Accept": "text/event-stream",
            },
        )
        # SSE response is a streaming response — status may be 200 or connection close
        # The key assertion is that the envelope was captured with streaming=True
        assert len(captured) == 1
        ctx = captured[0].context.get(CTX_AIGUILLEUR, {})
        assert ctx.get("streaming") is True
