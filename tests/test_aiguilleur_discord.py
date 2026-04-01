"""Tests for the Discord Aiguilleur adapter.

Covers on_message (incoming path) and _consume_outgoing_stream (outgoing path).
Streaming is intentionally disabled on Discord — no streaming tests here.
All Discord library calls and Redis interactions are mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from aiguilleur.channel_config import ChannelConfig

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    correlation_id: str = "corr-001",
    channel_id: int = 999888777,
    sender_id: str = "discord:123456789",
) -> Envelope:
    """Create a minimal Envelope for Discord adapter tests.

    Args:
        correlation_id: Unique request identifier.
        channel_id: Discord channel ID stored in metadata.
        sender_id: The originating user identifier.

    Returns:
        A test Envelope instance with reply_to populated.
    """
    return Envelope(
        content="Hello!",
        sender_id=sender_id,
        channel="discord",
        session_id="sess-001",
        correlation_id=correlation_id,
        metadata={
            "reply_to": str(channel_id),
        },
    )


# ---------------------------------------------------------------------------
# on_message tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_ignores_own_messages():
    """on_message must silently return when the bot receives its own message.

    No XADD call should be made.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 42

    mock_message = MagicMock()
    mock_message.author.id = 42   # same as bot id → ignored

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis_conn = mock_redis

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_queues_envelope_on_mention():
    """on_message must XADD to relais:messages:incoming when the bot is mentioned.

    The envelope payload should be valid JSON containing the sender_id.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 999
    mock_message.author.name = "TestUser"
    mock_message.mentions = [mock_user]           # bot mentioned
    mock_message.channel = type("TextChannel", (), {})()
    mock_message.channel.id = 555
    mock_message.content = f"<@{mock_user.id}> hello world"

    mock_redis = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._llm_profile = "default"

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
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

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
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._llm_profile = "default"

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
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

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
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._llm_profile = "default"

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
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

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
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._llm_profile = "default"

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            # Must not raise
            await client.on_message(mock_message)


# ---------------------------------------------------------------------------
# _consume_outgoing_stream tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_sends_reply_and_xack():
    """_consume_outgoing_stream must send the envelope content and XACK the message.

    One message arrives via xreadgroup; channel.send and XACK must both be called.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    env = _make_envelope()
    env_json = env.to_json()

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    mock_redis = AsyncMock()
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
        client._redis_conn = mock_redis
        client.stream_out = "relais:messages:outgoing:discord"
        client.group_name = "discord_relay_group"
        client.consumer_name = "discord_test"
        client.is_closed = is_closed
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._consume_outgoing_stream()

    mock_channel.send.assert_called_once_with(env.content)
    mock_redis.xack.assert_called_once_with(
        "relais:messages:outgoing:discord", "discord_relay_group", b"msg-1"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_dm_fallback_when_channel_not_in_cache():
    """_consume_outgoing_stream must create a DM when get_channel returns None.

    When the channel is not in the in-process cache, the bot falls back to
    fetching the user and creating a DM channel to deliver the reply.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

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
        client._redis_conn = mock_redis
        client.stream_out = "relais:messages:outgoing:discord"
        client.group_name = "discord_relay_group"
        client.consumer_name = "discord_test"
        client.is_closed = is_closed
        client.get_channel = MagicMock(return_value=None)   # not in cache
        client.fetch_user = AsyncMock(return_value=mock_user)

        await client._consume_outgoing_stream()

    mock_user.create_dm.assert_called_once()
    mock_dm_channel.send.assert_called_once_with(env.content)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consume_outgoing_xreadgroup_exception_does_not_crash():
    """_consume_outgoing_stream must not crash on transient xreadgroup errors.

    The exception is caught and the loop retries after asyncio.sleep(1).
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

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
            client._redis_conn = mock_redis
            client.stream_out = "relais:messages:outgoing:discord"
            client.group_name = "discord_relay_group"
            client.consumer_name = "discord_test"
            client.is_closed = is_closed

            # Must not raise
            await client._consume_outgoing_stream()

# ---------------------------------------------------------------------------
# LLM profile stamping in envelope.metadata (Phase 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_stamps_channel_profile_from_channel_config():
    """When the channel has profile='fast', envelope.metadata['channel_profile'] == 'fast'.

    The DiscordAiguilleur must stamp the resolved channel profile under the
    'channel_profile' key (not 'llm_profile') on every incoming envelope it
    creates.  Downstream Portail is responsible for resolving 'llm_profile'.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 999
    mock_message.author.name = "TestUser"
    mock_message.mentions = [mock_user]
    mock_message.channel = type("TextChannel", (), {})()
    mock_message.channel.id = 555
    mock_message.content = f"<@{mock_user.id}> hello"

    mock_redis = AsyncMock()

    # Channel config with an explicit profile
    channel_config = ChannelConfig(name="discord", profile="fast")

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._channel_config = channel_config
        client._llm_profile = "fast"   # pre-resolved at init time

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_called_once()
    payload_json = mock_redis.xadd.call_args[0][1]["payload"]
    data = json.loads(payload_json)
    assert data["metadata"]["channel_profile"] == "fast"
    assert "llm_profile" not in data["metadata"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_on_message_stamps_default_channel_profile_when_no_channel_profile():
    """When the channel has no profile, envelope.metadata['channel_profile'] falls back to 'default'.

    The DiscordAiguilleur uses get_default_llm_profile() when
    ChannelConfig.profile is None.  The result is stamped as 'channel_profile',
    not 'llm_profile'.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_message = MagicMock()
    mock_message.author.id = 999
    mock_message.author.name = "TestUser"
    mock_message.mentions = [mock_user]
    mock_message.channel = type("TextChannel", (), {})()
    mock_message.channel.id = 555
    mock_message.content = f"<@{mock_user.id}> hello"

    mock_redis = AsyncMock()

    # Channel config without a profile
    channel_config = ChannelConfig(name="discord", profile=None)

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._channel_config = channel_config
        client._llm_profile = "default"  # resolved at init to config fallback

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_called_once()
    payload_json = mock_redis.xadd.call_args[0][1]["payload"]
    data = json.loads(payload_json)
    assert data["metadata"]["channel_profile"] == "default"
    assert "llm_profile" not in data["metadata"]
