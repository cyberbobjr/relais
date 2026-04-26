"""Tests TDD — /compact command handler.

RED phase: these tests define the expected behaviour of handle_compact()
before the implementation exists in commandant/commands.py.
"""
import json
import pytest
from unittest.mock import AsyncMock

from common.contexts import CTX_PORTAIL, CTX_ATELIER_CONTROL
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_COMMAND, ACTION_ATELIER_COMPACT
from common.streams import STREAM_ATELIER_CONTROL


@pytest.fixture
def compact_envelope() -> Envelope:
    """Typical Envelope for a /compact message from Discord."""
    return Envelope(
        content="/compact",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
        correlation_id="corr_001",
        action=ACTION_MESSAGE_COMMAND,
        context={
            CTX_PORTAIL: {
                "user_id": "usr_admin",
                "user_record": {"display_name": "Admin"},
                "llm_profile": "default",
            }
        },
    )


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xadd = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# parse_command — compact is registered as a known command
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_compact_command():
    """/compact must be a known command and return CommandResult(command='compact')."""
    from commandant.commands import parse_command

    result = parse_command("/compact")
    assert result is not None
    assert result.command == "compact"
    assert result.args == []


@pytest.mark.unit
def test_compact_in_known_commands():
    """KNOWN_COMMANDS must include 'compact'."""
    from commandant.commands import KNOWN_COMMANDS

    assert "compact" in KNOWN_COMMANDS


@pytest.mark.unit
def test_compact_in_command_registry():
    """COMMAND_REGISTRY must have an entry for 'compact'."""
    from commandant.commands import COMMAND_REGISTRY

    assert "compact" in COMMAND_REGISTRY
    spec = COMMAND_REGISTRY["compact"]
    assert spec.name == "compact"
    assert spec.handler is not None


# ---------------------------------------------------------------------------
# handle_compact — publishes to relais:atelier:control
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_compact_publishes_to_control_stream(
    compact_envelope: Envelope, mock_redis: AsyncMock
) -> None:
    """handle_compact() must xadd one message to STREAM_ATELIER_CONTROL."""
    from commandant.commands import handle_compact

    await handle_compact(compact_envelope, mock_redis)

    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    stream_name = call_args[0][0]
    assert stream_name == STREAM_ATELIER_CONTROL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_compact_envelope_action(
    compact_envelope: Envelope, mock_redis: AsyncMock
) -> None:
    """The published envelope must have action=ACTION_ATELIER_COMPACT."""
    from commandant.commands import handle_compact

    await handle_compact(compact_envelope, mock_redis)

    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = json.loads(payload_json)
    assert published["action"] == ACTION_ATELIER_COMPACT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_compact_control_ctx_fields(
    compact_envelope: Envelope, mock_redis: AsyncMock
) -> None:
    """The published envelope must carry atelier_control ctx with op, user_id, envelope_json."""
    from commandant.commands import handle_compact

    await handle_compact(compact_envelope, mock_redis)

    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = json.loads(payload_json)
    ctx = published["context"][CTX_ATELIER_CONTROL]
    assert ctx["op"] == "compact"
    assert ctx["user_id"] == "usr_admin"
    assert "envelope_json" in ctx


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_compact_falls_back_to_sender_id_when_no_portail_ctx(
    mock_redis: AsyncMock,
) -> None:
    """When CTX_PORTAIL is absent, user_id falls back to envelope.sender_id."""
    from commandant.commands import handle_compact

    envelope = Envelope(
        content="/compact",
        sender_id="telegram:789",
        channel="telegram",
        session_id="sess_x",
        correlation_id="corr_x",
        action=ACTION_MESSAGE_COMMAND,
    )
    await handle_compact(envelope, mock_redis)

    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = json.loads(payload_json)
    ctx = published["context"][CTX_ATELIER_CONTROL]
    assert ctx["user_id"] == "telegram:789"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_compact_preserves_session_and_channel(
    compact_envelope: Envelope, mock_redis: AsyncMock
) -> None:
    """The published envelope must preserve session_id and channel from the original."""
    from commandant.commands import handle_compact

    await handle_compact(compact_envelope, mock_redis)

    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = json.loads(payload_json)
    assert published["session_id"] == compact_envelope.session_id
    assert published["channel"] == compact_envelope.channel
