"""Tests TDD — Portail DND check.

Verifies that Portail drops messages silently when relais:state:dnd is set
and forwards normally when DND is inactive.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from portail.main import Portail


@pytest.fixture
def mock_redis_no_dnd() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)  # DND inactif
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


@pytest.fixture
def mock_redis_dnd_active() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1")  # DND actif
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_forwards_when_dnd_inactive(mock_redis_no_dnd):
    """Sans DND actif, le message est forwardé vers relais:security."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message("bonjour"))

    portail = Portail()
    # Use guest policy so unknown sender (discord:123) is still forwarded —
    # this test focuses on DND behaviour, not on unknown-user policy.
    portail._unknown_user_policy = "guest"
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)

    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_drops_message_when_dnd_active(mock_redis_dnd_active):
    """Avec DND actif, le message est ACKé mais PAS forwardé vers relais:security."""
    mock_redis_dnd_active.xreadgroup = AsyncMock(return_value=_make_message("bonjour"))

    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_dnd_active, shutdown=shutdown)

    mock_redis_dnd_active.xack.assert_called_once()

    security_calls = [c for c in mock_redis_dnd_active.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_checks_dnd_key_name(mock_redis_dnd_active):
    """Le check DND utilise exactement la clé 'relais:state:dnd'."""
    mock_redis_dnd_active.xreadgroup = AsyncMock(return_value=_make_message("test"))

    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_dnd_active, shutdown=shutdown)

    mock_redis_dnd_active.get.assert_called_with("relais:state:dnd")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_drops_command_without_forwarding(mock_redis_no_dnd):
    """Un message commande (/clear, /help…) est ACKé sans être forwardé vers relais:security."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message("/clear"))

    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)

    mock_redis_no_dnd.xack.assert_called_once()
    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_drops_quoted_command_without_forwarding(mock_redis_no_dnd):
    """Une commande entre guillemets ("/help") est aussi ignorée par Portail."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message('"/help"'))

    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)

    mock_redis_no_dnd.xack.assert_called_once()
    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_drops_single_quoted_command_without_forwarding(mock_redis_no_dnd):
    """Une commande entre quotes simples ('/help') est aussi ignorée par Portail."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message("'/help'"))

    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)

    mock_redis_no_dnd.xack.assert_called_once()
    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_forwards_non_command_message(mock_redis_no_dnd):
    """Un message normal (sans slash) est bien forwardé vers relais:security."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message("bonjour le monde"))

    portail = Portail()
    # Use guest policy so unknown sender (discord:123) is still forwarded —
    # this test focuses on command detection, not on unknown-user policy.
    portail._unknown_user_policy = "guest"
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)

    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 1
