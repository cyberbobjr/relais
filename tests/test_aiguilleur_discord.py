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


def _init_typing_mocks(client) -> None:
    """Attach typing-indicator stubs to a bare client bypassing __init__.

    Sets ``_typing_tasks`` to an empty dict and replaces ``loop`` with a
    MagicMock whose ``create_task`` immediately closes the coroutine it
    receives (preventing "coroutine was never awaited" warnings) and returns
    a mock Task.

    Args:
        client: A ``_RelaisDiscordClient`` instance created via ``__new__``.
    """
    client._typing_tasks = {}
    mock_loop = MagicMock()
    mock_loop.create_task.side_effect = lambda coro: (coro.close(), MagicMock())[1]
    client.loop = mock_loop


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
        _init_typing_mocks(client)

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
        _init_typing_mocks(client)

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
        _init_typing_mocks(client)

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
        _init_typing_mocks(client)

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
        client._typing_tasks = {}

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
        client._typing_tasks = {}

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
        _init_typing_mocks(client)

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
        _init_typing_mocks(client)

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    mock_redis.xadd.assert_called_once()
    payload_json = mock_redis.xadd.call_args[0][1]["payload"]
    data = json.loads(payload_json)
    assert data["metadata"]["channel_profile"] == "default"
    assert "llm_profile" not in data["metadata"]


# ---------------------------------------------------------------------------
# Typing indicator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_typing_task_started_on_message():
    """on_message must register a typing task keyed by correlation_id.

    After on_message returns, _typing_tasks must contain exactly one entry
    and loop.create_task must have been called once with a coroutine.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_user = MagicMock()
    mock_user.id = 100

    mock_channel = MagicMock()
    mock_channel.id = 555
    mock_channel.trigger_typing = AsyncMock()

    mock_message = MagicMock()
    mock_message.author.id = 999
    mock_message.author.name = "TestUser"
    mock_message.mentions = [mock_user]
    mock_message.channel = mock_channel
    mock_message.content = f"<@{mock_user.id}> hello"

    mock_redis = AsyncMock()
    mock_task = MagicMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._redis_conn = mock_redis
        client.stream_in = "relais:messages:incoming"
        client._llm_profile = "default"
        client._typing_tasks = {}
        mock_loop = MagicMock()
        created_coros = []

        def _create_task(coro):
            created_coros.append(coro)
            coro.close()
            return mock_task

        mock_loop.create_task.side_effect = _create_task
        client.loop = mock_loop

        with patch.object(type(client), "user", new_callable=PropertyMock, return_value=mock_user):
            await client.on_message(mock_message)

    assert mock_loop.create_task.call_count == 1
    assert len(created_coros) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_typing_task_cancelled_on_delivery():
    """_deliver_outgoing_message must cancel the typing task for the correlation_id.

    A mock task pre-registered in _typing_tasks must have cancel() called and
    the entry must be removed from the dict before channel.send() is called.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    env = _make_envelope(correlation_id="corr-xyz")
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()
    mock_task = MagicMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {"corr-xyz": mock_task}
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._deliver_outgoing_message({"payload": env.to_json()})

    mock_task.cancel.assert_called_once()
    assert "corr-xyz" not in client._typing_tasks
    mock_channel.send.assert_called_once_with(env.content)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delivery_without_matching_typing_task_does_not_crash():
    """_deliver_outgoing_message must not crash when no typing task is registered.

    Handles replays or messages originated before the typing feature was deployed.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    env = _make_envelope(correlation_id="unknown-corr")
    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {}   # no entry for "unknown-corr"
        client.get_channel = MagicMock(return_value=mock_channel)

        # Must not raise
        await client._deliver_outgoing_message({"payload": env.to_json()})

    mock_channel.send.assert_called_once_with(env.content)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_typing_loop_handles_discord_api_error():
    """_typing_loop must exit silently when channel.typing() raises an exception.

    A Discord API error (e.g. deleted channel) must not propagate to the caller.
    """
    import discord as discord_lib
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_channel = MagicMock()
    mock_channel.typing.return_value.__aenter__ = AsyncMock(
        side_effect=discord_lib.HTTPException(MagicMock(status=403), "Forbidden")
    )
    mock_channel.typing.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {"err-corr": MagicMock()}

        # Must not raise
        await client._typing_loop(mock_channel, "err-corr")

    # Task removed from dict in finally block
    assert "err-corr" not in client._typing_tasks


@pytest.mark.asyncio
@pytest.mark.unit
async def test_typing_loop_respects_max_timeout():
    """_typing_loop must stop after _TYPING_MAX_SECONDS even without cancellation.

    Patching _TYPING_MAX_SECONDS to a small value verifies the loop exits after
    the sleep completes and the context manager is exited cleanly.
    """
    import aiguilleur.channels.discord.adapter as adapter_mod
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_channel = MagicMock()
    mock_channel.typing.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_channel.typing.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {"t-corr": MagicMock()}

        with patch.object(adapter_mod, "_TYPING_MAX_SECONDS", 0.01):
            await client._typing_loop(mock_channel, "t-corr")

    mock_channel.typing.assert_called_once()
    assert "t-corr" not in client._typing_tasks


@pytest.mark.asyncio
@pytest.mark.unit
async def test_all_typing_tasks_cancelled_on_close():
    """close() must cancel all pending typing tasks before delegating to super().

    Prevents "task was destroyed but it is pending" warnings on shutdown.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_task_a = MagicMock()
    mock_task_b = MagicMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {"corr-a": mock_task_a, "corr-b": mock_task_b}

        with patch("discord.Client.close", new_callable=AsyncMock):
            await client.close()

    mock_task_a.cancel.assert_called_once()
    mock_task_b.cancel.assert_called_once()
    assert client._typing_tasks == {}


# ---------------------------------------------------------------------------
# _split_discord_message tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_split_discord_message_short_content_returned_as_is():
    """Content at or below the limit must be returned as a single-element list."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    content = "hello world"
    assert _split_discord_message(content, limit=2000) == [content]
    assert _split_discord_message("x" * 2000, limit=2000) == ["x" * 2000]


@pytest.mark.unit
def test_split_discord_message_splits_on_double_newline():
    """Content exceeding the limit splits at the last \\n\\n before the boundary."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    para1 = "A" * 100
    para2 = "B" * 100
    content = para1 + "\n\n" + para2
    # limit=110 → must split at the \n\n
    parts = _split_discord_message(content, limit=110)
    assert len(parts) == 2
    assert parts[0] == para1 + "\n\n"
    assert parts[1] == para2


@pytest.mark.unit
def test_split_discord_message_splits_on_single_newline_when_no_double():
    """Falls back to \\n when no \\n\\n is present before the boundary."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    line1 = "A" * 100
    line2 = "B" * 100
    content = line1 + "\n" + line2
    parts = _split_discord_message(content, limit=110)
    assert len(parts) == 2
    assert parts[0] == line1 + "\n"
    assert parts[1] == line2


@pytest.mark.unit
def test_split_discord_message_splits_on_space_when_no_newline():
    """Falls back to space when no newline separator exists before the boundary."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    word1 = "A" * 100
    word2 = "B" * 100
    content = word1 + " " + word2
    parts = _split_discord_message(content, limit=110)
    assert len(parts) == 2
    assert parts[0] == word1          # no trailing space
    assert parts[1] == word2          # no leading space


@pytest.mark.unit
def test_split_discord_message_hard_cut_when_no_separator():
    """Hard-cuts at the limit when no natural separator is found."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    content = "X" * 2500
    parts = _split_discord_message(content, limit=2000)
    assert len(parts) == 2
    assert parts[0] == "X" * 2000
    assert parts[1] == "X" * 500


@pytest.mark.unit
def test_split_discord_message_three_parts():
    """Content requiring three parts is split correctly."""
    from aiguilleur.channels.discord.adapter import _split_discord_message

    content = "X" * 4500
    parts = _split_discord_message(content, limit=2000)
    assert len(parts) == 3
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts) == content


# ---------------------------------------------------------------------------
# Long message splitting in _deliver_outgoing_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deliver_outgoing_sends_multiple_parts_for_long_message():
    """_deliver_outgoing_message splits content > 2000 chars into multiple sends."""
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    long_content = "A" * 4500
    env = Envelope(
        content=long_content,
        sender_id="discord:42",
        channel="discord",
        session_id="sess-1",
        correlation_id="corr-long",
        metadata={"reply_to": "999"},
    )

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {}
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._deliver_outgoing_message({"payload": env.to_json()})

    assert mock_channel.send.await_count == 3
    all_sent = "".join(call.args[0] for call in mock_channel.send.await_args_list)
    assert all_sent == long_content


# ---------------------------------------------------------------------------
# Progress event handling in _deliver_outgoing_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deliver_outgoing_progress_tool_call_sends_notification():
    """A progress envelope with event='tool_call' triggers a send with event and detail."""
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    env = Envelope(
        content="",
        sender_id="discord:42",
        channel="discord",
        session_id="sess-p",
        correlation_id="corr-p",
        metadata={
            "reply_to": "999",
            "message_type": "progress",
            "progress_event": "tool_call",
            "progress_detail": "web_search",
        },
    )

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {}
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._deliver_outgoing_message({"payload": env.to_json()})

    mock_channel.send.assert_called_once()
    sent_text = mock_channel.send.call_args.args[0]
    assert "tool_call" in sent_text
    assert "web_search" in sent_text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deliver_outgoing_progress_tool_result_sends_notification():
    """A progress envelope with event='tool_result' triggers a send with event and detail."""
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    env = Envelope(
        content="",
        sender_id="discord:42",
        channel="discord",
        session_id="sess-tr",
        correlation_id="corr-tr",
        metadata={
            "reply_to": "999",
            "message_type": "progress",
            "progress_event": "tool_result",
            "progress_detail": "some result",
        },
    )

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {}
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._deliver_outgoing_message({"payload": env.to_json()})

    mock_channel.send.assert_called_once()
    sent_text = mock_channel.send.call_args.args[0]
    assert "tool_result" in sent_text
    assert "some result" in sent_text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deliver_outgoing_progress_does_not_cancel_typing():
    """A progress envelope must NOT cancel the typing indicator task."""
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient as RelaisDiscordClient

    mock_task = MagicMock()
    env = Envelope(
        content="",
        sender_id="discord:42",
        channel="discord",
        session_id="sess-nc",
        correlation_id="corr-nc",
        metadata={
            "reply_to": "999",
            "message_type": "progress",
            "progress_event": "tool_call",
            "progress_detail": "some_tool",
        },
    )

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock()

    with patch.object(RelaisDiscordClient, "__init__", lambda s: None):
        client = RelaisDiscordClient.__new__(RelaisDiscordClient)
        client._typing_tasks = {"corr-nc": mock_task}
        client.get_channel = MagicMock(return_value=mock_channel)

        await client._deliver_outgoing_message({"payload": env.to_json()})

    mock_task.cancel.assert_not_called()
    assert "corr-nc" in client._typing_tasks
