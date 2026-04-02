"""Tests TDD — Sentinelle command bifurcation (Phase 3 RED).

Verifies the new routing logic:
- Regular messages → relais:tasks (unchanged)
- Known + authorised commands → relais:commands
- Unknown commands → inline reply to relais:messages:outgoing:{channel}
- Known but unauthorised commands → inline reply with permission denied
"""
import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from common.envelope import Envelope
from sentinelle.acl import ACLManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_users_yaml(tmp_path: Path) -> Path:
    """Write a users.yaml with admin (command wildcard) and user (send only)."""
    content = dedent("""\
        access_control:
          default_mode: allowlist
        groups: []
        users:
          usr_admin:
            display_name: "Admin"
            role: admin
            blocked: false
            identifiers:
              discord:
                dm: "admin001"
          usr_user:
            display_name: "User"
            role: user
            blocked: false
            identifiers:
              discord:
                dm: "user001"
        roles:
          admin:
            actions: ["*"]
          user:
            actions: []
    """)
    p = tmp_path / "users.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_envelope(
    content: str,
    sender_id: str = "discord:admin001",
    channel: str = "discord",
) -> Envelope:
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        timestamp=0.0,
        metadata={"user_role": "admin"},
        media_refs=[],
    )


def _make_redis(envelope: Envelope) -> AsyncMock:
    """Return a mock Redis with the envelope pre-loaded in xreadgroup."""
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(side_effect=[
        [("relais:security", [(b"1-0", {"payload": envelope.to_json()})])],
        [],
    ])
    redis.xadd = AsyncMock(return_value=b"2-0")
    redis.xack = AsyncMock(return_value=1)
    return redis


def _shutdown_after_first_batch() -> MagicMock:
    from common.shutdown import GracefulShutdown
    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]
    return shutdown


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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

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
        """User /clear (no 'command' wildcard) → permission-denied reply."""
        from sentinelle.main import Sentinelle

        # User role has only "send", not "command"
        env = _make_envelope(
            "/clear",
            sender_id="discord:user001",
        )
        env.metadata["user_role"] = "user"
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        assert len(outgoing_calls) == 1

    async def test_unauthorised_command_reply_mentions_command_name(
        self, tmp_path: Path
    ) -> None:
        """Permission-denied reply mentions 'clear' in its content."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:user001")
        env.metadata["user_role"] = "user"
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

        outgoing_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:messages:outgoing:discord"]
        reply_env = Envelope.from_json(outgoing_calls[0].args[1]["payload"])
        assert "clear" in reply_env.content.lower()

    async def test_unauthorised_command_not_sent_to_commands_stream(
        self, tmp_path: Path
    ) -> None:
        """Unauthorised known command must NOT be forwarded to relais:commands."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:user001")
        env.metadata["user_role"] = "user"
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

        commands_calls = [c for c in redis.xadd.await_args_list
                          if c.args[0] == "relais:commands"]
        assert len(commands_calls) == 0

    async def test_unauthorised_command_acked(self, tmp_path: Path) -> None:
        """Unauthorised command message is ACKed after reply is sent."""
        from sentinelle.main import Sentinelle

        env = _make_envelope("/clear", sender_id="discord:user001")
        env.metadata["user_role"] = "user"
        redis = _make_redis(env)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel._acl = ACLManager(config_path=_make_users_yaml(tmp_path))

        await sentinel._process_stream(redis, shutdown=_shutdown_after_first_batch())

        redis.xack.assert_awaited_once_with("relais:security", "sentinelle_group", b"1-0")
