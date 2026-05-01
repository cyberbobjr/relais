"""Tests for GET /v1/commands endpoint in the REST channel adapter.

TDD: Tests written BEFORE the implementation (RED phase).
Covers: happy path (200), authentication enforcement (401),
Commandant timeout (503), and XADD stream publishing.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from common.user_record import UserRecord


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_user_record(user_id: str = "usr_test") -> UserRecord:
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


def _make_registry(valid_token: str = "test-key") -> MagicMock:
    record = _make_user_record()
    registry = MagicMock()
    registry.resolve_rest_api_key.side_effect = lambda raw: (
        record if raw == valid_token else None
    )
    return registry


def _make_mock_store() -> AsyncMock:
    store = AsyncMock()
    store.get_session_history.return_value = []
    store.close = AsyncMock()
    return store


def _make_adapter_mock() -> MagicMock:
    from aiguilleur.channel_config import ChannelConfig

    config = ChannelConfig(
        name="rest",
        enabled=True,
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


_SAMPLE_CATALOG = {
    "commands": [
        {"name": "clear", "description": "Clears conversation history."},
        {"name": "help", "description": "Displays the list of available commands."},
    ]
}

_AUTH = {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def commands_client(request):
    """Build a TestClient wired for GET /v1/commands.

    Accepts an optional indirect parameter ``brpop_result``:
    - ``None`` simulates a Commandant timeout (BRPOP returns None).
    - A ``(key_bytes, json_bytes)`` tuple simulates a valid catalog response.
    Defaults to a two-entry catalog response.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from aiguilleur.channels.rest.correlator import ResponseCorrelator
    from aiguilleur.channels.rest.server import create_app

    default_brpop = (
        b"relais:commandant:catalog:test",
        json.dumps(_SAMPLE_CATALOG).encode(),
    )
    brpop_result = getattr(request, "param", default_brpop)

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()
    mock_redis.brpop = AsyncMock(return_value=brpop_result)
    mock_redis.xread = AsyncMock(return_value=[])
    mock_redis.xreadgroup = AsyncMock(return_value=[])
    mock_redis.xack = AsyncMock()
    mock_redis.xgroup_create = AsyncMock()

    registry = _make_registry("test-key")
    correlator = ResponseCorrelator()
    adapter = _make_adapter_mock()
    mock_store = _make_mock_store()

    server_config = {
        "bind": "127.0.0.1",
        "port": 8080,
        "request_timeout": 2,
        "cors_origins": ["*"],
        "include_traces": False,
    }

    with patch("aiguilleur.channels.rest.server.LongTermStore", return_value=mock_store):
        app = create_app(
            adapter=adapter,
            redis_conn=mock_redis,
            correlator=correlator,
            registry=registry,
            config=server_config,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        client._mock_redis = mock_redis  # type: ignore[attr-defined]
        yield client
        await client.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGetCommands:
    @pytest.mark.asyncio
    async def test_returns_200_with_commands(self, commands_client):
        """GET /v1/commands with auth → 200 JSON with a 'commands' list."""
        resp = await commands_client.get("/v1/commands", headers=_AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert "commands" in data
        assert isinstance(data["commands"], list)
        assert len(data["commands"]) > 0
        for cmd in data["commands"]:
            assert "name" in cmd
            assert "description" in cmd

    @pytest.mark.asyncio
    async def test_requires_authentication(self, commands_client):
        """GET /v1/commands without Authorization header → 401."""
        resp = await commands_client.get("/v1/commands")
        assert resp.status == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize("commands_client", [None], indirect=True)
    async def test_commandant_timeout_returns_503(self, commands_client):
        """GET /v1/commands when BRPOP returns None (timeout) → 503 with error."""
        resp = await commands_client.get("/v1/commands", headers=_AUTH)
        assert resp.status == 503
        data = await resp.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_xadd_published_to_commandant_query_stream(self, commands_client):
        """GET /v1/commands must XADD to relais:commandant:query with a payload."""
        from common.streams import STREAM_COMMANDANT_QUERY

        resp = await commands_client.get("/v1/commands", headers=_AUTH)
        assert resp.status == 200

        mock_redis: AsyncMock = commands_client._mock_redis  # type: ignore[attr-defined]
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args.args[0] == STREAM_COMMANDANT_QUERY
        payload_dict = call_args.args[1]
        assert "payload" in payload_dict
