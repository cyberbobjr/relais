"""Unit tests for Discord adapter guard against empty envelope content.

Tests validate:
- _deliver_outgoing_message() skips sending when envelope.content is ''
- _deliver_outgoing_message() skips sending when envelope.content is whitespace-only
- _deliver_outgoing_message() sends normally when envelope.content is non-empty
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(content: str = "Hello") -> MagicMock:
    """Build a minimal Envelope mock.

    Args:
        content: The message content string.

    Returns:
        MagicMock simulating an Envelope with content, correlation_id, metadata.
    """
    env = MagicMock()
    env.content = content
    env.correlation_id = "test-corr-id"
    env.metadata = {"reply_to": "12345"}
    env.sender_id = "discord:9999"
    return env


def _make_discord_client() -> MagicMock:
    """Build a minimal mock of _RelaisDiscordClient.

    Returns:
        MagicMock with _cancel_typing, _resolve_discord_channel, and
        the _deliver_outgoing_message method bound to a real channel mock.
    """
    client = MagicMock()
    client._cancel_typing = MagicMock()

    discord_channel = AsyncMock()
    discord_channel.send = AsyncMock()

    client._resolve_discord_channel = AsyncMock(return_value=discord_channel)
    return client, discord_channel


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_guard_empty_content_skips_send() -> None:
    """_deliver_outgoing_message() must not call channel.send() for empty content.

    When envelope.content == '', the guard should log a warning and return
    without calling Discord's send() API.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient

    envelope = _make_envelope(content="")

    discord_channel = AsyncMock()
    discord_channel.send = AsyncMock()

    with patch.object(
        _RelaisDiscordClient,
        "_resolve_discord_channel",
        new=AsyncMock(return_value=discord_channel),
    ), patch.object(
        _RelaisDiscordClient,
        "_cancel_typing",
        new=MagicMock(),
    ), patch("aiguilleur.channels.discord.adapter.Envelope.from_json", return_value=envelope):
        client = _RelaisDiscordClient.__new__(_RelaisDiscordClient)
        client._typing_tasks = {}

        await _RelaisDiscordClient._deliver_outgoing_message(
            client, {"payload": "{}"}
        )

    discord_channel.send.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_guard_whitespace_content_skips_send() -> None:
    """_deliver_outgoing_message() must not call channel.send() for whitespace content.

    When envelope.content is only whitespace ('   \n'), the guard should
    log a warning and return without calling Discord's send() API.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient

    envelope = _make_envelope(content="   \n")

    discord_channel = AsyncMock()
    discord_channel.send = AsyncMock()

    with patch.object(
        _RelaisDiscordClient,
        "_resolve_discord_channel",
        new=AsyncMock(return_value=discord_channel),
    ), patch.object(
        _RelaisDiscordClient,
        "_cancel_typing",
        new=MagicMock(),
    ), patch("aiguilleur.channels.discord.adapter.Envelope.from_json", return_value=envelope):
        client = _RelaisDiscordClient.__new__(_RelaisDiscordClient)
        client._typing_tasks = {}

        await _RelaisDiscordClient._deliver_outgoing_message(
            client, {"payload": "{}"}
        )

    discord_channel.send.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discord_guard_non_empty_content_sends() -> None:
    """_deliver_outgoing_message() calls channel.send() for non-empty content.

    Verifies the guard does NOT block delivery when content is a real message.
    """
    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient

    envelope = _make_envelope(content="Hello from Atelier!")

    discord_channel = AsyncMock()
    discord_channel.send = AsyncMock()

    with patch.object(
        _RelaisDiscordClient,
        "_resolve_discord_channel",
        new=AsyncMock(return_value=discord_channel),
    ), patch.object(
        _RelaisDiscordClient,
        "_cancel_typing",
        new=MagicMock(),
    ), patch("aiguilleur.channels.discord.adapter.Envelope.from_json", return_value=envelope):
        client = _RelaisDiscordClient.__new__(_RelaisDiscordClient)
        client._typing_tasks = {}

        await _RelaisDiscordClient._deliver_outgoing_message(
            client, {"payload": "{}"}
        )

    discord_channel.send.assert_awaited_once_with("Hello from Atelier!")
