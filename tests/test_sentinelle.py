"""Unit tests for sentinelle.acl (ACLManager) and Sentinelle outgoing pass-through."""

import json
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio
import yaml

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING_PENDING

from sentinelle.acl import ACLManager


# ---------------------------------------------------------------------------
# Shared YAML fixtures
# ---------------------------------------------------------------------------

# sentinelle.yaml format: only access_control and groups (no users/roles)
_SENTINELLE_YAML = dedent("""\
    access_control:
      default_mode: allowlist
    groups: []
""")

_SENTINELLE_YAML_BLOCKLIST = dedent("""\
    access_control:
      default_mode: blocklist
    groups: []
""")


def _write_sentinelle_yaml(tmp_path: Path, content: str = _SENTINELLE_YAML) -> Path:
    """Write a sentinelle.yaml file to the given temporary directory.

    Args:
        tmp_path: Pytest temporary directory fixture.
        content: YAML content to write (sentinelle.yaml format).

    Returns:
        Path to the created file.
    """
    p = tmp_path / "sentinelle.yaml"
    p.write_text(content, encoding="utf-8")
    return p




def _make_user_record(
    *,
    role: str = "user",
    blocked: bool = False,
    actions: list[str] | None = None,
) -> "UserRecord":
    """Build a minimal UserRecord for ACL tests.

    Args:
        role: Role name for the record.
        blocked: Whether the user is blocked.
        actions: List of allowed actions; defaults to ["send"].

    Returns:
        A UserRecord instance.
    """
    from common.user_record import UserRecord
    return UserRecord(
        user_id="usr_test",
        display_name="Test User",
        role=role,
        blocked=blocked,
        actions=actions if actions is not None else ["send"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        allowed_subagents=[],
        prompt_path=None,
    )


# ===========================================================================
# ACLManager tests
# ===========================================================================


class TestACLManagerPermissiveMode:
    """Tests for ACLManager when no sentinelle.yaml is available (permissive mode)."""

    def test_is_allowed_returns_true_when_no_config(self) -> None:
        """is_allowed() returns True for any user/channel when no config file exists."""
        acl = ACLManager(config_path=Path("/nonexistent/path/sentinelle.yaml"))
        assert acl.is_allowed("discord:unknown", "discord") is True

    def test_is_allowed_returns_true_for_any_channel_in_permissive_mode(self) -> None:
        """Permissive mode allows all channels unconditionally."""
        acl = ACLManager(config_path=Path("/nonexistent/path/sentinelle.yaml"))
        assert acl.is_allowed("discord:anyone", "telegram") is True

    def test_is_allowed_returns_true_regardless_of_user_record_in_permissive_mode(self) -> None:
        """Permissive mode returns True even without a user_record."""
        acl = ACLManager(config_path=Path("/nonexistent/path/sentinelle.yaml"))
        assert acl.is_allowed("discord:unknown", "discord", user_record=None) is True


class TestACLManagerWithConfig:
    """Tests for ACLManager with a valid sentinelle.yaml loaded."""

    def test_user_with_valid_record_is_allowed(self, tmp_path: Path) -> None:
        """User with non-blocked user_record is allowed in allowlist mode."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="user", blocked=False)

        assert acl.is_allowed("discord:user001", "discord", user_record=record) is True

    def test_user_without_user_record_is_denied_in_allowlist_mode(self, tmp_path: Path) -> None:
        """No user_record in allowlist mode → denied (fail-closed)."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:user001", "discord", user_record=None) is False

    def test_blocked_user_is_denied_regardless_of_channel(self, tmp_path: Path) -> None:
        """Blocked user_record always returns False."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="user", blocked=True)

        assert acl.is_allowed("discord:user001", "discord", user_record=record) is False
        assert acl.is_allowed("discord:user001", "telegram", user_record=record) is False

    def test_user_allowed_for_action_in_role(self, tmp_path: Path) -> None:
        """User with matching action in user_record.actions is allowed."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="user", actions=["send"])

        assert acl.is_allowed("discord:user001", "discord", action="send", user_record=record) is True

    def test_user_denied_for_action_not_in_role(self, tmp_path: Path) -> None:
        """User without the action in user_record.actions is denied."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="user", actions=["send"])

        assert acl.is_allowed("discord:user001", "discord", action="admin", user_record=record) is False

    def test_wildcard_actions_allows_any_command(self, tmp_path: Path) -> None:
        """user_record with actions=['*'] allows any command."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="admin", actions=["*"])

        assert acl.is_allowed("discord:admin001", "discord", action="clear", user_record=record) is True
        assert acl.is_allowed("discord:admin001", "discord", action="obscure_cmd", user_record=record) is True

    def test_unknown_user_without_record_is_denied(self, tmp_path: Path) -> None:
        """No user_record returns False in allowlist mode (fail-closed)."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:9999999", "discord") is False

    def test_blocklist_mode_allows_user_without_record(self, tmp_path: Path) -> None:
        """In blocklist mode, a sender without user_record is allowed through."""
        config_path = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML_BLOCKLIST)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:unknown999", "discord", user_record=None) is True


# ===========================================================================
# ACLManager access-mode and command authorization tests
# ===========================================================================


@pytest.mark.unit
class TestACLManagerAllowlistMode:
    """Tests for ACLManager in allowlist mode — deny when user_record is absent."""

    def test_no_user_record_returns_false_in_allowlist(self, tmp_path: Path) -> None:
        """Missing user_record → False in allowlist mode (fail-closed).

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        result = acl.is_allowed("discord:999", "discord")
        assert result is False

    def test_valid_user_record_returns_true_in_allowlist(self, tmp_path: Path) -> None:
        """Valid non-blocked user_record → True in allowlist mode.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)
        record = _make_user_record(role="user", blocked=False)

        assert acl.is_allowed("discord:111", "discord", user_record=record) is True


@pytest.mark.unit
class TestACLManagerBlocklistMode:
    """Tests for ACLManager in blocklist mode — allow when user_record is absent."""

    def test_no_user_record_returns_true_in_blocklist(self, tmp_path: Path) -> None:
        """Missing user_record → True in blocklist mode (admit all by default).

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        config_path = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML_BLOCKLIST)
        acl = ACLManager(config_path=config_path)

        result = acl.is_allowed("discord:999", "discord")
        assert result is True



# ===========================================================================
# Sentinelle outgoing pass-through tests
# ===========================================================================


def _make_outgoing_envelope(channel: str = "discord") -> Envelope:
    """Build a minimal outgoing response Envelope.

    Args:
        channel: Channel name to use for the envelope.

    Returns:
        A valid Envelope with a reply text.
    """
    return Envelope(
        content="Hello from Atelier",
        sender_id="sentinelle",
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        timestamp=0.0,
        action=ACTION_MESSAGE_OUTGOING_PENDING,
        media_refs=[],
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestSentinelleOutgoingPassthrough:
    """Tests for Sentinelle._process_outgoing_stream() pass-through logic."""

    async def test_passthrough_forwards_to_outgoing_stream(self, tmp_path: Path) -> None:
        """Valid envelope on outgoing_pending is forwarded to outgoing stream.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        import asyncio
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            asyncio.CancelledError(),
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        spec = sentinel.stream_specs()[1]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis_conn, shutdown_event)
        except asyncio.CancelledError:
            pass

        # Envelope must have been forwarded to relais:messages:outgoing:discord
        outgoing_calls = [
            c for c in redis_conn.xadd.await_args_list
            if c.args[0] == "relais:messages:outgoing:discord"
        ]
        assert len(outgoing_calls) == 1

    async def test_passthrough_adds_trace(self, tmp_path: Path) -> None:
        """Forwarded envelope carries the sentinelle outgoing pass-through trace.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        import asyncio
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            asyncio.CancelledError(),
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        spec = sentinel.stream_specs()[1]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis_conn, shutdown_event)
        except asyncio.CancelledError:
            pass

        outgoing_calls = [
            c for c in redis_conn.xadd.await_args_list
            if c.args[0] == "relais:messages:outgoing:discord"
        ]
        assert len(outgoing_calls) == 1
        forwarded = json.loads(outgoing_calls[0].args[1]["payload"])
        traces = forwarded.get("traces", [])
        assert any("sentinelle" in str(t) for t in traces)

    async def test_passthrough_acks_message(self, tmp_path: Path) -> None:
        """Message is ACKed from outgoing_pending after forwarding.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        import asyncio
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            asyncio.CancelledError(),
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.stream_in = "relais:security"
        sentinel.stream_out = "relais:tasks"
        sentinel.stream_commands = "relais:commands"
        sentinel.group_name = "sentinelle_group"
        sentinel.consumer_name = "sentinelle_1"
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        spec = sentinel.stream_specs()[1]
        shutdown_event = asyncio.Event()
        try:
            await sentinel._run_stream_loop(spec, redis_conn, shutdown_event)
        except asyncio.CancelledError:
            pass

        redis_conn.xack.assert_awaited_once_with(
            "relais:messages:outgoing_pending",
            "sentinelle_outgoing_group",
            b"1-0",
        )

