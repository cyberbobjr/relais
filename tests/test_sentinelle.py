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


# Keep legacy alias used in reload tests below
def _write_users_yaml(tmp_path: Path, content: str = _SENTINELLE_YAML) -> Path:
    """Write a sentinelle.yaml file (legacy alias for backward compat with reload tests).

    Args:
        tmp_path: Pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    return _write_sentinelle_yaml(tmp_path, content)


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
        display_name="Test User",
        role=role,
        blocked=blocked,
        actions=actions if actions is not None else ["send"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        llm_profile="default",
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


class TestACLManagerReload:
    """Tests for ACLManager.reload() hot-reload behaviour."""

    def test_reload_picks_up_new_groups_from_disk(self, tmp_path: Path) -> None:
        """reload() re-reads sentinelle.yaml and reflects changes made after initial load."""
        config_path = _write_sentinelle_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        # At this point, group "grp_test" does not exist — allowlist mode rejects unknown groups
        assert acl.is_allowed("discord:anyone", "discord", context="group", scope_id="grp_test") is False

        # Overwrite the YAML on disk with the new group allowed
        updated_yaml = dedent("""\
            access_control:
              default_mode: allowlist
            groups:
              - channel: discord
                group_id: "grp_test"
                allowed: true
                blocked: false
        """)
        config_path.write_text(updated_yaml, encoding="utf-8")

        acl.reload()

        assert acl.is_allowed("discord:anyone", "discord", context="group", scope_id="grp_test") is True

    def test_reload_removes_old_groups_that_no_longer_exist(self, tmp_path: Path) -> None:
        """reload() clears previously loaded groups that are absent from the updated file."""
        initial_yaml = dedent("""\
            access_control:
              default_mode: allowlist
            groups:
              - channel: discord
                group_id: "grp_existing"
                allowed: true
                blocked: false
        """)
        config_path = _write_sentinelle_yaml(tmp_path, initial_yaml)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:anyone", "discord", context="group", scope_id="grp_existing") is True

        # Remove the group from the file
        config_path.write_text(_SENTINELLE_YAML, encoding="utf-8")
        acl.reload()

        assert acl.is_allowed("discord:anyone", "discord", context="group", scope_id="grp_existing") is False


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


@pytest.mark.unit
@pytest.mark.asyncio
class TestACLManagerNotifyPending:
    """Tests for ACLManager.notify_pending() — publishes to admin stream."""

    async def test_notify_pending_publishes_to_admin_stream(self) -> None:
        """notify_pending publishes to relais:admin:pending_users stream.

        Args: (none)
        """
        acl = ACLManager(config_path=Path("/nonexistent/sentinelle.yaml"))

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await acl.notify_pending(redis_mock, "discord:999", "discord")

        redis_mock.xadd.assert_awaited_once()
        call_args = redis_mock.xadd.call_args
        stream_name = call_args[0][0]
        assert stream_name == "relais:admin:pending_users"

    async def test_notify_pending_payload_contains_user_id(self) -> None:
        """Payload published to pending_users includes the unknown user_id.

        Args: (none)
        """
        acl = ACLManager(config_path=Path("/nonexistent/sentinelle.yaml"))

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await acl.notify_pending(redis_mock, "discord:999", "discord")

        payload_dict = redis_mock.xadd.call_args[0][1]
        assert "discord:999" in str(payload_dict)


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
        metadata={},
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
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            [],  # second call returns empty → loop exits via shutdown
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        from common.shutdown import GracefulShutdown
        shutdown = MagicMock(spec=GracefulShutdown)
        # Stop after processing the first batch
        shutdown.is_stopping.side_effect = [False, False, True]

        await sentinel._process_outgoing_stream(redis_conn, shutdown=shutdown)

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
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            [],
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        from common.shutdown import GracefulShutdown
        shutdown = MagicMock(spec=GracefulShutdown)
        shutdown.is_stopping.side_effect = [False, False, True]

        await sentinel._process_outgoing_stream(redis_conn, shutdown=shutdown)

        outgoing_calls = [
            c for c in redis_conn.xadd.await_args_list
            if c.args[0] == "relais:messages:outgoing:discord"
        ]
        assert len(outgoing_calls) == 1
        forwarded = json.loads(outgoing_calls[0].args[1]["payload"])
        traces = forwarded.get("metadata", {}).get("traces", [])
        assert any("sentinelle" in str(t) for t in traces)

    async def test_passthrough_acks_message(self, tmp_path: Path) -> None:
        """Message is ACKed from outgoing_pending after forwarding.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.main import Sentinelle

        env = _make_outgoing_envelope("discord")
        payload = env.to_json()

        redis_conn = AsyncMock()
        redis_conn.xgroup_create = AsyncMock(return_value="OK")
        redis_conn.xreadgroup = AsyncMock(side_effect=[
            [("relais:messages:outgoing_pending", [(b"1-0", {"payload": payload})])],
            [],
        ])
        redis_conn.xadd = AsyncMock(return_value=b"2-0")
        redis_conn.xack = AsyncMock(return_value=1)

        sentinel = Sentinelle.__new__(Sentinelle)
        sentinel.outgoing_group_name = "sentinelle_outgoing_group"
        sentinel.outgoing_consumer_name = "sentinelle_outgoing_1"

        from common.shutdown import GracefulShutdown
        shutdown = MagicMock(spec=GracefulShutdown)
        shutdown.is_stopping.side_effect = [False, False, True]

        await sentinel._process_outgoing_stream(redis_conn, shutdown=shutdown)

        redis_conn.xack.assert_awaited_once_with(
            "relais:messages:outgoing_pending",
            "sentinelle_outgoing_group",
            b"1-0",
        )

