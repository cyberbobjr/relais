"""Tests TDD — Portail Phase 0: no command drop.

After Phase 0:
- Commands are forwarded to relais:security (no early drop)
- _is_command method no longer exists on Portail
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from portail.main import Portail


def _make_message(content: str) -> list:
    """Helper: retourne un résultat xreadgroup avec un seul message."""
    payload = json.dumps({
        "content": content,
        "sender_id": "discord:123",
        "channel": "discord",
        "session_id": "s1",
        "correlation_id": "c1",
        "timestamp": 0.0,
        "metadata": {},
        "media_refs": [],
    })
    return [(b"relais:messages:incoming", [(b"1-1", {b"payload": payload.encode()})])]


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_forwards_slash_command_to_security(mock_redis: AsyncMock) -> None:
    """After Phase 0, /clear must be forwarded to relais:security (not dropped)."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_message("/clear"))

    portail = Portail()
    portail._unknown_user_policy = "guest"  # unknown sender allowed
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis, shutdown=shutdown)

    security_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_forwards_quoted_command_to_security(mock_redis: AsyncMock) -> None:
    """After Phase 0, '"/help"' must be forwarded to relais:security (not dropped)."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_message('"/help"'))

    portail = Portail()
    portail._unknown_user_policy = "guest"
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis, shutdown=shutdown)

    security_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 1


@pytest.mark.unit
def test_portail_has_no_is_command_method() -> None:
    """After Phase 0, Portail must NOT have a _is_command static method."""
    portail = Portail()
    assert not hasattr(portail, "_is_command"), (
        "Portail._is_command should be removed in Phase 0"
    )


