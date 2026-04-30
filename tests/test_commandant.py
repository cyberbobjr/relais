"""Tests TDD — Commandant brick.

Tests for commands (parse_command, CommandResult, CommandSpec, COMMAND_REGISTRY,
handle_clear, handle_help)
and Commandant handler dispatch.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_COMMAND


@pytest.fixture
def sample_envelope() -> Envelope:
    """Typical Envelope for a /clear message from Discord."""
    return Envelope(
        content="/clear",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
        correlation_id="corr_001",
        action=ACTION_MESSAGE_COMMAND,
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
        action=ACTION_MESSAGE_COMMAND,
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
        action=ACTION_MESSAGE_COMMAND,
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
        action=ACTION_MESSAGE_COMMAND,
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
    """stream_specs must return the first spec for relais:commands."""
    from commandant.main import Commandant
    c = Commandant()
    specs = c.stream_specs()
    assert len(specs) == 2
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
        action=ACTION_MESSAGE_COMMAND,
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
        action=ACTION_MESSAGE_COMMAND,
    )

    result = await commandant._handle(envelope, mock_redis)
    assert result is True

    all_xadd_streams = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in s for s in all_xadd_streams)
    assert not any("relais:messages:outgoing:discord" in s for s in all_xadd_streams)


# ---------------------------------------------------------------------------
# Tests handle_sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_sessions_sends_memory_request(mock_redis, sample_envelope):
    """handle_sessions must publish to relais:memory:request."""
    from commandant.commands import handle_sessions
    await handle_sessions(sample_envelope, mock_redis)

    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_sessions_publishes_correct_action(mock_redis, sample_envelope):
    """handle_sessions payload must carry action=ACTION_MEMORY_SESSIONS."""
    from commandant.commands import handle_sessions
    from common.envelope_actions import ACTION_MEMORY_SESSIONS
    await handle_sessions(sample_envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    assert memory_calls, "No xadd call to relais:memory:request"
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert sent_env.action == ACTION_MEMORY_SESSIONS


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_sessions_includes_user_id(mock_redis):
    """handle_sessions must include user_id from CTX_PORTAIL in CTX_SOUVENIR_REQUEST."""
    from commandant.commands import handle_sessions
    from common.contexts import CTX_PORTAIL, CTX_SOUVENIR_REQUEST
    from common.envelope_actions import ACTION_MESSAGE_COMMAND

    envelope = Envelope(
        content="/sessions",
        sender_id="discord:42",
        channel="discord",
        session_id="s1",
        correlation_id="c1",
        action=ACTION_MESSAGE_COMMAND,
        context={CTX_PORTAIL: {"user_id": "usr_alice"}},
    )
    await handle_sessions(envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert sent_env.context[CTX_SOUVENIR_REQUEST]["user_id"] == "usr_alice"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_sessions_falls_back_to_sender_id_when_no_portail(mock_redis):
    """handle_sessions must fall back to envelope.sender_id when CTX_PORTAIL is absent."""
    from commandant.commands import handle_sessions
    from common.contexts import CTX_SOUVENIR_REQUEST
    from common.envelope_actions import ACTION_MESSAGE_COMMAND

    envelope = Envelope(
        content="/sessions",
        sender_id="telegram:99",
        channel="telegram",
        session_id="s2",
        correlation_id="c2",
        action=ACTION_MESSAGE_COMMAND,
    )
    await handle_sessions(envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert sent_env.context[CTX_SOUVENIR_REQUEST]["user_id"] == "telegram:99"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_sessions_includes_envelope_json(mock_redis, sample_envelope):
    """handle_sessions CTX_SOUVENIR_REQUEST must include envelope_json key."""
    from commandant.commands import handle_sessions
    from common.contexts import CTX_SOUVENIR_REQUEST
    await handle_sessions(sample_envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    souvenir_ctx = sent_env.context[CTX_SOUVENIR_REQUEST]
    assert "envelope_json" in souvenir_ctx
    # envelope_json must be valid JSON
    parsed = json.loads(souvenir_ctx["envelope_json"])
    assert parsed["sender_id"] == sample_envelope.sender_id


# ---------------------------------------------------------------------------
# Tests handle_resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_resume_with_session_id(mock_redis):
    """handle_resume with a session_id must publish ACTION_MEMORY_RESUME to relais:memory:request."""
    from commandant.commands import handle_resume
    from common.contexts import CTX_SOUVENIR_REQUEST
    from common.envelope_actions import ACTION_MESSAGE_COMMAND, ACTION_MEMORY_RESUME

    envelope = Envelope(
        content="/resume sess_xyz",
        sender_id="discord:77",
        channel="discord",
        session_id="s3",
        correlation_id="c3",
        action=ACTION_MESSAGE_COMMAND,
        context={},
    )
    await handle_resume(envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    assert memory_calls, "Expected xadd on relais:memory:request"
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert sent_env.action == ACTION_MEMORY_RESUME
    assert sent_env.context[CTX_SOUVENIR_REQUEST]["target_session_id"] == "sess_xyz"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_resume_includes_user_id(mock_redis):
    """handle_resume must include user_id from CTX_PORTAIL in CTX_SOUVENIR_REQUEST."""
    from commandant.commands import handle_resume
    from common.contexts import CTX_PORTAIL, CTX_SOUVENIR_REQUEST
    from common.envelope_actions import ACTION_MESSAGE_COMMAND

    envelope = Envelope(
        content="/resume sess_xyz",
        sender_id="discord:77",
        channel="discord",
        session_id="s3",
        correlation_id="c3",
        action=ACTION_MESSAGE_COMMAND,
        context={CTX_PORTAIL: {"user_id": "usr_bob"}},
    )
    await handle_resume(envelope, mock_redis)

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    payload_arg = memory_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert sent_env.context[CTX_SOUVENIR_REQUEST]["user_id"] == "usr_bob"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_resume_without_session_id(mock_redis):
    """/resume with no argument must send a usage message to the outgoing stream."""
    from commandant.commands import handle_resume
    from common.envelope_actions import ACTION_MESSAGE_COMMAND

    envelope = Envelope(
        content="/resume ",
        sender_id="discord:77",
        channel="discord",
        session_id="s4",
        correlation_id="c4",
        action=ACTION_MESSAGE_COMMAND,
    )
    await handle_resume(envelope, mock_redis)

    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:messages:outgoing:discord" in str(c)]
    assert outgoing_calls, "Expected a usage error message on the outgoing stream"

    memory_calls = [c for c in mock_redis.xadd.call_args_list
                    if "relais:memory:request" in str(c)]
    assert not memory_calls, "Should NOT publish to memory:request when no session_id"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_resume_without_session_id_content(mock_redis):
    """/resume with no argument must include 'Usage' in the reply content."""
    from commandant.commands import handle_resume
    from common.envelope_actions import ACTION_MESSAGE_COMMAND

    envelope = Envelope(
        content="/resume",
        sender_id="discord:77",
        channel="discord",
        session_id="s4",
        correlation_id="c4",
        action=ACTION_MESSAGE_COMMAND,
    )
    await handle_resume(envelope, mock_redis)

    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:messages:outgoing:discord" in str(c)]
    payload_arg = outgoing_calls[0].args[1]
    sent_env = Envelope.from_json(payload_arg["payload"])
    assert "Usage" in sent_env.content or "usage" in sent_env.content.lower()


# ---------------------------------------------------------------------------
# Tests parse_command — sessions & resume
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_command_sessions():
    """/sessions → CommandResult(command='sessions', args=[])."""
    from commandant.commands import parse_command
    result = parse_command("/sessions")
    assert result is not None
    assert result.command == "sessions"
    assert result.args == []


@pytest.mark.unit
def test_parse_command_resume_with_arg():
    """/resume abc123 → CommandResult(command='resume', args=['abc123'])."""
    from commandant.commands import parse_command
    result = parse_command("/resume abc123")
    assert result is not None
    assert result.command == "resume"
    assert result.args == ["abc123"]


@pytest.mark.unit
def test_known_commands_includes_sessions_and_resume():
    """KNOWN_COMMANDS must include 'sessions' and 'resume'."""
    from commandant.commands import KNOWN_COMMANDS
    assert "sessions" in KNOWN_COMMANDS
    assert "resume" in KNOWN_COMMANDS


# ---------------------------------------------------------------------------
# Tests _handle_catalog_query (CQRS catalog endpoint)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_second_stream_spec_is_catalogquery_stream():
    """stream_specs()[1] must be the catalog query stream with the right group/consumer."""
    from commandant.main import Commandant
    from common.streams import STREAM_COMMANDANT_QUERY
    c = Commandant()
    specs = c.stream_specs()
    assert len(specs) == 2
    assert specs[1].stream == STREAM_COMMANDANT_QUERY
    assert specs[1].group == "commandant_catalog_group"
    assert specs[1].consumer == "commandant_catalog_1"
    assert specs[1].ack_mode == "always"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_catalog_query_responds_with_catalog(mock_redis):
    """_handle_catalog_query must LPUSH a JSON payload with a 'commands' list to the per-request key."""
    from commandant.main import Commandant
    from common.streams import key_commandant_catalog
    from common.envelope_actions import ACTION_CATALOG_QUERY

    corr_id = "test-corr-001"
    envelope = Envelope(
        content="catalog_query",
        sender_id="rest:anonymous",
        channel="rest",
        session_id=corr_id,
        correlation_id=corr_id,
        action=ACTION_CATALOG_QUERY,
    )
    commandant = Commandant()
    result = await commandant._handle_catalog_query(envelope, mock_redis)

    assert result is True
    expected_key = key_commandant_catalog(corr_id)
    mock_redis.lpush.assert_called_once()
    call_args = mock_redis.lpush.call_args
    assert call_args.args[0] == expected_key
    payload = json.loads(call_args.args[1])
    assert "commands" in payload
    assert isinstance(payload["commands"], list)
    assert len(payload["commands"]) > 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_catalog_sorted_alphabetically(mock_redis):
    """_handle_catalog_query must return commands sorted alphabetically by name."""
    from commandant.main import Commandant
    from common.envelope_actions import ACTION_CATALOG_QUERY

    envelope = Envelope(
        content="catalog_query",
        sender_id="rest:anonymous",
        channel="rest",
        session_id="corr-alpha",
        correlation_id="corr-alpha",
        action=ACTION_CATALOG_QUERY,
    )
    commandant = Commandant()
    await commandant._handle_catalog_query(envelope, mock_redis)

    payload = json.loads(mock_redis.lpush.call_args.args[1])
    names = [cmd["name"] for cmd in payload["commands"]]
    assert names == sorted(names), f"Commands not sorted: {names}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_catalog_no_handler_field(mock_redis):
    """_handle_catalog_query must NOT expose 'handler' callables in the catalog items."""
    from commandant.main import Commandant
    from common.envelope_actions import ACTION_CATALOG_QUERY

    envelope = Envelope(
        content="catalog_query",
        sender_id="rest:anonymous",
        channel="rest",
        session_id="corr-handler",
        correlation_id="corr-handler",
        action=ACTION_CATALOG_QUERY,
    )
    commandant = Commandant()
    await commandant._handle_catalog_query(envelope, mock_redis)

    payload = json.loads(mock_redis.lpush.call_args.args[1])
    for item in payload["commands"]:
        assert "handler" not in item, (
            f"Command '{item.get('name')}' exposes internal 'handler' field"
        )
