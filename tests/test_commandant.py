"""Tests TDD — Commandant brick.

Tests for commands (parse_command, CommandResult, CommandSpec, COMMAND_REGISTRY,
handle_clear, handle_help)
and Commandant main consumer loop.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope


@pytest.fixture
def sample_envelope() -> Envelope:
    """Envelope typique d'un message /clear venant de Discord."""
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
    """/clear retourne CommandResult(command='clear', args=[])."""
    from commandant.commands import parse_command
    result = parse_command("/clear")
    assert result is not None
    assert result.command == "clear"
    assert result.args == []


@pytest.mark.unit
def test_parse_unknown_command_returns_none():
    """Commande inconnue → None (pas de réponse, pas d'erreur)."""
    from commandant.commands import parse_command
    result = parse_command("/foo")
    assert result is None


@pytest.mark.unit
def test_parse_plain_message_returns_none():
    """Message normal (pas de slash) → None."""
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
    """'/' seul sans nom de commande → None."""
    from commandant.commands import parse_command
    result = parse_command("/")
    assert result is None


@pytest.mark.unit
def test_parse_command_case_insensitive():
    """/CLEAR et /Clear doivent être reconnus."""
    from commandant.commands import parse_command
    assert parse_command("/CLEAR") is not None
    assert parse_command("/Clear") is not None


@pytest.mark.unit
def test_parse_command_strips_whitespace():
    """'  /clear  ' → reconnu (strip avant parsing)."""
    from commandant.commands import parse_command
    result = parse_command("  /clear  ")
    assert result is not None
    assert result.command == "clear"


@pytest.mark.unit
def test_command_result_is_dataclass():
    """`CommandResult` est un dataclass avec .command et .args."""
    from commandant.commands import parse_command
    result = parse_command("/clear")
    assert hasattr(result, "command")
    assert hasattr(result, "args")


@pytest.mark.unit
def test_parse_help_command():
    """/help retourne CommandResult(command='help', args=[])."""
    from commandant.commands import parse_command
    result = parse_command("/help")
    assert result is not None
    assert result.command == "help"
    assert result.args == []


@pytest.mark.unit
def test_parse_command_quoted():
    """"/help" (avec double quotes) est reconnu — contournement Discord."""
    from commandant.commands import parse_command
    result = parse_command('"/help"')
    assert result is not None
    assert result.command == "help"


@pytest.mark.unit
def test_parse_command_quoted_with_whitespace():
    """'  "/clear"  ' → reconnu après strip des espaces et des quotes."""
    from commandant.commands import parse_command
    result = parse_command('  "/clear"  ')
    assert result is not None
    assert result.command == "clear"


@pytest.mark.unit
def test_parse_command_single_quoted():
    """'/help' (avec simple quotes) est reconnu."""
    from commandant.commands import parse_command
    result = parse_command("'/help'")
    assert result is not None
    assert result.command == "help"


@pytest.mark.unit
def test_parse_command_single_quote_not_stripped():
    """Une quote ouvrante seule ne doit pas être dépouillée (format invalide)."""
    from commandant.commands import parse_command
    result = parse_command('"/help')
    assert result is None


@pytest.mark.unit
def test_parse_command_empty_quotes_returns_none():
    """"" (deux quotes vides) → None."""
    from commandant.commands import parse_command
    result = parse_command('""')
    assert result is None


# ---------------------------------------------------------------------------
# Tests commandant/command_parser.py — COMMAND_REGISTRY
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_command_registry_contains_all_commands():
    """COMMAND_REGISTRY doit contenir clear et help."""
    from commandant.commands import COMMAND_REGISTRY
    assert "clear" in COMMAND_REGISTRY
    assert "help" in COMMAND_REGISTRY


@pytest.mark.unit
def test_command_spec_has_name_and_description():
    """Chaque CommandSpec a un .name et .description non vides."""
    from commandant.commands import COMMAND_REGISTRY
    for name, spec in COMMAND_REGISTRY.items():
        assert spec.name == name
        assert spec.description, f"Command '{name}' has empty description"


@pytest.mark.unit
def test_known_commands_derived_from_registry():
    """KNOWN_COMMANDS doit être cohérent avec COMMAND_REGISTRY."""
    from commandant.commands import COMMAND_REGISTRY, KNOWN_COMMANDS
    assert set(KNOWN_COMMANDS) == set(COMMAND_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Tests commandant/handlers.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_publishes_to_memory_request(mock_redis, sample_envelope):
    """handle_clear envoie action='clear' sur relais:memory:request."""
    from commandant.commands import handle_clear
    await handle_clear(sample_envelope, mock_redis)

    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_does_not_publish_confirmation(mock_redis, sample_envelope):
    """handle_clear ne publie PAS de confirmation : c'est Souvenir qui confirme après le vrai nettoyage."""
    from commandant.commands import handle_clear
    await handle_clear(sample_envelope, mock_redis)

    expected_stream = f"relais:messages:outgoing:{sample_envelope.channel}"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert not any(expected_stream in c for c in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_help_publishes_outgoing(mock_redis):
    """handle_help publie exactement un message sur relais:messages:outgoing:{channel}."""
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
    """La réponse de /help contient tous les noms de commandes du registre."""
    from commandant.commands import handle_help
    from commandant.commands import COMMAND_REGISTRY
    envelope = Envelope(
        content="/help",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
    )
    await handle_help(envelope, mock_redis)

    # Extraire le JSON envoyé sur le stream outgoing
    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "relais:messages:outgoing" in str(c)]
    assert outgoing_calls, "Aucun message publié sur outgoing"
    payload_arg = outgoing_calls[0].args[1]  # {"payload": "<json>"}
    response_envelope = Envelope.from_json(payload_arg["payload"])

    for name in COMMAND_REGISTRY:
        assert f"/{name}" in response_envelope.content, (
            f"/{name} absent de la réponse /help: {response_envelope.content!r}"
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_help_includes_descriptions(mock_redis):
    """La réponse de /help contient les descriptions de chaque commande."""
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
            f"Description de '{name}' absente de la réponse /help"
        )


# ---------------------------------------------------------------------------
# Tests commandant/main.py (boucle consumer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
async def test_commandant_acks_non_command_messages(mock_redis):
    """Messages non-commandes → ACK sans traitement (pas de xadd vers outgoing)."""
    from commandant.main import Commandant

    mock_redis.xreadgroup = AsyncMock(return_value=[
        (b"relais:messages:incoming", [(b"1-1", {b"payload": json.dumps({
            "content": "bonjour",
            "sender_id": "discord:999",
            "channel": "discord",
            "session_id": "s1",
            "correlation_id": "c1",
            "timestamp": 0.0,
            "metadata": {},
            "media_refs": [],
        }).encode()})])
    ])

    commandant = Commandant()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await commandant._process_stream(mock_redis, shutdown=shutdown)

    mock_redis.xack.assert_called_once()
    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "outgoing" in str(c)]
    assert len(outgoing_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_commandant_acks_command_messages(mock_redis):
    """Messages-commandes → ACK + xadd memory:request (confirmation déléguée à Souvenir)."""
    from commandant.main import Commandant

    mock_redis.xreadgroup = AsyncMock(return_value=[
        (b"relais:messages:incoming", [(b"1-1", {b"payload": json.dumps({
            "content": "/clear",
            "sender_id": "discord:999",
            "channel": "discord",
            "session_id": "s1",
            "correlation_id": "c1",
            "timestamp": 0.0,
            "metadata": {},
            "media_refs": [],
        }).encode()})])
    ])

    commandant = Commandant()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    await commandant._process_stream(mock_redis, shutdown=shutdown)

    mock_redis.xack.assert_called_once()
    all_xadd_streams = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in s for s in all_xadd_streams)
    assert not any("relais:messages:outgoing:discord" in s for s in all_xadd_streams)
