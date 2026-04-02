"""Tests TDD — Commandant new stream: relais:commands (Phase 4 RED).

Verifies that after the migration:
- Commandant.stream_in == "relais:commands"
- Consumer group is created on relais:commands
- Commands are dispatched correctly from the new stream
- Non-command payloads on relais:commands are still ACKed without error
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope


def _make_payload(content: str, channel: str = "discord") -> bytes:
    return json.dumps({
        "content": content,
        "sender_id": "discord:admin001",
        "channel": channel,
        "session_id": "sess-001",
        "correlation_id": "corr-001",
        "timestamp": 0.0,
        "metadata": {},
        "media_refs": [],
    }).encode()


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


@pytest.mark.unit
class TestCommandantNewStream:
    """Commandant.stream_in must be 'relais:commands'."""

    def test_commandant_stream_in_is_relais_commands(self) -> None:
        """After migration, stream_in == 'relais:commands'."""
        from commandant.main import Commandant
        c = Commandant()
        assert c.stream_in == "relais:commands"

    def test_commandant_group_name_unchanged(self) -> None:
        """Consumer group name must remain 'commandant_group'."""
        from commandant.main import Commandant
        c = Commandant()
        assert c.group_name == "commandant_group"


@pytest.mark.unit
@pytest.mark.asyncio
class TestCommandantConsumesFromCommandsStream:
    """Commandant processes messages from relais:commands correctly."""

    async def test_clear_command_dispatched_from_commands_stream(
        self, mock_redis: AsyncMock
    ) -> None:
        """/clear arriving on relais:commands triggers memory:request."""
        from commandant.main import Commandant

        mock_redis.xreadgroup = AsyncMock(return_value=[
            (b"relais:commands", [(b"1-1", {b"payload": _make_payload("/clear")})])
        ])

        commandant = Commandant()
        shutdown = MagicMock()
        shutdown.is_stopping.side_effect = [False, True]

        await commandant._process_stream(mock_redis, shutdown=shutdown)

        mock_redis.xack.assert_called_once()
        streams = [str(c) for c in mock_redis.xadd.call_args_list]
        assert any("relais:memory:request" in s for s in streams)

    async def test_consumer_group_created_on_relais_commands(
        self, mock_redis: AsyncMock
    ) -> None:
        """Consumer group is created on relais:commands at startup."""
        from commandant.main import Commandant

        commandant = Commandant()
        shutdown = MagicMock()
        shutdown.is_stopping.return_value = True  # exit immediately

        await commandant._process_stream(mock_redis, shutdown=shutdown)

        mock_redis.xgroup_create.assert_called_once()
        call_args = mock_redis.xgroup_create.call_args
        assert call_args.args[0] == "relais:commands"

    async def test_help_command_dispatched_from_commands_stream(
        self, mock_redis: AsyncMock
    ) -> None:
        """/help arriving on relais:commands → outgoing reply."""
        from commandant.main import Commandant

        mock_redis.xreadgroup = AsyncMock(return_value=[
            (b"relais:commands", [(b"1-1", {b"payload": _make_payload("/help")})])
        ])

        commandant = Commandant()
        shutdown = MagicMock()
        shutdown.is_stopping.side_effect = [False, True]

        await commandant._process_stream(mock_redis, shutdown=shutdown)

        mock_redis.xack.assert_called_once()
        streams = [str(c) for c in mock_redis.xadd.call_args_list]
        assert any("relais:messages:outgoing:discord" in s for s in streams)

    async def test_unknown_command_on_commands_stream_not_replied(
        self, mock_redis: AsyncMock
    ) -> None:
        """/foobar on relais:commands: Commandant no longer handles unknown commands.

        Unknown commands are rejected by Sentinelle before reaching relais:commands,
        so Commandant should simply ACK without sending any outgoing reply.
        """
        from commandant.main import Commandant

        mock_redis.xreadgroup = AsyncMock(return_value=[
            (b"relais:commands", [(b"1-1", {b"payload": _make_payload("/foobar")})])
        ])

        commandant = Commandant()
        shutdown = MagicMock()
        shutdown.is_stopping.side_effect = [False, True]

        await commandant._process_stream(mock_redis, shutdown=shutdown)

        mock_redis.xack.assert_called_once()
        outgoing = [c for c in mock_redis.xadd.call_args_list
                    if "relais:messages:outgoing" in str(c)]
        assert len(outgoing) == 0
