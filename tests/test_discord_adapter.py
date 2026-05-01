"""Tests for Discord adapter — Phase 2 streaming uniformization.

TDD — tests are written before the implementation.
Phase 2 behaviour:
  - Atelier always publishes token-by-token to relais:messages:streaming:discord:{corr_id}
  - The adapter subscribes to relais:streaming:start:discord pub/sub
  - On each start signal: spawn _consume_streaming_reply() task that reads
    the streaming stream, buffers chunks, sends the complete message once
    is_final=1 is received
  - When outgoing:discord receives an envelope with context["atelier"]["streamed"]=True,
    the envelope is silently dropped (the streaming consumer already delivered it)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MESSAGE_INCOMING,
    ACTION_MESSAGE_OUTGOING,
    ACTION_MESSAGE_PROGRESS,
)
from common.contexts import CTX_AIGUILLEUR, CTX_ATELIER
from common.streams import stream_streaming


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client():
    """Return a _RelaisDiscordClient with mocked RedisClient and redis_conn."""
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient

    with patch("aiguilleur.channels.discord.adapter.RedisClient"):
        client = _RelaisDiscordClient()

    redis_conn = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=[])
    redis_conn.xadd = AsyncMock(return_value="0-0")
    client._redis_conn = redis_conn
    return client


def _outgoing_envelope(streamed: bool = False, content: str = "Hello!") -> Envelope:
    """Build a final-reply envelope destined for Discord."""
    atelier_ctx: dict = {"streamed": streamed} if streamed else {}
    return Envelope(
        content=content,
        sender_id="discord:111",
        channel="discord",
        session_id="sess-1",
        action=ACTION_MESSAGE_OUTGOING,
        context={
            CTX_AIGUILLEUR: {"reply_to": "999"},
            CTX_ATELIER: atelier_ctx,
        },
    )


# ---------------------------------------------------------------------------
# Phase 2.1 — _deliver_outgoing_message skips when streamed flag is set
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_skips_outgoing_when_streamed_flag_set() -> None:
    """_deliver_outgoing_message must not call channel.send() when streamed=True.

    The LLM response was already delivered via the streaming stream.
    Sending again from outgoing:discord would produce a duplicate message.
    """
    client = _make_client()
    mock_channel = AsyncMock()

    with patch.object(client, "_resolve_discord_channel", return_value=mock_channel):
        await client._deliver_outgoing_message(
            {"payload": _outgoing_envelope(streamed=True).to_json()}
        )

    mock_channel.send.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_delivers_outgoing_when_streamed_flag_false() -> None:
    """_deliver_outgoing_message still sends when streamed=False (non-LLM messages).

    Error replies, command rejections from Sentinelle, and other non-LLM
    messages are delivered via outgoing:discord without streaming; they must
    still be sent normally.
    """
    client = _make_client()
    mock_channel = AsyncMock()

    with patch.object(client, "_resolve_discord_channel", return_value=mock_channel):
        await client._deliver_outgoing_message(
            {"payload": _outgoing_envelope(streamed=False, content="Accès refusé.").to_json()}
        )

    mock_channel.send.assert_called_once_with("Accès refusé.")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_skips_outgoing_without_resolving_channel() -> None:
    """When streamed=True, _resolve_discord_channel is never called.

    Avoids an unnecessary Discord API call for a message that will be dropped.
    """
    client = _make_client()

    with patch.object(client, "_resolve_discord_channel", new_callable=AsyncMock) as mock_resolve:
        await client._deliver_outgoing_message(
            {"payload": _outgoing_envelope(streamed=True).to_json()}
        )

    mock_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 2.2 — _consume_streaming_reply buffers tokens and sends complete message
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_consume_streaming_reply_buffers_and_sends() -> None:
    """_consume_streaming_reply reads token entries and sends assembled message.

    Given a streaming stream with three chunks followed by is_final=1, the
    method must concatenate them and deliver a single Discord message.
    """
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-1",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "999"}},
    )
    corr_id = envelope.correlation_id
    stream_key = stream_streaming("discord", corr_id)

    entries = [
        (stream_key, [
            ("1-0", {"type": "token", "chunk": "Bonjour", "seq": "0", "is_final": "0"}),
            ("1-1", {"type": "token", "chunk": " le", "seq": "1", "is_final": "0"}),
            ("1-2", {"type": "token", "chunk": " monde", "seq": "2", "is_final": "0"}),
        ]),
    ]
    final_entry = [
        (stream_key, [
            ("1-3", {"type": "token", "chunk": "", "seq": "3", "is_final": "1"}),
        ]),
    ]

    call_count = 0

    async def mock_xread(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return entries
        if call_count == 2:
            return final_entry
        return []

    client._redis_conn.xread = mock_xread

    mock_channel = AsyncMock()

    with patch.object(client, "_resolve_discord_channel", return_value=mock_channel):
        await client._consume_streaming_reply(envelope)

    mock_channel.send.assert_called_once_with("Bonjour le monde")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_consume_streaming_reply_cancels_typing() -> None:
    """_consume_streaming_reply cancels the typing indicator after delivery."""
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-1",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "999"}},
    )
    corr_id = envelope.correlation_id

    # Register a fake typing task
    fake_task = MagicMock()
    fake_task.cancel = MagicMock()
    client._typing_tasks[corr_id] = fake_task

    stream_key = stream_streaming("discord", corr_id)
    client._redis_conn.xread = AsyncMock(return_value=[
        (stream_key, [("1-0", {"type": "token", "chunk": "Hi", "seq": "0", "is_final": "1"})]),
    ])

    mock_channel = AsyncMock()

    with patch.object(client, "_resolve_discord_channel", return_value=mock_channel):
        await client._consume_streaming_reply(envelope)

    # Typing task must have been cancelled
    fake_task.cancel.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_consume_streaming_reply_ignores_empty_final_chunk() -> None:
    """is_final=1 sentinel with empty chunk is not included in the message.

    The StreamPublisher always sends an empty final sentinel to close the stream.
    That empty string must not be appended to the buffered text.
    """
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-1",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "999"}},
    )
    stream_key = stream_streaming("discord", envelope.correlation_id)

    client._redis_conn.xread = AsyncMock(return_value=[
        (stream_key, [
            ("1-0", {"type": "token", "chunk": "Final", "seq": "0", "is_final": "0"}),
            ("1-1", {"type": "token", "chunk": "", "seq": "1", "is_final": "1"}),
        ]),
    ])

    mock_channel = AsyncMock()

    with patch.object(client, "_resolve_discord_channel", return_value=mock_channel):
        await client._consume_streaming_reply(envelope)

    mock_channel.send.assert_called_once_with("Final")


# ---------------------------------------------------------------------------
# Phase 2.3 — _subscribe_streaming_start pub/sub listener
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_subscribe_streaming_start_spawns_task() -> None:
    """_subscribe_streaming_start spawns _consume_streaming_reply for each start message."""
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-1",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "999"}},
    )

    messages = [{"type": "message", "data": envelope.to_json().encode()}]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    client._redis_conn.pubsub = MagicMock(return_value=mock_pubsub)

    spawned: list[Envelope] = []

    async def fake_consume(env: Envelope) -> None:
        spawned.append(env)

    with patch.object(client, "_consume_streaming_reply", side_effect=fake_consume):
        await client._subscribe_streaming_start()
        # Yield to the event loop so the spawned task can run
        await asyncio.sleep(0)

    assert len(spawned) == 1
    assert spawned[0].correlation_id == envelope.correlation_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_subscribe_streaming_start_subscribes_correct_channel() -> None:
    """_subscribe_streaming_start subscribes to relais:streaming:start:discord."""
    client = _make_client()

    subscribed: list[str] = []

    async def fake_listen():
        return
        yield

    mock_pubsub = AsyncMock()

    async def capture_subscribe(channel: str) -> None:
        subscribed.append(channel)

    mock_pubsub.subscribe = capture_subscribe
    mock_pubsub.listen = fake_listen

    client._redis_conn.pubsub = MagicMock(return_value=mock_pubsub)

    await client._subscribe_streaming_start()

    assert "relais:streaming:start:discord" in subscribed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_subscribe_streaming_start_ignores_non_message_events() -> None:
    """_subscribe_streaming_start ignores pub/sub subscription confirmation events."""
    client = _make_client()

    messages = [
        {"type": "subscribe", "data": 1},
        {"type": "psubscribe", "data": 1},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    client._redis_conn.pubsub = MagicMock(return_value=mock_pubsub)

    spawned: list = []

    with patch.object(client, "_consume_streaming_reply", side_effect=lambda e: spawned.append(e)):
        await client._subscribe_streaming_start()

    assert spawned == []


# ---------------------------------------------------------------------------
# Phase 2.4 — setup_hook launches streaming subscriber
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_setup_hook_creates_streaming_subscriber_task() -> None:
    """setup_hook must create a background task for _subscribe_streaming_start.

    The streaming subscriber must be running alongside _consume_outgoing_stream
    so that streaming replies are buffered before the final outgoing:discord
    envelope arrives.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient

    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")

    mock_redis_client = MagicMock()
    mock_redis_client.get_connection = AsyncMock(return_value=redis_conn)

    with patch("aiguilleur.channels.discord.adapter.RedisClient", return_value=mock_redis_client):
        client = _RelaisDiscordClient()

    created_tasks: list[str] = []

    def mock_create_task(coro, **kwargs):
        created_tasks.append(getattr(coro, "__name__", repr(coro)))
        task = MagicMock()
        task.cancel = MagicMock()
        coro.close()
        return task

    with (
        patch.object(client, "_consume_outgoing_stream", new_callable=AsyncMock),
        patch.object(client, "_subscribe_streaming_start", new_callable=AsyncMock),
        patch.object(client, "loop", create=True) as mock_loop,
    ):
        mock_loop.create_task = mock_create_task
        await client.setup_hook()

    # Two tasks: _consume_outgoing_stream + _subscribe_streaming_start
    assert len(created_tasks) == 2
