"""Tests TDD — Commandant brick.

Tests for commands (parse_command, CommandResult, CommandSpec, COMMAND_REGISTRY,
handle_clear, handle_help)
and Commandant handler dispatch.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope


@pytest.fixture
def sample_envelope() -> Envelope:
    """Typical Envelope for a /clear message from Discord."""
    return Envelope(
        content="/clear",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
        correlation_id="corr_001",
    )


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xadd = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock()
    redis.xgroup_create = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# Tests commandant/command_parser.py
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_clear_command():
    """/clear must return CommandResult(command='clear', args=[])."""
    from commandant.commands import parse_command
    result = parse_command("/clear")
    assert result is not None
    assert result.command == "clear"
    assert result.args == []


@pytest.mark.unit
def test_parse_unknown_command_returns_none():
    """Unknown command → None (no response, no error)."""
    from commandant.commands import parse_command
    result = parse_command("/foo")
    assert result is None


@pytest.mark.unit
def test_parse_plain_message_returns_none():
    """Normal message (no slash) → None."""
    from commandant.commands import parse_command
    result = parse_command("bonjour")
    assert result is None


@pytest.mark.unit
def test_parse_empty_string_returns_none():
    from commandant.commands import parse_command
    result = parse_command("")
    assert result is None


@pytest.mark.unit
def test_parse_slash_only_returns_none():
    """'/' alone without a command name → None."""
    from commandant.commands import parse_command
    result = parse_command("/")
    assert result is None


@pytest.mark.unit
def test_parse_command_case_insensitive():
    """/CLEAR and /Clear must be recognised."""
    from commandant.commands import parse_command
    assert parse_command("/CLEAR") is not None
    assert parse_command("/Clear") is not None


@pytest.mark.unit
def test_parse_command_strips_whitespace():
    """'  /clear  ' → recognised (stripped before parsing)."""
    from commandant.commands import parse_command
    result = parse_command("  /clear  ")
    assert result is not None
    assert result.command == "clear"


@pytest.mark.unit
def test_command_result_is_dataclass():
    """`CommandResult` is a dataclass with .command and .args."""
    from commandant.commands import parse_command
    result = parse_command("/clear")
    assert hasattr(result, "command")
    assert hasattr(result, "args")


@pytest.mark.unit
def test_parse_help_command():
    """/help must return CommandResult(command='help', args=[])."""
    from commandant.commands import parse_command
    result = parse_command("/help")
    assert result is not None
    assert result.command == "help"
    assert result.args == []


@pytest.mark.unit
def test_parse_command_quoted():
    """"/help" (with double quotes) must be recognised — Discord workaround."""
    from commandant.commands import parse_command
    result = parse_command('"/help"')
    assert result is not None
    assert result.command == "help"


@pytest.mark.unit
def test_parse_command_quoted_with_whitespace():
    """'  "/clear"  ' → recognised after stripping whitespace and quotes."""
    from commandant.commands import parse_command
    result = parse_command('  "/clear"  ')
    assert result is not None
    assert result.command == "clear"


@pytest.mark.unit
def test_parse_command_single_quoted():
    """'/help' (with single quotes) must be recognised."""
    from commandant.commands import parse_command
    result = parse_command("'/help'")
    assert result is not None
    assert result.command == "help"


@pytest.mark.unit
def test_parse_command_single_quote_not_stripped():
    """A lone opening quote must not be stripped (invalid format)."""
    from commandant.commands import parse_command
    result = parse_command('"/help')
    assert result is None


@pytest.mark.unit
def test_parse_command_empty_quotes_returns_none():
    """"" (two empty quotes) → None."""
    from commandant.commands import parse_command
    result = parse_command('""')
    assert result is None


# ---------------------------------------------------------------------------
# Tests commandant/command_parser.py — COMMAND_REGISTRY
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_command_registry_contains_all_commands():
    """COMMAND_REGISTRY must contain clear and help."""
    from commandant.commands import COMMAND_REGISTRY
    assert "clear" in COMMAND_REGISTRY
    assert "help" in COMMAND_REGISTRY


@pytest.mark.unit
def test_command_spec_has_name_and_description():
    """Each CommandSpec must have a non-empty .name and .description."""
    from commandant.commands import COMMAND_REGISTRY
    for name, spec in COMMAND_REGISTRY.items():
        assert spec.name == name
        assert spec.description, f"Command '{name}' has empty description"


@pytest.mark.unit
def test_known_commands_derived_from_registry():
    """KNOWN_COMMANDS must be consistent with COMMAND_REGISTRY."""
    from commandant.commands import COMMAND_REGISTRY, KNOWN_COMMANDS
    assert set(KNOWN_COMMANDS) == set(COMMAND_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Tests commandant/handlers.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_publishes_to_memory_request(mock_redis, sample_envelope):
    """handle_clear must publish action='clear' to relais:memory:request."""
    from commandant.commands import handle_clear
    await handle_clear(sample_envelope, mock_redis)

    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_does_not_publish_confirmation(mock_redis, sample_envelope):
    """handle_clear must NOT publish a confirmation: Souvenir confirms after the actual cleanup."""
    from commandant.commands import handle_clear
    await handle_clear(sample_envelope, mock_redis)

    expected_stream = f"relais:messages:outgoing:{sample_envelope.channel}"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert not any(expected_stream in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_help_publishes_outgoing(mock_redis):
    """handle_help must publish exactly one message to relais:messages:outgoing:{channel}."""
    from commandant.commands import handle_help
    envelope = Envelope(
        content="/help",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
    )
    await handle_help(envelope, mock_redis)

    expected_stream = "relais:messages:outgoing:discord"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any(expected_stream in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_help_lists_all_command_names(mock_redis):
    """The /help response must contain all command names from the registry."""
    from commandant.commands import handle_help
    from commandant.commands import COMMAND_REGISTRY
    envelope = Envelope(
        content="/help",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
    )
    await handle_help(envelope, mock_redis)

    # Extract the JSON sent to the outgoing stream
    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:messages:outgoing" in str(c)]
    assert outgoing_calls, "No message published to outgoing"
    payload_arg = outgoing_calls[0].args[1]  # {"payload": "<json>"}
    response_envelope = Envelope.from_json(payload_arg["payload"])

    for name in COMMAND_REGISTRY:
        assert f"/{name}" in response_envelope.content, (
            f"/{name} missing from /help response: {response_envelope.content!r}"
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_help_includes_descriptions(mock_redis):
    """The /help response must contain the description of each command."""
    from commandant.commands import handle_help
    from commandant.commands import COMMAND_REGISTRY
    envelope = Envelope(
        content="/help",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
    )
    await handle_help(envelope, mock_redis)

    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:messages:outgoing" in str(c)]
    payload_arg = outgoing_calls[0].args[1]
    response_envelope = Envelope.from_json(payload_arg["payload"])

    for name, spec in COMMAND_REGISTRY.items():
        assert spec.description in response_envelope.content, (
            f"Description of '{name}' missing from /help response"
        )


# ---------------------------------------------------------------------------
# Tests commandant/main.py — _handle dispatch (BrickBase handler)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_commandant_inherits_brick_base():
    """Commandant must inherit from BrickBase."""
    from commandant.main import Commandant
    from common.brick_base import BrickBase
    assert issubclass(Commandant, BrickBase)


@pytest.mark.unit
def test_commandant_stream_spec():
    """stream_specs must return a single spec for relais:commands."""
    from commandant.main import Commandant
    c = Commandant()
    specs = c.stream_specs()
    assert len(specs) == 1
    assert specs[0].stream == "relais:commands"
    assert specs[0].group == "commandant_group"
    assert specs[0].consumer == "commandant_1"
    assert specs[0].ack_mode == "always"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_non_command_returns_true(mock_redis):
    """Non-command messages → _handle returns True (ACK), no xadd to outgoing."""
    from commandant.main import Commandant
    commandant = Commandant()

    envelope = Envelope(
        content="bonjour",
        sender_id="discord:999",
        channel="discord",
        session_id="s1",
        correlation_id="c1",
    )

    result = await commandant._handle(envelope, mock_redis)
    assert result is True

    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "outgoing" in str(c)]
    assert len(outgoing_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_command_dispatches(mock_redis):
    """Command /clear → _handle dispatches to handler and returns True."""
    from commandant.main import Commandant
    commandant = Commandant()

    envelope = Envelope(
        content="/clear",
        sender_id="discord:999",
        channel="discord",
        session_id="s1",
        correlation_id="c1",
    )

    result = await commandant._handle(envelope, mock_redis)
    assert result is True

    all_xadd_streams = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in s for s in all_xadd_streams)
    assert not any("relais:messages:outgoing:discord" in s for s in all_xadd_streams)
