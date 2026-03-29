"""Tests for Discord Aiguilleur — Phase D.bis streaming support.

Covers _handle_streaming_message and _subscribe_streaming_start methods.
All Discord library calls and Redis interactions are mocked.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, call, PropertyMock
from typing import Any

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    correlation_id: str = "corr-stream-001",
    channel_id: int = 999888777,
    sender_id: str = "discord:123456789",
) -> Envelope:
    """Create a minimal Envelope configured for Discord streaming tests.

    Args:
        correlation_id: Used to derive the Redis stream key.
        channel_id: Discord channel ID stored in metadata.
        sender_id: The originating user identifier.

    Returns:
        A test Envelope instance with discord_channel_id populated.
    """
    return Envelope(
        content="Hello!",
        sender_id=sender_id,
        channel="discord",
        session_id="sess-stream",
        correlation_id=correlation_id,
        metadata={
            "reply_to": str(channel_id),
            "discord_channel_id": str(channel_id),
        },
    )


def _make_xread_result(
    entries: list[tuple[str, dict[str, str]]],
    stream_key: str = "relais:messages:streaming:discord:corr-stream-001",
) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    """Build a fake xread return value.

    Args:
        entries: List of (entry_id, fields_dict) tuples.
        stream_key: The stream name as it would appear in xread results.

    Returns:
        A list shaped like the real aioredis xread response.
    """
    return [(stream_key, entries)]


# ---------------------------------------------------------------------------
# STEP 1: Failing tests (RED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_sends_placeholder():
    """_handle_streaming_message must send a placeholder '▌' message first.

    The Discord channel.send('▌') call should happen before any xread
    iteration begins.
    """
    # Import inside test to allow patching at module level
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    channel_id = int(envelope.metadata["discord_channel_id"])

    # Mock Discord message returned by channel.send
    mock_message = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    # After placeholder, xread returns a single is_final chunk so the loop exits
    final_entry = ("entry-1", {"chunk": "Hi", "seq": "0", "is_final": "1"})
    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        return_value=_make_xread_result(
            [final_entry], f"relais:messages:streaming:discord:{envelope.correlation_id}"
        )
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    mock_channel.send.assert_called_once_with("▌")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_edits_as_chunks_arrive():
    """Intermediate chunks accumulate and message.edit is called with cursor.

    Sequence:
      chunk "Hello" (seq=0, is_final=0)  → buffer=5 chars, below threshold
      chunk " world" (seq=1, is_final=0) → buffer=11 chars, still below threshold
      chunk "" (seq=2, is_final=1)       → is_final triggers final edit without cursor

    With STREAM_EDIT_THROTTLE_CHARS=80, intermediate edits fire only on is_final,
    so we expect exactly one edit call: the final "Hello world" (no cursor).
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    mock_message = AsyncMock()
    mock_message.edit = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    # xread returns three separate responses simulating chunk arrival
    mock_redis.xread = AsyncMock(
        side_effect=[
            _make_xread_result([("id1", {"chunk": "Hello", "seq": "0", "is_final": "0"})], stream_key),
            _make_xread_result([("id2", {"chunk": " world", "seq": "1", "is_final": "0"})], stream_key),
            _make_xread_result([("id3", {"chunk": "", "seq": "2", "is_final": "1"})], stream_key),
        ]
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    # Only the final edit should have been called (no intermediate, buffer <80 chars)
    mock_message.edit.assert_called_once_with(content="Hello world")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_throttles_edits():
    """Intermediate edit fires when accumulated buffer reaches THROTTLE_CHARS.

    We send chunks totalling >= 80 chars before is_final to trigger a mid-stream
    edit, then confirm a second final edit occurs without the cursor.
    """
    from aiguilleur.discord.main import RelaisDiscordClient, STREAM_EDIT_THROTTLE_CHARS

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    # Build a chunk that exceeds the threshold on its own
    big_chunk = "A" * STREAM_EDIT_THROTTLE_CHARS

    mock_message = AsyncMock()
    mock_message.edit = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        side_effect=[
            _make_xread_result(
                [("id1", {"chunk": big_chunk, "seq": "0", "is_final": "0"})], stream_key
            ),
            _make_xread_result(
                [("id2", {"chunk": "!", "seq": "1", "is_final": "1"})], stream_key
            ),
        ]
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    # First call: intermediate edit with cursor after big chunk
    # Second call: final edit without cursor after "!"
    assert mock_message.edit.call_count == 2
    first_call_content = mock_message.edit.call_args_list[0].kwargs["content"]
    second_call_content = mock_message.edit.call_args_list[1].kwargs["content"]

    assert first_call_content == big_chunk + "▌"
    assert second_call_content == big_chunk + "!"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_stops_on_final():
    """After is_final=1, xread must not be called again.

    Verifies the while loop exits cleanly and does not poll Redis further.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    mock_message = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        return_value=_make_xread_result(
            [("id-final", {"chunk": "Done", "seq": "0", "is_final": "1"})], stream_key
        )
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    # xread called exactly once: the call that returned is_final=1
    mock_redis.xread.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_subscribe_streaming_start_launches_handler():
    """_subscribe_streaming_start must spawn _handle_streaming_message for each message.

    We publish one envelope to the Pub/Sub channel and assert that
    _handle_streaming_message was called with the deserialized Envelope.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    pubsub_message = {
        "type": "message",
        "data": envelope.to_json(),
    }

    mock_pubsub = AsyncMock()
    # listen() must be a regular callable returning an async iterable (not a coroutine)
    mock_pubsub.listen = MagicMock(return_value=_async_iter([pubsub_message]))
    mock_pubsub.subscribe = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    handler_calls: list[Envelope] = []
    spawned_tasks: list[asyncio.Task] = []

    async def fake_handle(env: Envelope) -> None:
        handler_calls.append(env)

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client._handle_streaming_message = fake_handle

        # Capture tasks so we can await them after _subscribe_streaming_start returns
        def capturing_create_task(coro):
            task = asyncio.ensure_future(coro)
            spawned_tasks.append(task)
            return task

        with patch("asyncio.create_task", side_effect=capturing_create_task):
            await client._subscribe_streaming_start()

        # Drive all spawned tasks to completion
        if spawned_tasks:
            await asyncio.gather(*spawned_tasks)

    assert len(handler_calls) == 1
    assert handler_calls[0].correlation_id == envelope.correlation_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_handles_discord_429_gracefully():
    """A Discord HTTP 429 during an intermediate edit must not crash the loop.

    The error should be caught, the loop continues, and the final edit
    (triggered by is_final=1) must still be attempted.
    """
    import discord as discord_lib
    from aiguilleur.discord.main import RelaisDiscordClient, STREAM_EDIT_THROTTLE_CHARS

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    big_chunk = "B" * STREAM_EDIT_THROTTLE_CHARS

    # First edit raises 429; second edit (final) succeeds
    rate_limit_response = MagicMock()
    rate_limit_response.status = 429
    rate_limit_exc = discord_lib.HTTPException(rate_limit_response, "rate limited")

    mock_message = AsyncMock()
    mock_message.edit = AsyncMock(
        side_effect=[rate_limit_exc, None]  # first raises, second succeeds
    )
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        side_effect=[
            _make_xread_result(
                [("id1", {"chunk": big_chunk, "seq": "0", "is_final": "0"})], stream_key
            ),
            _make_xread_result(
                [("id2", {"chunk": "!", "seq": "1", "is_final": "1"})], stream_key
            ),
        ]
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        # Should not raise; 429 is swallowed
        await client._handle_streaming_message(envelope)

    # The final edit must still be reached despite the intermediate 429
    assert mock_message.edit.call_count == 2
    final_content = mock_message.edit.call_args_list[1].kwargs["content"]
    assert final_content == big_chunk + "!"


# ---------------------------------------------------------------------------
# Async iteration helper
# ---------------------------------------------------------------------------


async def _async_iter(items: list[Any]):
    """Yield items one by one as an async iterator, then stop.

    Args:
        items: The list of objects to iterate over.

    Yields:
        Each item in order.
    """
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# on_message tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_ignores_own_messages():
    """on_message must silently return when the bot receives its own message.

    No XADD call should be made.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 42

    mock_message = MagicMock()
    mock_message.author.id = 42   # same as bot id → ignored

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_queues_envelope_on_mention():
    """on_message must XADD to relais:messages:incoming when the bot is mentioned.

    The envelope payload should be valid JSON containing the sender_id.
    """
    import json
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 999
    mock_message.author.name = "TestUser"
    mock_message.mentions = [mock_user]           # bot mentioned
    mock_message.channel = MagicMock()
    mock_message.channel.__class__ = MagicMock    # not DMChannel
    mock_message.channel.id = 555
    mock_message.content = f"<@{mock_user.id}> hello world"

    # Patch isinstance check for DMChannel
    import discord as discord_lib
    mock_message.channel.__class__ = type("TextChannel", (), {})

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    stream_name = call_args[0][0]
    payload_json = call_args[0][1]["payload"]
    assert stream_name == "relais:messages:incoming"
    data = json.loads(payload_json)
    assert "discord:999" in data["sender_id"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_dm_queues_envelope():
    """on_message must XADD when message arrives in a DM channel.

    DM channel check: isinstance(message.channel, discord.DMChannel).
    """
    import discord as discord_lib
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock(spec=discord_lib.Message)
    mock_message.author.id = 888
    mock_message.author.name = "DMUser"
    mock_message.mentions = []            # not mentioned
    mock_message.channel = MagicMock(spec=discord_lib.DMChannel)   # is DM
    mock_message.channel.id = 777
    mock_message.content = "just a DM"

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_empty_content_defaults_to_coucou():
    """on_message must substitute 'Coucou!' when cleaned content is empty.

    After stripping the mention from the message content, if nothing remains
    the default greeting is used.
    """
    import json
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 222
    mock_message.author.name = "Shy"
    mock_message.mentions = [mock_user]
    mock_message.channel = MagicMock()
    mock_message.channel.id = 333
    mock_message.content = f"<@{mock_user.id}>"  # mention only, stripped → ""

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    data = json.loads(payload_json)
    assert data["content"] == "Coucou!"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_xadd_failure_does_not_raise():
    """on_message must catch exceptions from XADD and not propagate them.

    Redis failure should be logged without crashing the event handler.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 333
    mock_message.mentions = [mock_user]
    mock_message.channel = MagicMock()
    mock_message.channel.id = 444
    mock_message.content = "hello"

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(side_effect=ConnectionError("redis down"))

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            # Must not raise
            await client.on_message(mock_message)


# ---------------------------------------------------------------------------
# consume_outgoing_stream tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_sends_reply_and_xack():
    """consume_outgoing_stream must send the envelope content and XACK the message.

    One message arrives via xreadgroup; channel.send and XACK must be called.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    env = _make_envelope()
    env_json = env.to_json()

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    mock_redis = AsyncMock()
    # is_closed: False (enter loop), True (exit after one iteration)
    is_closed_calls = [False, True]
    call_idx = {"n": 0}

    def is_closed():
        v = is_closed_calls[call_idx["n"]]
        call_idx["n"] = min(call_idx["n"] + 1, len(is_closed_calls) - 1)
        return v

    mock_redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    mock_redis.xreadgroup = AsyncMock(
        return_value=[
            (
                b"relais:messages:outgoing:discord",
                [(b"msg-1", {"payload": env_json})]
            )
        ]
    )
    mock_redis.xack = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_out = "relais:messages:outgoing:discord"
        client.group_name = "discord_relay_group"
        client.consumer_name = "discord_test"
        client.is_closed = is_closed
        client.get_channel = MagicMock(return_value=mock_channel)

        await client.consume_outgoing_stream()

    mock_channel.send.assert_called_once_with(env.content)
    mock_redis.xack.assert_called_once_with(
        "relais:messages:outgoing:discord", "discord_relay_group", b"msg-1"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_dm_fallback_when_channel_not_in_cache():
    """consume_outgoing_stream must fetch user and create DM when get_channel returns None.

    When the channel is not cached, the bot falls back to fetching the user and
    creating a DM channel to deliver the reply.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    env = _make_envelope(sender_id="discord:123456789")
    env_json = env.to_json()

    mock_dm_channel = AsyncMock()
    mock_dm_channel.send = AsyncMock()
    mock_user = AsyncMock()
    mock_user.create_dm = AsyncMock(return_value=mock_dm_channel)

    mock_redis = AsyncMock()
    is_closed_calls = [False, True]
    call_idx = {"n": 0}

    def is_closed():
        v = is_closed_calls[call_idx["n"]]
        call_idx["n"] = min(call_idx["n"] + 1, len(is_closed_calls) - 1)
        return v

    mock_redis.xgroup_create = AsyncMock()
    mock_redis.xreadgroup = AsyncMock(
        return_value=[
            (
                b"relais:messages:outgoing:discord",
                [(b"msg-2", {"payload": env_json})]
            )
        ]
    )
    mock_redis.xack = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client.redis_conn = mock_redis
        client.stream_out = "relais:messages:outgoing:discord"
        client.group_name = "discord_relay_group"
        client.consumer_name = "discord_test"
        client.is_closed = is_closed
        client.get_channel = MagicMock(return_value=None)   # not in cache
        client.fetch_user = AsyncMock(return_value=mock_user)

        await client.consume_outgoing_stream()

    mock_user.create_dm.assert_called_once()
    mock_dm_channel.send.assert_called_once_with(env.content)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_xreadgroup_exception_does_not_crash():
    """consume_outgoing_stream must not crash on transient xreadgroup errors.

    The stream error is caught and the loop retries after a short sleep.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    mock_redis = AsyncMock()
    call_count = {"n": 0}

    def is_closed():
        call_count["n"] += 1
        return call_count["n"] > 2   # exit after 2 loop iterations

    mock_redis.xgroup_create = AsyncMock()
    mock_redis.xreadgroup = AsyncMock(side_effect=ConnectionError("transient"))

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            client = RelaisDiscordClient.__new__(RelaisDiscordClient)
            client.redis_conn = mock_redis
            client.stream_out = "relais:messages:outgoing:discord"
            client.group_name = "discord_relay_group"
            client.consumer_name = "discord_test"
            client.is_closed = is_closed

            # Must not raise
            await client.consume_outgoing_stream()


# ---------------------------------------------------------------------------
# Streaming edge case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_channel_fetch_fallback():
    """_handle_streaming_message must fetch the channel when not in cache.

    get_channel returns None → fetch_channel is called and streaming proceeds.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    mock_message = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    final_entry = ("entry-1", {"chunk": "done", "seq": "0", "is_final": "1"})
    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        return_value=_make_xread_result([final_entry], stream_key)
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=None)   # not in cache
        client.fetch_channel = AsyncMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    client.fetch_channel.assert_called_once()
    mock_channel.send.assert_called_once_with("▌")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_channel_not_found_returns():
    """_handle_streaming_message must return early when channel cannot be found.

    Both get_channel (None) and fetch_channel (raises) fail → function exits
    without touching xread or send.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(side_effect=Exception("unknown channel"))

        await client._handle_streaming_message(envelope)

    mock_redis.xread.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_placeholder_send_failure_returns():
    """_handle_streaming_message must return early when placeholder send fails.

    If channel.send('▌') raises, no xread calls should be made.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(side_effect=Exception("discord outage"))

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    mock_redis.xread.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_xread_exception_breaks_loop():
    """_handle_streaming_message must break out of the loop when xread raises.

    After an xread exception the function exits without calling msg.edit.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()

    mock_message = AsyncMock()
    mock_message.edit = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(side_effect=ConnectionError("stream gone"))

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    mock_message.edit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_empty_xread_continues_until_final():
    """_handle_streaming_message must keep polling when xread returns empty.

    First xread returns [] (timeout), second returns is_final chunk.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    mock_message = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        side_effect=[
            [],   # timeout — continue
            _make_xread_result([("id1", {"chunk": "final", "seq": "0", "is_final": "1"})], stream_key),
        ]
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    assert mock_redis.xread.call_count == 2
    mock_message.edit.assert_called_once_with(content="final")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_streaming_message_bytes_keys():
    """_handle_streaming_message must support bytes dict keys from aioredis.

    Some aioredis versions return {b'chunk': ..., b'is_final': ...} instead
    of str keys. The function must decode them correctly.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    envelope = _make_envelope()
    stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

    mock_message = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_message)

    # Use bytes keys — simulate old aioredis that returns a non-dict with bytes keys.
    # isinstance(fields, dict) must be False so the else-branch handles decoding.
    class _BytesFields:
        """Non-dict mapping with bytes keys, mimicking old aioredis entry fields."""

        def __init__(self, data: dict) -> None:
            self._data = data

        def get(self, key: bytes, default: bytes = b"") -> bytes:
            """Return value for key or default."""
            return self._data.get(key, default)

    bytes_entry = ("id1", _BytesFields({b"chunk": b"hello bytes", b"seq": b"0", b"is_final": b"1"}))

    mock_redis = AsyncMock()
    mock_redis.xread = AsyncMock(
        return_value=_make_xread_result([bytes_entry], stream_key)
    )

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._handle_streaming_message(envelope)

    mock_message.edit.assert_called_once_with(content="hello bytes")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_subscribe_streaming_start_error_handling():
    """_subscribe_streaming_start must not crash on malformed Pub/Sub payloads.

    An invalid JSON payload should be caught and logged, then the loop continues.
    """
    from aiguilleur.discord.main import RelaisDiscordClient

    bad_message = {"type": "message", "data": "not valid json {{{"}
    good_envelope = _make_envelope()
    good_message = {"type": "message", "data": good_envelope.to_json()}

    mock_pubsub = AsyncMock()
    mock_pubsub.listen = MagicMock(
        return_value=_async_iter([bad_message, good_message])
    )
    mock_pubsub.subscribe = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    handler_calls: list = []

    async def fake_handle(env):
        handler_calls.append(env)

    spawned_tasks: list = []

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis = mock_redis
        client._handle_streaming_message = fake_handle

        def capturing_create_task(coro):
            task = asyncio.ensure_future(coro)
            spawned_tasks.append(task)
            return task

        with patch("asyncio.create_task", side_effect=capturing_create_task):
            await client._subscribe_streaming_start()

        if spawned_tasks:
            await asyncio.gather(*spawned_tasks)

    # Bad message skipped; good message processed
    assert len(handler_calls) == 1
    assert handler_calls[0].correlation_id == good_envelope.correlation_id
