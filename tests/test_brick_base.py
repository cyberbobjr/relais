"""TDD tests for common.brick_base — BrickBase abstract class.

Written BEFORE the implementation (RED phase).

Tests cover:
- Full lifecycle: start → handle messages → shutdown
- ack_mode="always" → XACK even if handler returns False
- ack_mode="on_success" → no XACK if handler returns False
- ack_mode="on_success" → XACK if handler returns True
- reload_config via pubsub (simulate Redis message)
- N specs (multiple streams) processed in parallel
- on_startup / on_shutdown called in correct order
- exception in handler does not crash the loop
- BrickLogger writes to relais:logs + logging
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — concrete BrickBase subclass for testing
# ---------------------------------------------------------------------------


def _make_fake_brick(
    spec_ack_mode: str = "always",
    handler_return: bool = True,
    handler_raises: Exception | None = None,
):
    """Return a concrete BrickBase subclass suitable for unit tests.

    Args:
        spec_ack_mode: Value to pass as ack_mode in the StreamSpec.
        handler_return: Value returned by the handler.
        handler_raises: If not None, the handler will raise this exception.

    Returns:
        A FakeBrick class (not an instance).
    """
    from common.brick_base import BrickBase, StreamSpec

    class FakeBrick(BrickBase):
        def __init__(self):
            super().__init__("fake")
            self.handled_envelopes: list = []
            self.startup_called = False
            self.shutdown_called = False
            self._handler_return = handler_return
            self._handler_raises = handler_raises

        def _load(self) -> None:
            pass

        def stream_specs(self) -> list[StreamSpec]:
            return [
                StreamSpec(
                    stream="relais:test:stream",
                    group="fake_group",
                    consumer="fake_1",
                    handler=self._handle,
                    ack_mode=spec_ack_mode,
                )
            ]

        async def _handle(self, envelope, redis_conn) -> bool:
            if self._handler_raises is not None:
                raise self._handler_raises
            self.handled_envelopes.append(envelope)
            return self._handler_return

        async def on_startup(self, redis: Any) -> None:
            self.startup_called = True

        async def on_shutdown(self) -> None:
            self.shutdown_called = True

    return FakeBrick


def _make_redis_mock(payloads: list[str]) -> AsyncMock:
    """Build an AsyncMock Redis connection that returns *payloads* once, then [].

    Args:
        payloads: List of Envelope JSON strings to return on the first xreadgroup call.

    Returns:
        AsyncMock configured as a minimal Redis-like object.
    """
    from common.envelope import Envelope

    messages = [
        (f"{i}-0".encode(), {"payload": p})
        for i, p in enumerate(payloads, start=1)
    ]
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(
        side_effect=[
            [("relais:test:stream", messages)] if messages else [],
            [],  # second call returns empty → loop exits
        ]
    )
    redis.xack = AsyncMock(return_value=1)
    redis.xadd = AsyncMock(return_value=b"1-0")
    redis.pubsub = MagicMock(return_value=AsyncMock())
    return redis


def _make_envelope_payload(content: str = "hello") -> str:
    """Return a serialized Envelope as JSON string.

    Args:
        content: Message body for the envelope.

    Returns:
        JSON string.
    """
    from common.envelope import Envelope

    env = Envelope(
        content=content,
        sender_id="discord:123",
        channel="discord",
        session_id="sess-1",
        correlation_id="corr-1",
    )
    return env.to_json()


# ---------------------------------------------------------------------------
# StreamSpec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_spec_is_frozen():
    """StreamSpec is an immutable frozen dataclass."""
    from common.brick_base import StreamSpec

    spec = StreamSpec(
        stream="relais:test",
        group="g",
        consumer="c",
        handler=AsyncMock(),
        ack_mode="always",
    )
    with pytest.raises((TypeError, AttributeError)):
        spec.stream = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_stream_spec_defaults():
    """StreamSpec default values: ack_mode='always', block_ms=2000, count=10."""
    from common.brick_base import StreamSpec

    spec = StreamSpec(
        stream="s",
        group="g",
        consumer="c",
        handler=AsyncMock(),
    )
    assert spec.ack_mode == "always"
    assert spec.block_ms == 2000
    assert spec.count == 10


# ---------------------------------------------------------------------------
# BrickBase lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_startup_called_before_loop():
    """on_startup is called before the stream loop starts."""
    FakeBrick = _make_fake_brick()
    brick = FakeBrick()
    payload = _make_envelope_payload()
    redis = _make_redis_mock([payload])
    order: list[str] = []

    original_on_startup = brick.on_startup

    async def tracked_startup(r):
        order.append("startup")
        await original_on_startup(r)

    brick.on_startup = tracked_startup

    original_run_loop = brick._run_stream_loop.__func__  # type: ignore[attr-defined]

    async def tracked_loop(self_arg, spec, r, shutdown_event):
        order.append("loop")
        shutdown_event.set()

    with patch.object(type(brick), "_run_stream_loop", tracked_loop):
        with patch("common.brick_base.RedisClient") as mock_rc:
            mock_rc.return_value.get_connection = AsyncMock(return_value=redis)
            mock_rc.return_value.close = AsyncMock()
            await brick.start()

    assert order.index("startup") < order.index("loop")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_shutdown_called_after_loop():
    """on_shutdown is called after the stream loop finishes."""
    FakeBrick = _make_fake_brick()
    brick = FakeBrick()
    redis = _make_redis_mock([])
    order: list[str] = []

    original_shutdown = brick.on_shutdown

    async def tracked_shutdown():
        order.append("shutdown")
        await original_shutdown()

    brick.on_shutdown = tracked_shutdown

    async def quick_loop(self_arg, spec, r, shutdown_event):
        order.append("loop_done")
        shutdown_event.set()

    with patch.object(type(brick), "_run_stream_loop", quick_loop):
        with patch("common.brick_base.RedisClient") as mock_rc:
            mock_rc.return_value.get_connection = AsyncMock(return_value=redis)
            mock_rc.return_value.close = AsyncMock()
            await brick.start()

    assert order.index("loop_done") < order.index("shutdown")


# ---------------------------------------------------------------------------
# ack_mode="always"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ack_mode_always_acks_when_handler_returns_false():
    """ack_mode='always' issues XACK even when the handler returns False."""
    FakeBrick = _make_fake_brick(spec_ack_mode="always", handler_return=False)
    brick = FakeBrick()
    payload = _make_envelope_payload()
    redis = _make_redis_mock([payload])

    shutdown_event = asyncio.Event()

    # Only run 2 xreadgroup calls then stop
    original_xreadgroup = redis.xreadgroup

    call_count = {"n": 0}

    async def limited_xreadgroup(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("relais:test:stream", [("1-0", {"payload": payload})])]
        shutdown_event.set()
        return []

    redis.xreadgroup = limited_xreadgroup

    spec = brick.stream_specs()[0]
    await brick._run_stream_loop(spec, redis, shutdown_event)

    redis.xack.assert_awaited_once_with("relais:test:stream", "fake_group", "1-0")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ack_mode_always_acks_when_handler_returns_true():
    """ack_mode='always' issues XACK when the handler returns True."""
    FakeBrick = _make_fake_brick(spec_ack_mode="always", handler_return=True)
    brick = FakeBrick()
    payload = _make_envelope_payload()
    redis = _make_redis_mock([payload])

    shutdown_event = asyncio.Event()
    call_count = {"n": 0}

    async def limited_xreadgroup(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("relais:test:stream", [("1-0", {"payload": payload})])]
        shutdown_event.set()
        return []

    redis.xreadgroup = limited_xreadgroup

    spec = brick.stream_specs()[0]
    await brick._run_stream_loop(spec, redis, shutdown_event)

    redis.xack.assert_awaited_once_with("relais:test:stream", "fake_group", "1-0")


# ---------------------------------------------------------------------------
# ack_mode="on_success"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ack_mode_on_success_no_ack_when_handler_returns_false():
    """ack_mode='on_success' does NOT XACK when the handler returns False."""
    FakeBrick = _make_fake_brick(spec_ack_mode="on_success", handler_return=False)
    brick = FakeBrick()
    payload = _make_envelope_payload()

    shutdown_event = asyncio.Event()
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()
    call_count = {"n": 0}

    async def limited_xreadgroup(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("relais:test:stream", [("1-0", {"payload": payload})])]
        shutdown_event.set()
        return []

    redis.xreadgroup = limited_xreadgroup

    spec = brick.stream_specs()[0]
    await brick._run_stream_loop(spec, redis, shutdown_event)

    redis.xack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ack_mode_on_success_acks_when_handler_returns_true():
    """ack_mode='on_success' issues XACK when the handler returns True."""
    FakeBrick = _make_fake_brick(spec_ack_mode="on_success", handler_return=True)
    brick = FakeBrick()
    payload = _make_envelope_payload()

    shutdown_event = asyncio.Event()
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()
    call_count = {"n": 0}

    async def limited_xreadgroup(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("relais:test:stream", [("1-0", {"payload": payload})])]
        shutdown_event.set()
        return []

    redis.xreadgroup = limited_xreadgroup

    spec = brick.stream_specs()[0]
    await brick._run_stream_loop(spec, redis, shutdown_event)

    redis.xack.assert_awaited_once_with("relais:test:stream", "fake_group", "1-0")


# ---------------------------------------------------------------------------
# Exception in handler does not crash the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_exception_in_handler_does_not_crash_loop():
    """An exception raised by the handler is caught; the loop continues."""
    FakeBrick = _make_fake_brick(
        spec_ack_mode="always",
        handler_raises=RuntimeError("boom"),
    )
    brick = FakeBrick()
    payload = _make_envelope_payload()

    shutdown_event = asyncio.Event()
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()
    call_count = {"n": 0}

    async def limited_xreadgroup(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("relais:test:stream", [("1-0", {"payload": payload})])]
        shutdown_event.set()
        return []

    redis.xreadgroup = limited_xreadgroup

    spec = brick.stream_specs()[0]
    # Should NOT raise
    await brick._run_stream_loop(spec, redis, shutdown_event)

    # With ack_mode="always", XACK is still called even on exception
    redis.xack.assert_awaited_once_with("relais:test:stream", "fake_group", "1-0")


# ---------------------------------------------------------------------------
# Multiple StreamSpecs (parallel loops)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_multiple_specs_run_in_parallel():
    """N StreamSpecs are launched as concurrent asyncio tasks."""
    from common.brick_base import BrickBase, StreamSpec

    handled: list[str] = []

    class TwoStreamBrick(BrickBase):
        def _load(self) -> None:
            pass

        def stream_specs(self) -> list[StreamSpec]:
            return [
                StreamSpec(
                    stream="relais:test:a",
                    group="g_a",
                    consumer="c_a",
                    handler=self._handle_a,
                ),
                StreamSpec(
                    stream="relais:test:b",
                    group="g_b",
                    consumer="c_b",
                    handler=self._handle_b,
                ),
            ]

        async def _handle_a(self, env, r) -> bool:
            handled.append("a")
            return True

        async def _handle_b(self, env, r) -> bool:
            handled.append("b")
            return True

    brick = TwoStreamBrick("two_stream")

    loop_calls: list[str] = []

    async def fake_loop(self_arg, spec, r, shutdown_event):
        loop_calls.append(spec.stream)
        shutdown_event.set()

    with patch.object(TwoStreamBrick, "_run_stream_loop", fake_loop):
        with patch("common.brick_base.RedisClient") as mock_rc:
            redis = AsyncMock()
            redis.xadd = AsyncMock()
            mock_rc.return_value.get_connection = AsyncMock(return_value=redis)
            mock_rc.return_value.close = AsyncMock()
            await brick.start()

    assert "relais:test:a" in loop_calls
    assert "relais:test:b" in loop_calls


# ---------------------------------------------------------------------------
# reload_config via pubsub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reload_config_returns_true_by_default():
    """BrickBase.reload_config() returns True by default (no-op safe_reload)."""
    from common.brick_base import BrickBase, StreamSpec

    class MinimalBrick(BrickBase):
        def _load(self) -> None:
            pass

        def stream_specs(self) -> list[StreamSpec]:
            return []

    brick = MinimalBrick("minimal")
    result = await brick.reload_config()
    assert result is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_config_reload_listener_triggers_reload_on_message():
    """_config_reload_listener triggers reload_config when it receives 'reload'."""
    from common.brick_base import BrickBase, StreamSpec

    class MinimalBrick(BrickBase):
        reload_called = False

        def _load(self) -> None:
            pass

        def stream_specs(self) -> list[StreamSpec]:
            return []

        async def reload_config(self) -> bool:
            MinimalBrick.reload_called = True
            return True

    brick = MinimalBrick("minimal")

    # Simulate a pubsub message sequence: subscribe-reply, then "reload", then stop
    messages = [
        {"type": "subscribe", "data": 1},
        {"type": "message", "data": "reload"},
    ]
    msg_iter = iter(messages)

    pubsub_mock = AsyncMock()

    async def fake_listen():
        for msg in messages:
            yield msg

    pubsub_mock.listen = fake_listen
    pubsub_mock.subscribe = AsyncMock()

    redis_mock = AsyncMock()
    redis_mock.pubsub = MagicMock(return_value=pubsub_mock)

    # Run listener until it finishes the fake message sequence
    await brick._config_reload_listener(redis_mock, asyncio.Event())

    assert MinimalBrick.reload_called


# ---------------------------------------------------------------------------
# BrickLogger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_brick_logger_info_calls_xadd():
    """BrickLogger.info() publishes to relais:logs stream."""
    from common.brick_base import BrickLogger

    redis_mock = AsyncMock()
    redis_mock.xadd = AsyncMock()
    logger = BrickLogger("test_brick", lambda: redis_mock)

    await logger.info("hello world", correlation_id="corr-1")

    redis_mock.xadd.assert_awaited_once()
    stream_arg = redis_mock.xadd.call_args[0][0]
    assert stream_arg == "relais:logs"
    payload = redis_mock.xadd.call_args[0][1]
    assert payload["level"] == "INFO"
    assert payload["brick"] == "test_brick"
    assert "hello world" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_brick_logger_error_calls_xadd_with_error_level():
    """BrickLogger.error() publishes with level='ERROR'."""
    from common.brick_base import BrickLogger

    redis_mock = AsyncMock()
    redis_mock.xadd = AsyncMock()
    logger = BrickLogger("test_brick", lambda: redis_mock)

    await logger.error("something went wrong", correlation_id="corr-2")

    payload = redis_mock.xadd.call_args[0][1]
    assert payload["level"] == "ERROR"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_brick_logger_handles_redis_failure_gracefully():
    """BrickLogger swallows Redis errors without raising."""
    from common.brick_base import BrickLogger

    redis_mock = AsyncMock()
    redis_mock.xadd = AsyncMock(side_effect=Exception("redis down"))
    logger = BrickLogger("test_brick", lambda: redis_mock)

    # Must not raise
    await logger.info("this should not blow up")
