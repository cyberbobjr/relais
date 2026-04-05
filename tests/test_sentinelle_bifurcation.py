"""Tests TDD — Sentinelle command bifurcation with user_record in envelope.

Verifies the new routing logic:
- Regular messages → relais:tasks (unchanged)
- Known + authorised commands → relais:commands
- Unknown commands → inline reply to relais:messages:outgoing:{channel}
- Known but unauthorised commands → inline reply with permission denied
- Fail-closed: envelope without user_record → deny
"""
import asyncio
import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from common.envelope import Envelope
from common.user_record import UserRecord
from sentinelle.acl import ACLManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sentinelle_yaml(tmp_path: Path) -> Path:
    """Write a sentinelle.yaml with allowlist mode (no users, no roles).

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the written file.
    """
    content = dedent("""\
        access_control:
          default_mode: allowlist
        groups: []
    """)
    p = tmp_path / "sentinelle.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_admin_record() -> UserRecord:
    """Build an admin UserRecord with wildcard actions.

    Returns:
        Fully configured admin UserRecord.
    """
    return UserRecord(
        user_id="usr_admin",
        display_name="Admin",
        role="admin",
        blocked=False,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        allowed_subagents=[],
        prompt_path=None,
    )


def _make_user_record() -> UserRecord:
    """Build a regular user UserRecord with no commands allowed.

    Returns:
        Configured user UserRecord.
    """
    return UserRecord(
        user_id="usr_user",
        display_name="User",
        role="user",
        blocked=False,
        actions=[],
        skills_dirs=[],
        allowed_mcp_tools=[],
        allowed_subagents=[],
        prompt_path=None,
    )


def _make_envelope(
    content: str,
    sender_id: str = "discord:admin001",
    channel: str = "discord",
    user_record: UserRecord | None = None,
) -> Envelope:
    """Build a test Envelope with user_record pre-stamped in metadata.

    Args:
        content: Message content (may start with '/' for commands).
        sender_id: Originating user identifier.
        channel: Originating channel.
        user_record: Pre-stamped UserRecord. Defaults to admin record.

    Returns:
        A valid Envelope with user_record in metadata.
    """
    if user_record is None:
        user_record = _make_admin_record()
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        timestamp=0.0,
        metadata={"user_record": user_record.to_dict()},
        media_refs=[],
    )


def _make_redis(envelope: Envelope) -> AsyncMock:
    """Return a mock Redis with the envelope pre-loaded in xreadgroup.

    Args:
        envelope: The envelope to return from xreadgroup.

    Returns:
        Configured AsyncMock Redis client.
    """
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(side_effect=[
        [("relais:security", [(b"1-0", {"payload": envelope.to_json()})])],
        asyncio.CancelledError(),
    ])
    redis.xadd = AsyncMock(return_value=b"2-0")
    redis.xack = AsyncMock(return_value=1)
    return redis


# ---------------------------------------------------------------------------
# Normal message routing (unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleNormalMessageRouting:
    """Normal messages (no slash) are still forwarded to relais:tasks."""

    async def test_normal_message_goes_to_tasks(self, tmp_path: Path) -> None:
        """Non-command message from authorised user → relais:tasks."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("bonjour", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        tasks_calls = [c for c in redis.xadd.await_args_list
                       if c.args[0] == "relais:tasks"]
        assert len(tasks_calls) == 1

    async def test_normal_message_not_sent_to_commands(self, tmp_path: Path) -> None:
        """Non-command message must not be published to relais:commands."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("bonjour", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        commands_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:commands"]
        assert len(commands_calls) == 0


# ---------------------------------------------------------------------------
# Authorised command routing → relais:commands
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleAuthorisedCommandRouting:
    """Known + authorised commands are routed to relais:commands."""

    async def test_authorised_command_goes_to_relais_commands(self, tmp_path: Path) -> None:
        """Admin /clear → published to relais:commands."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        commands_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:commands"]
        assert len(commands_calls) == 1

    async def test_authorised_command_not_sent_to_tasks(self, tmp_path: Path) -> None:
        """Admin /clear must NOT be sent to relais:tasks."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        tasks_calls = [c for c in redis.xadd.await_args_list
                       if c.args[0] == "relais:tasks"]
        assert len(tasks_calls) == 0

    async def test_authorised_command_acked(self, tmp_path: Path) -> None:
        """Message is ACKed after routing to relais:commands."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        redis.xack.assert_awaited_once_with("relais:security", "sentinelle_group", b"1-0")

    async def test_stream_commands_attribute_set(self) -> None:
        """Sentinelle instance must have stream_commands = 'relais:commands'."""
        from sentinelle.main import Sentinelle

        s = Sentinelle.__new__(Sentinelle)
        s.__init__()  # type: ignore[misc]
        assert s.stream_commands == "relais:commands"


# ---------------------------------------------------------------------------
# Unknown command → inline rejection reply
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleUnknownCommandRejection:
    """Unknown commands (not in KNOWN_COMMANDS) get an inline reply."""

    async def test_unknown_command_sends_reply(self, tmp_path: Path) -> None:
        """/foobar (unknown) → reply published to relais:messages:outgoing:{channel}."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/foobar", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        assert len(outgoing_calls) == 1

    async def test_unknown_command_reply_mentions_command_name(self, tmp_path: Path) -> None:
        """Reply for unknown /foobar mentions 'foobar' in its content."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/foobar", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        reply_env = Envelope.from_json(outgoing_calls[0].args[1]["payload"])
        assert "foobar" in reply_env.content.lower()

    async def test_unknown_command_not_sent_to_commands_stream(self, tmp_path: Path) -> None:
        """Unknown command must NOT be forwarded to relais:commands."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/foobar", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        commands_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:commands"]
        assert len(commands_calls) == 0

    async def test_unknown_command_acked(self, tmp_path: Path) -> None:
        """Unknown command message is ACKed after reply is sent."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/foobar", sender_id="discord:admin001")
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        redis.xack.assert_awaited_once_with("relais:security", "sentinelle_group", b"1-0")


# ---------------------------------------------------------------------------
# Unauthorised command → permission-denied reply
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleUnauthorisedCommandRejection:
    """Known but unauthorised commands get a permission-denied reply."""

    async def test_unauthorised_command_sends_permission_denied_reply(
        self, tmp_path: Path
    ) -> None:
        """User /clear (no actions) → permission-denied reply."""
        from sentinelle.main import Sentinelle

        user = _make_user_record()
        env = _make_envelope("/clear", sender_id="discord:user001", user_record=user)
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        assert len(outgoing_calls) == 1

    async def test_unauthorised_command_reply_mentions_command_name(
        self, tmp_path: Path
    ) -> None:
        """Permission-denied reply mentions 'clear' in its content."""
        from sentinelle.main import Sentinelle

        user = _make_user_record()
        env = _make_envelope("/clear", sender_id="discord:user001", user_record=user)
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        reply_env = Envelope.from_json(outgoing_calls[0].args[1]["payload"])
        assert "clear" in reply_env.content.lower()

    async def test_unauthorised_command_not_sent_to_commands_stream(
        self, tmp_path: Path
    ) -> None:
        """Unauthorised known command must NOT be forwarded to relais:commands."""
        from sentinelle.main import Sentinelle

        user = _make_user_record()
        env = _make_envelope("/clear", sender_id="discord:user001", user_record=user)
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        commands_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:commands"]
        assert len(commands_calls) == 0

    async def test_unauthorised_command_acked(self, tmp_path: Path) -> None:
        """Unauthorised command message is ACKed after reply is sent."""
        from sentinelle.main import Sentinelle

        user = _make_user_record()
        env = _make_envelope("/clear", sender_id="discord:user001", user_record=user)
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        redis.xack.assert_awaited_once_with("relais:security", "sentinelle_group", b"1-0")


# ---------------------------------------------------------------------------
# Fail-closed: no user_record in envelope → deny
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleFailClosed:
    """Envelope without user_record is denied (fail-closed)."""

    async def test_missing_user_record_drops_message(self, tmp_path: Path) -> None:
        """Envelope without user_record key is dropped (not forwarded).

        Sentinelle must deny messages that Portail failed to enrich.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.main import Sentinelle

        # Envelope with NO user_record in metadata
        env = Envelope(
            content="bonjour",
            sender_id="discord:admin001",
            channel="discord",
            session_id="sess-001",
            correlation_id="corr-001",
            timestamp=0.0,
            metadata={},
            media_refs=[],
        )
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        tasks_calls = [c for c in redis.xadd.await_args_list
                       if c.args[0] == "relais:tasks"]
        assert len(tasks_calls) == 0, "missing user_record must deny the message"

    async def test_missing_user_record_still_acked(self, tmp_path: Path) -> None:
        """Envelope without user_record is ACKed (not left in PEL).

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.main import Sentinelle

        env = Envelope(
            content="bonjour",
            sender_id="discord:admin001",
            channel="discord",
            session_id="sess-001",
            correlation_id="corr-001",
            timestamp=0.0,
            metadata={},
            media_refs=[],
        )
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_sentinelle_yaml(tmp_path))

        spec = sentinel.stream_specs()[0]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis, shutdown_event)
        except asyncio.CancelledError:
            pass

        redis.xack.assert_awaited_once_with("relais:security", "sentinelle_group", b"1-0")
