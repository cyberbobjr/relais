"""Tests TDD — Commandant new stream: relais:commands (Phase 4 RED).

Verifies that after the migration:
- Commandant stream_specs targets "relais:commands"
- Commands are dispatched correctly via _handle
- Non-command payloads are handled without error
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_COMMAND


def _make_envelope(content: str, channel: str = "discord") -> Envelope:
    """Build a test Envelope with the given content."""
    return Envelope(
        content=content,
        sender_id="discord:admin001",
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        action=ACTION_MESSAGE_COMMAND,
    )


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
    """Commandant stream_specs must target 'relais:commands'."""

    def test_commandant_stream_is_relais_commands(self) -> None:
        """After migration, stream spec targets 'relais:commands'."""
        from commandant.main import Commandant
        c = Commandant()
        specs = c.stream_specs()
        assert specs[0].stream == "relais:commands"

    def test_commandant_group_name_unchanged(self) -> None:
        """Consumer group name must remain 'commandant_group'."""
        from commandant.main import Commandant
        c = Commandant()
        specs = c.stream_specs()
        assert specs[0].group == "commandant_group"


@pytest.mark.unit
@pytest.mark.asyncio
class TestCommandantConsumesFromCommandsStream:
    """Commandant processes messages from relais:commands correctly."""

    async def test_clear_command_dispatched_from_commands_stream(
        self, mock_redis: AsyncMock
    ) -> None:
        """/clear arriving on relais:commands triggers memory:request."""
        from commandant.main import Commandant

        commandant = Commandant()
        envelope = _make_envelope("/clear")

        result = await commandant._handle(envelope, mock_redis)
        assert result is True

        streams = [str(c) for c in mock_redis.xadd.call_args_list]
        assert any("relais:memory:request" in s for s in streams)

    async def test_help_command_dispatched_from_commands_stream(
        self, mock_redis: AsyncMock
    ) -> None:
        """/help arriving on relais:commands → outgoing reply."""
        from commandant.main import Commandant

        commandant = Commandant()
        envelope = _make_envelope("/help")

        result = await commandant._handle(envelope, mock_redis)
        assert result is True

        streams = [str(c) for c in mock_redis.xadd.call_args_list]
        assert any("relais:messages:outgoing:discord" in s for s in streams)

    async def test_unknown_command_on_commands_stream_not_replied(
        self, mock_redis: AsyncMock
    ) -> None:
        """/foobar on relais:commands: Commandant no longer handles unknown commands.

        Unknown commands are rejected by Sentinelle before reaching relais:commands,
        so Commandant should simply return True without sending any outgoing reply.
        """
        from commandant.main import Commandant

        commandant = Commandant()
        envelope = _make_envelope("/foobar")

        result = await commandant._handle(envelope, mock_redis)
        assert result is True

        outgoing = [c for c in mock_redis.xadd.call_args_list
                    if "relais:messages:outgoing" in str(c)]
        assert len(outgoing) == 0
