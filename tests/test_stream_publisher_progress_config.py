"""Unit tests for StreamPublisher with ProgressConfig gating — written TDD (RED first).

Tests:
1. push_progress does nothing if ProgressConfig(enabled=False)
2. push_progress skips a specific event when events[event]=False
3. push_progress truncates detail to detail_max_length
4. push_progress does not publish to outgoing if publish_to_outgoing=False
5. Without ProgressConfig (None) → current behaviour preserved (everything published)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from atelier.stream_publisher import StreamPublisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis() -> AsyncMock:
    """Build a minimal async Redis mock.

    Returns:
        AsyncMock with xadd and expire async methods.
    """
    redis = AsyncMock()
    redis.xadd = AsyncMock()
    redis.expire = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# 1. push_progress does nothing if ProgressConfig(enabled=False)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_disabled_by_progress_config() -> None:
    """push_progress() does not call xadd when ProgressConfig.enabled is False.

    When the master switch is off, no Redis call must be made for progress events.
    """
    from atelier.progress_config import ProgressConfig

    redis = _make_redis()
    cfg = ProgressConfig(enabled=False)
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-off", progress_config=cfg
    )

    await pub.push_progress("tool_call", "some_tool")

    redis.xadd.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. push_progress skips a specific event when events[event]=False
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_skips_disabled_event() -> None:
    """push_progress() does not publish when the specific event is disabled.

    If ProgressConfig.events['tool_call'] is False, no xadd should be called
    for a 'tool_call' event, but other events should still be published.
    """
    from atelier.progress_config import ProgressConfig

    redis = _make_redis()
    cfg = ProgressConfig(
        enabled=True,
        events={"tool_call": False, "tool_result": True, "subagent_start": True},
    )
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-event", progress_config=cfg
    )

    # tool_call is disabled — must not publish
    await pub.push_progress("tool_call", "search_web")
    redis.xadd.assert_not_awaited()

    # tool_result is enabled — must publish
    await pub.push_progress("tool_result", "result content")
    assert redis.xadd.await_count == 1


# ---------------------------------------------------------------------------
# 3. push_progress truncates detail to detail_max_length
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_truncates_detail() -> None:
    """push_progress() truncates detail string to detail_max_length characters.

    When detail is longer than ProgressConfig.detail_max_length, the published
    detail must be truncated to that length.
    """
    from atelier.progress_config import ProgressConfig

    redis = _make_redis()
    cfg = ProgressConfig(enabled=True, detail_max_length=10)
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-trunc", progress_config=cfg
    )

    long_detail = "A" * 50  # longer than limit of 10
    await pub.push_progress("tool_call", long_detail)

    assert redis.xadd.await_count == 1
    fields = redis.xadd.await_args_list[0].args[1]
    assert len(fields["detail"]) == 10
    assert fields["detail"] == "A" * 10


# ---------------------------------------------------------------------------
# 4. push_progress does not publish to outgoing if publish_to_outgoing=False
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_no_outgoing_when_publish_to_outgoing_false() -> None:
    """push_progress() does not publish to outgoing stream when publish_to_outgoing=False.

    Even with a source_envelope present, the outgoing xadd must be suppressed
    when ProgressConfig.publish_to_outgoing is False.
    """
    from atelier.progress_config import ProgressConfig
    from common.envelope import Envelope

    redis = _make_redis()
    cfg = ProgressConfig(enabled=True, publish_to_outgoing=False)
    src = Envelope(
        content="hello",
        sender_id="discord:1",
        channel="discord",
        session_id="sess",
        correlation_id="corr-nout",
        metadata={},
    )
    pub = StreamPublisher(
        redis,
        channel="discord",
        correlation_id="corr-nout",
        source_envelope=src,
        progress_config=cfg,
    )

    await pub.push_progress("tool_call", "a_tool")

    # Only one xadd (streaming key), not two
    assert redis.xadd.await_count == 1
    key = redis.xadd.await_args_list[0].args[0]
    assert key == "relais:messages:streaming:discord:corr-nout"


# ---------------------------------------------------------------------------
# 5. Without ProgressConfig (None) → current behaviour preserved
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_without_progress_config_publishes_normally() -> None:
    """When progress_config is None, push_progress() behaves exactly as before.

    No filtering, no truncation. The streaming xadd is always called.
    """
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="corr-none")

    # No progress_config argument — defaults to None
    await pub.push_progress("tool_call", "search_web")

    assert redis.xadd.await_count == 1
    fields = redis.xadd.await_args_list[0].args[1]
    assert fields["event"] == "tool_call"
    assert fields["detail"] == "search_web"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_without_progress_config_outgoing_still_published() -> None:
    """When progress_config is None, outgoing xadd is still called with source_envelope.

    Backward compatibility: if no ProgressConfig is passed, the existing behaviour
    (publish to outgoing when source_envelope is set) must be preserved.
    """
    from common.envelope import Envelope

    redis = _make_redis()
    src = Envelope(
        content="hi",
        sender_id="discord:5",
        channel="discord",
        session_id="s1",
        correlation_id="corr-bck",
        metadata={},
    )
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-bck", source_envelope=src
    )

    await pub.push_progress("tool_call", "tool_x")

    # streaming + outgoing = 2 calls
    assert redis.xadd.await_count == 2
    keys = [c.args[0] for c in redis.xadd.await_args_list]
    assert "relais:messages:outgoing:discord" in keys
