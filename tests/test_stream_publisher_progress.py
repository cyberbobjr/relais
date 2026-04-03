"""Unit tests for StreamPublisher.push_progress() and push_chunk() type field.

Tests validate:
- push_progress() calls XADD with type='progress', event, detail, seq, is_final='0'
- push_progress() increments seq like push_chunk()
- push_chunk() includes type='token' in every XADD call
- seq is shared between push_chunk and push_progress (monotonic counter)
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
# Tests — push_progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_xadd_fields() -> None:
    """push_progress('tool_call', 'web_search') calls XADD with correct fields.

    Validates that type='progress', event='tool_call', detail='web_search',
    is_final='0' are present in the XADD payload.
    """
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="corr-p1")

    await pub.push_progress("tool_call", "web_search")

    redis.xadd.assert_awaited_once()
    call_args = redis.xadd.await_args_list[0]
    key = call_args.args[0]
    fields = call_args.args[1]

    assert key == "relais:messages:streaming:discord:corr-p1"
    assert fields["type"] == "progress"
    assert fields["event"] == "tool_call"
    assert fields["detail"] == "web_search"
    assert fields["seq"] == "0"
    assert fields["is_final"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_increments_seq() -> None:
    """push_progress() increments the shared sequence counter.

    After two push_progress calls, seq values should be '0' and '1'.
    """
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="seq-prog")

    await pub.push_progress("tool_call", "tool_a")
    await pub.push_progress("tool_result", "tool_a: result")

    first_fields = redis.xadd.await_args_list[0].args[1]
    second_fields = redis.xadd.await_args_list[1].args[1]

    assert first_fields["seq"] == "0"
    assert second_fields["seq"] == "1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_seq_shared_with_push_chunk() -> None:
    """seq counter is shared between push_chunk and push_progress.

    Interleaving push_chunk and push_progress must yield monotonically
    increasing seq values across both call types.
    """
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="shared-seq")

    await pub.push_chunk("hello")         # seq=0
    await pub.push_progress("tool_call", "search")  # seq=1
    await pub.push_chunk(" world")        # seq=2

    calls = redis.xadd.await_args_list
    seqs = [c.args[1]["seq"] for c in calls]
    assert seqs == ["0", "1", "2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_maxlen_applied() -> None:
    """push_progress() uses maxlen=STREAM_MAXLEN on XADD (same as push_chunk)."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="maxlen-prog")

    await pub.push_progress("subagent_start", "tools:abc")

    call_kwargs = redis.xadd.await_args_list[0].kwargs
    assert call_kwargs.get("maxlen") == StreamPublisher.STREAM_MAXLEN


# ---------------------------------------------------------------------------
# Tests — push_chunk includes type='token'
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_chunk_has_type_token() -> None:
    """push_chunk() must include type='token' in the XADD fields.

    This is a backward-compatible addition: consumers reading older format
    still see chunk/seq/is_final, and new consumers can filter by type.
    """
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="type-test")

    await pub.push_chunk("some text")

    fields = redis.xadd.await_args_list[0].args[1]
    assert fields["type"] == "token"
    assert fields["chunk"] == "some text"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_chunk_final_sentinel_has_type_token() -> None:
    """finalize()'s sentinel push_chunk also has type='token'."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="final-type")

    await pub.finalize()

    fields = redis.xadd.await_args_list[0].args[1]
    assert fields["type"] == "token"
    assert fields["is_final"] == "1"


# ---------------------------------------------------------------------------
# Tests — outgoing stream publication (source_envelope)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_without_source_envelope_single_xadd() -> None:
    """Without source_envelope, push_progress calls xadd exactly once (streaming key only)."""
    redis = _make_redis()
    pub = StreamPublisher(redis, channel="discord", correlation_id="no-src")

    await pub.push_progress("tool_call", "web_search")

    assert redis.xadd.await_count == 1
    key = redis.xadd.await_args_list[0].args[0]
    assert key == "relais:messages:streaming:discord:no-src"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_with_source_envelope_calls_outgoing_xadd() -> None:
    """With source_envelope, push_progress calls xadd twice: streaming + outgoing keys."""
    from common.envelope import Envelope

    redis = _make_redis()
    src = Envelope(
        content="bonjour",
        sender_id="discord:42",
        channel="discord",
        session_id="sess-1",
        correlation_id="corr-src",
        metadata={"reply_to": "999"},
    )
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-src", source_envelope=src
    )

    await pub.push_progress("tool_call", "search_web")

    assert redis.xadd.await_count == 2
    keys = [c.args[0] for c in redis.xadd.await_args_list]
    assert "relais:messages:streaming:discord:corr-src" in keys
    assert "relais:messages:outgoing:discord" in keys


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_outgoing_envelope_metadata() -> None:
    """The payload published to the outgoing stream carries correct progress metadata."""
    import json
    from common.envelope import Envelope

    redis = _make_redis()
    src = Envelope(
        content="bonjour",
        sender_id="discord:42",
        channel="discord",
        session_id="sess-2",
        correlation_id="corr-meta",
        metadata={"reply_to": "888"},
    )
    pub = StreamPublisher(
        redis, channel="discord", correlation_id="corr-meta", source_envelope=src
    )

    await pub.push_progress("tool_call", "my_tool")

    # The outgoing XADD is the second call (after the streaming key XADD)
    outgoing_call = next(
        c for c in redis.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing:discord"
    )
    payload = json.loads(outgoing_call.args[1]["payload"])

    assert payload["metadata"]["message_type"] == "progress"
    assert payload["metadata"]["progress_event"] == "tool_call"
    assert payload["metadata"]["progress_detail"] == "my_tool"
    assert payload["channel"] == "discord"
    assert payload["correlation_id"] == "corr-meta"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_progress_outgoing_key_uses_channel_name() -> None:
    """Outgoing key is relais:messages:outgoing:{channel}, not hardcoded to 'discord'."""
    import json
    from common.envelope import Envelope

    redis = _make_redis()
    src = Envelope(
        content="hello",
        sender_id="telegram:99",
        channel="telegram",
        session_id="sess-3",
        correlation_id="corr-tg",
        metadata={},
    )
    pub = StreamPublisher(
        redis, channel="telegram", correlation_id="corr-tg", source_envelope=src
    )

    await pub.push_progress("subagent_start", "tools")

    keys = [c.args[0] for c in redis.xadd.await_args_list]
    assert "relais:messages:outgoing:telegram" in keys
