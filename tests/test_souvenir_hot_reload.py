"""Tests for souvenir.main.Souvenir — hot-reload interface: _load() no-op,
reload_config() no-op, and the Redis Pub/Sub listener _config_reload_listener().

Souvenir has no config to reload today.  The interface exists for consistency
and forward-compatibility.  Tests verify the contract rather than any specific
config behaviour.

TDD — tests are written before the implementation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_souvenir_minimal():
    """Build a minimal Souvenir instance with all heavy deps mocked.

    Returns:
        A Souvenir instance with mocked stores and Redis client.
    """
    from souvenir.main import Souvenir

    with (
        patch("souvenir.main.LongTermStore"),
        patch("souvenir.main.FileStore"),
        patch("souvenir.main.build_registry", return_value={}),
        patch("common.brick_base.RedisClient"),
    ):
        souvenir = Souvenir()

    if not hasattr(souvenir, "_config_lock"):
        souvenir._config_lock = asyncio.Lock()

    return souvenir


# ---------------------------------------------------------------------------
# _load() — exists and is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_has_load_method() -> None:
    """Souvenir must expose a _load() method."""
    souvenir = _make_souvenir_minimal()
    assert hasattr(souvenir, "_load"), "Souvenir must have a _load() method"
    assert callable(souvenir._load)


@pytest.mark.unit
def test_souvenir_load_is_noop_and_returns_none() -> None:
    """_load() is a no-op and returns None (no config to reload for now)."""
    souvenir = _make_souvenir_minimal()
    result = souvenir._load()
    assert result is None


@pytest.mark.unit
def test_souvenir_load_does_not_raise() -> None:
    """_load() never raises regardless of external state."""
    souvenir = _make_souvenir_minimal()
    try:
        souvenir._load()
    except Exception as exc:
        pytest.fail(f"_load() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# reload_config() — exists and is a no-op returning True
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_has_reload_config_method() -> None:
    """Souvenir must expose a reload_config() async method."""
    souvenir = _make_souvenir_minimal()
    assert hasattr(souvenir, "reload_config"), "Souvenir must have a reload_config() method"
    assert callable(souvenir.reload_config)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_reload_config_returns_true() -> None:
    """reload_config() returns True (no-op succeeds)."""
    souvenir = _make_souvenir_minimal()
    result = await souvenir.reload_config()
    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_reload_config_does_not_raise() -> None:
    """reload_config() never raises."""
    souvenir = _make_souvenir_minimal()
    try:
        await souvenir.reload_config()
    except Exception as exc:
        pytest.fail(f"reload_config() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# _config_reload_listener — pub/sub
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_listener_subscribes_to_correct_channel() -> None:
    """_config_reload_listener subscribes to relais:config:reload:souvenir."""
    souvenir = _make_souvenir_minimal()

    subscribe_calls: list[str] = []

    async def fake_listen():
        return
        yield

    mock_pubsub = AsyncMock()

    async def capture_subscribe(channel: str) -> None:
        subscribe_calls.append(channel)

    mock_pubsub.subscribe = capture_subscribe
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await souvenir._config_reload_listener(mock_redis)

    assert "relais:config:reload:souvenir" in subscribe_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_listener_calls_reload_on_reload_message() -> None:
    """_config_reload_listener calls reload_config() when it receives 'reload'."""
    souvenir = _make_souvenir_minimal()

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    souvenir.reload_config = fake_reload

    messages = [{"type": "message", "data": b"reload"}]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await souvenir._config_reload_listener(mock_redis)

    assert reload_called == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_listener_ignores_non_reload_messages() -> None:
    """_config_reload_listener does NOT call reload_config for irrelevant payloads."""
    souvenir = _make_souvenir_minimal()

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    souvenir.reload_config = fake_reload

    messages = [
        {"type": "message", "data": b"RELOAD"},
        {"type": "subscribe", "data": 1},
        {"type": "message", "data": b"clear"},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await souvenir._config_reload_listener(mock_redis)

    assert reload_called == []
