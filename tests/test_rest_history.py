"""Tests for GET /v1/history endpoint in the REST channel adapter.

TDD: Tests written BEFORE the implementation (RED phase).
Covers: missing session_id, happy path, empty session, limit cap, invalid limit,
and authentication enforcement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from common.user_record import UserRecord


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_user_record(user_id: str = "usr_test") -> UserRecord:
    """Build a minimal UserRecord for auth purposes."""
    return UserRecord(
        user_id=user_id,
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


def _make_registry(valid_token: str = "valid-token") -> MagicMock:
    """Return a mock UserRegistry that resolves a single fixed REST key."""
    record = _make_user_record()
    registry = MagicMock()
    registry.resolve_rest_api_key.side_effect = lambda raw: (
        record if raw == valid_token else None
    )
    return registry


def _make_mock_store(turns: list[dict] | None = None) -> AsyncMock:
    """Return a mock LongTermStore with get_session_history pre-configured."""
    store = AsyncMock()
    store.get_session_history.return_value = turns if turns is not None else []
    store.close = AsyncMock()
    return store


# We need a fake adapter (RestAiguilleur mock) so create_app() doesn't break.
def _make_adapter_mock() -> MagicMock:
    from aiguilleur.channel_config import ChannelConfig

    config = ChannelConfig(
        name="rest",
        enabled=True,
        streaming=False,
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
async def history_client(request):
    """Build a TestClient for the REST app, with a mock LongTermStore injected.

    Accepts an indirect parameter ``turns`` (list[dict]) that configures the
    mock store's return value. When the fixture is used without indirect
    parameterisation the store returns an empty list.
    """
    from aiohttp.test_utils import TestClient, TestServer
    import fakeredis.aioredis
    from aiguilleur.channels.rest.correlator import ResponseCorrelator
    from aiguilleur.channels.rest.server import create_app

    turns = getattr(request, "param", [])
    mock_store = _make_mock_store(turns)

    registry = _make_registry("test-key")
    fake_redis = fakeredis.aioredis.FakeRedis()
    correlator = ResponseCorrelator()
    adapter = _make_adapter_mock()

    config = {
        "bind": "127.0.0.1",
        "port": 8080,
        "request_timeout": 2,
        "cors_origins": ["*"],
        "include_traces": False,
    }

    with patch(
        "aiguilleur.channels.rest.server.LongTermStore", return_value=mock_store
    ):
        app = create_app(
            adapter=adapter,
            redis_conn=fake_redis,
            correlator=correlator,
            registry=registry,
            config=config,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        # Attach mock_store to client for assertion access in tests
        client._mock_store = mock_store  # type: ignore[attr-defined]
        yield client
        await client.close()


_AUTH = {"Authorization": "Bearer test-key"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGetHistory:
    @pytest.mark.asyncio
    async def test_get_history_missing_session_id(self, history_client):
        """GET /v1/history without session_id → 400 with error message."""
        resp = await history_client.get("/v1/history", headers=_AUTH)
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data
        assert "session_id" in data["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "history_client",
        [
            [
                {
                    "user_content": "hello",
                    "assistant_content": "hi there",
                    "created_at": 1_700_000_000.0,
                    "correlation_id": "corr-1",
                },
                {
                    "user_content": "how are you?",
                    "assistant_content": "I am fine",
                    "created_at": 1_700_000_001.0,
                    "correlation_id": "corr-2",
                },
            ]
        ],
        indirect=True,
    )
    async def test_get_history_returns_turns(self, history_client):
        """GET /v1/history?session_id=s1 → 200 with session_id and 2 turns."""
        resp = await history_client.get(
            "/v1/history?session_id=my-session", headers=_AUTH
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["session_id"] == "my-session"
        assert len(data["turns"]) == 2
        assert data["turns"][0]["user_content"] == "hello"
        assert data["turns"][1]["user_content"] == "how are you?"

    @pytest.mark.asyncio
    async def test_get_history_empty_session(self, history_client):
        """GET /v1/history?session_id=empty with auth → 404 (session not found for owner).

        When an authenticated user requests a session with no matching turns,
        the endpoint returns 404 to avoid leaking the existence of session IDs
        belonging to other users.
        """
        # history_client default fixture param returns []
        resp = await history_client.get(
            "/v1/history?session_id=empty-session", headers=_AUTH
        )
        assert resp.status == 404
        data = await resp.json()
        assert "error" in data

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "history_client",
        [[{"user_content": "q", "assistant_content": "a", "created_at": 1.0, "correlation_id": "c"}]],
        indirect=True,
    )
    async def test_get_history_limit_capped_at_200(self, history_client):
        """limit=500 in query → store is called with limit=200 (capped)."""
        resp = await history_client.get(
            "/v1/history?session_id=s1&limit=500", headers=_AUTH
        )
        assert resp.status == 200
        store: AsyncMock = history_client._mock_store  # type: ignore[attr-defined]
        store.get_session_history.assert_called_once_with("s1", 200, user_id="usr_test")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "history_client",
        [[{"user_content": "q", "assistant_content": "a", "created_at": 1.0, "correlation_id": "c"}]],
        indirect=True,
    )
    async def test_get_history_invalid_limit_defaults_to_50(self, history_client):
        """limit=abc in query → ValueError caught, store called with limit=50."""
        resp = await history_client.get(
            "/v1/history?session_id=s1&limit=abc", headers=_AUTH
        )
        assert resp.status == 200
        store: AsyncMock = history_client._mock_store  # type: ignore[attr-defined]
        store.get_session_history.assert_called_once_with("s1", 50, user_id="usr_test")

    @pytest.mark.asyncio
    async def test_get_history_requires_auth(self, history_client):
        """GET /v1/history without Authorization header → 401."""
        resp = await history_client.get("/v1/history?session_id=s1")
        assert resp.status == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "history_client",
        [[{"user_content": "q", "assistant_content": "a", "created_at": 1.0, "correlation_id": "c"}]],
        indirect=True,
    )
    async def test_get_history_default_limit_is_50(self, history_client):
        """GET /v1/history without limit param → store called with limit=50."""
        resp = await history_client.get(
            "/v1/history?session_id=s1", headers=_AUTH
        )
        assert resp.status == 200
        store: AsyncMock = history_client._mock_store  # type: ignore[attr-defined]
        store.get_session_history.assert_called_once_with("s1", 50, user_id="usr_test")
