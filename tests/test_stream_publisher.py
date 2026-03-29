"""Unit tests for atelier.stream_publisher."""

from unittest.mock import AsyncMock, call

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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_chunk_xadd_with_seq() -> None:
    """push_chunk() calls XADD twice with incrementing seq values."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="corr-123")

    await pub.push_chunk("hello")
    await pub.push_chunk("world")

    assert redis.xadd.await_count == 2

    first_call = redis.xadd.await_args_list[0]
    second_call = redis.xadd.await_args_list[1]

    # Verify stream key format
    expected_key = "relais:messages:streaming:discord:corr-123"
    assert first_call.args[0] == expected_key
    assert second_call.args[0] == expected_key

    # Verify seq increments
    first_fields = first_call.args[1]
    second_fields = second_call.args[1]

    assert first_fields["chunk"] == "hello"
    assert first_fields["seq"] == "0"
    assert first_fields["is_final"] == "0"

    assert second_fields["chunk"] == "world"
    assert second_fields["seq"] == "1"
    assert second_fields["is_final"] == "0"


@pytest.mark.asyncio
async def test_finalize_sends_is_final_1() -> None:
    """finalize() sends a final XADD with is_final='1' and empty chunk."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="corr-123")

    await pub.push_chunk("data")
    await pub.finalize()

    # After push_chunk("data") seq=0, after finalize() push_chunk("") seq=1
    final_call = redis.xadd.await_args_list[-1]
    final_fields = final_call.args[1]

    assert final_fields["chunk"] == ""
    assert final_fields["seq"] == "1"
    assert final_fields["is_final"] == "1"


@pytest.mark.asyncio
async def test_stream_key_format() -> None:
    """StreamPublisher uses key format 'relais:messages:streaming:{channel}:{correlation_id}'."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="telegram", correlation_id="abc-123")

    await pub.push_chunk("test")

    used_key = redis.xadd.await_args_list[0].args[0]
    assert used_key == "relais:messages:streaming:telegram:abc-123"


@pytest.mark.asyncio
async def test_maxlen_applied_on_xadd() -> None:
    """XADD is called with maxlen=StreamPublisher.STREAM_MAXLEN."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="corr-xyz")

    await pub.push_chunk("chunk")

    call_kwargs = redis.xadd.await_args_list[0].kwargs
    assert call_kwargs.get("maxlen") == StreamPublisher.STREAM_MAXLEN


@pytest.mark.asyncio
async def test_finalize_sets_stream_ttl() -> None:
    """finalize() calls expire() on the stream key with STREAM_TTL_SECONDS."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="ttl-test")

    await pub.finalize()

    redis.expire.assert_awaited_once_with(
        "relais:messages:streaming:discord:ttl-test",
        StreamPublisher.STREAM_TTL_SECONDS,
    )


@pytest.mark.asyncio
async def test_seq_starts_at_zero() -> None:
    """The first push_chunk call uses seq='0'."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="seq-test")

    await pub.push_chunk("first")

    fields = redis.xadd.await_args_list[0].args[1]
    assert fields["seq"] == "0"


@pytest.mark.asyncio
async def test_finalize_after_no_push_chunks_sends_seq_0() -> None:
    """finalize() with no prior push_chunk() sends seq='0' as the final marker."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="empty-test")

    await pub.finalize()

    fields = redis.xadd.await_args_list[0].args[1]
    assert fields["seq"] == "0"
    assert fields["is_final"] == "1"
    assert fields["chunk"] == ""
