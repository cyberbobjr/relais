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

_USERS_YAML = dedent("""\
    access_control:
      default_mode: allowlist
    groups: []
    users:
      usr_admin:
        display_name: "Admin User"
        role: admin
        blocked: false
        llm_profile: default
        identifiers:
          discord:
            dm: "admin001"
            server: "admin001"
          telegram:
            dm: "admin001"
      usr_user001:
        display_name: "Regular User"
        role: user
        blocked: false
        llm_profile: default
        identifiers:
          discord:
            dm: "user001"
    roles:
      admin:
        actions: ["send", "admin", "config"]
      user:
        actions: ["send"]
""")


def _write_users_yaml(tmp_path: Path, content: str = _USERS_YAML) -> Path:
    """Write a users.yaml file to the given temporary directory.

    Args:
        tmp_path: Pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    p = tmp_path / "users.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ===========================================================================
# ACLManager tests
# ===========================================================================


class TestACLManagerPermissiveMode:
    """Tests for ACLManager when no users.yaml is available (permissive mode)."""

    def test_is_allowed_returns_true_when_no_users_yaml(self) -> None:
        """is_allowed() returns True for any user/channel when no config file exists."""
        acl = ACLManager(config_path=Path("/nonexistent/path/users.yaml"))
        assert acl.is_allowed("discord:unknown", "discord") is True

    def test_is_allowed_returns_true_for_any_channel_in_permissive_mode(self) -> None:
        """Permissive mode allows all channels unconditionally."""
        acl = ACLManager(config_path=Path("/nonexistent/path/users.yaml"))
        assert acl.is_allowed("discord:anyone", "telegram") is True

    def test_get_user_role_returns_admin_in_permissive_mode(self) -> None:
        """get_user_role() returns 'admin' for any user_id in permissive mode."""
        acl = ACLManager(config_path=Path("/nonexistent/path/users.yaml"))
        assert acl.get_user_role("discord:unknown") == "admin"


class TestACLManagerWithConfig:
    """Tests for ACLManager with a valid users.yaml loaded."""

    def test_admin_is_allowed_on_all_configured_channels(self, tmp_path: Path) -> None:
        """Admin user can access every channel where they have an identifier."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:admin001", "discord") is True
        assert acl.is_allowed("telegram:admin001", "telegram") is True

    def test_user_is_allowed_on_authorized_channel(self, tmp_path: Path) -> None:
        """Regular user can access their explicitly authorized channel."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:user001", "discord") is True

    def test_user_is_denied_on_unauthorized_channel(self, tmp_path: Path) -> None:
        """Regular user is denied access to a channel not in their list."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:user001", "telegram") is False

    def test_unknown_user_is_denied_in_strict_mode(self, tmp_path: Path) -> None:
        """Unknown user_id returns False when users.yaml exists (strict mode)."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:9999999", "discord") is False

    def test_get_user_role_returns_correct_role_for_admin(self, tmp_path: Path) -> None:
        """get_user_role() returns 'admin' for an admin user."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.get_user_role("discord:admin001") == "admin"

    def test_get_user_role_returns_correct_role_for_user(self, tmp_path: Path) -> None:
        """get_user_role() returns 'user' for a regular user."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.get_user_role("discord:user001") == "user"

    def test_get_user_role_returns_unknown_for_nonexistent_user(self, tmp_path: Path) -> None:
        """get_user_role() returns 'unknown' for a user_id not in the config."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.get_user_role("discord:doesnotexist") == "unknown"

    def test_user_denied_for_action_not_in_role(self, tmp_path: Path) -> None:
        """Regular user (role=user) is denied for an action not in their role definition."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        # "user" role only has "send" action — "admin" action must be denied
        assert acl.is_allowed("discord:user001", "discord", action="admin") is False


class TestACLManagerReload:
    """Tests for ACLManager.reload() hot-reload behaviour."""

    def test_reload_picks_up_new_users_from_disk(self, tmp_path: Path) -> None:
        """reload() re-reads users.yaml and reflects changes made after initial load."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        # At this point, "discord:newuser" does not exist
        assert acl.is_allowed("discord:newuser", "discord") is False

        # Overwrite the YAML on disk with an additional user
        updated_yaml = dedent("""\
            access_control:
              default_mode: allowlist
            groups: []
            users:
              usr_admin:
                display_name: "Admin User"
                role: admin
                blocked: false
                identifiers:
                  discord:
                    dm: "admin001"
                    server: "admin001"
                  telegram:
                    dm: "admin001"
              usr_user001:
                display_name: "Regular User"
                role: user
                blocked: false
                identifiers:
                  discord:
                    dm: "user001"
              usr_newuser:
                display_name: "New User"
                role: user
                blocked: false
                identifiers:
                  discord:
                    dm: "newuser"
            roles:
              admin:
                actions: ["send", "admin", "config"]
              user:
                actions: ["send"]
        """)
        config_path.write_text(updated_yaml, encoding="utf-8")

        acl.reload()

        assert acl.is_allowed("discord:newuser", "discord") is True

    def test_reload_removes_old_users_that_no_longer_exist(self, tmp_path: Path) -> None:
        """reload() clears previously loaded users that are absent from the updated file."""
        config_path = _write_users_yaml(tmp_path)
        acl = ACLManager(config_path=config_path)

        assert acl.is_allowed("discord:user001", "discord") is True

        # Remove user001 from the file
        minimal_yaml = dedent("""\
            access_control:
              default_mode: allowlist
            groups: []
            users:
              usr_admin:
                display_name: "Admin User"
                role: admin
                blocked: false
                identifiers:
                  discord:
                    dm: "admin001"
            roles:
              admin:
                actions: ["send", "admin"]
        """)
        config_path.write_text(minimal_yaml, encoding="utf-8")
        acl.reload()

        assert acl.is_allowed("discord:user001", "discord") is False


# ===========================================================================
# Unknown-user policy tests (migrated from test_sentinelle_policy.py)
# ===========================================================================


def _make_users_yaml(extra_users: dict[str, dict[str, Any]] | None = None) -> str:
    """Build a minimal users.yaml YAML string.

    Args:
        extra_users: Additional user dicts merged into the users mapping.

    Returns:
        YAML text ready to be written to a tmp file.
    """
    users: dict[str, dict[str, Any]] = {
        "usr_alice": {
            "display_name": "Alice",
            "role": "user",
            "blocked": False,
            "identifiers": {"discord": {"dm": "111"}},
        },
    }
    if extra_users:
        users.update(extra_users)

    data: dict[str, Any] = {
        "access_control": {"default_mode": "allowlist"},
        "groups": [],
        "users": users,
        "roles": {
            "user": {"actions": ["send"]},
            "admin": {"actions": ["send", "admin"]},
        },
    }
    return yaml.dump(data)


@pytest.mark.unit
class TestUnknownUserPolicyDeny:
    """T1: unknown user + deny policy → returns False, no side effects."""

    def test_deny_returns_false(self, tmp_path: Path) -> None:
        """ACLManager.check_unknown_user returns False for deny policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="deny",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is False

    def test_deny_does_not_mutate_known_users(self, tmp_path: Path) -> None:
        """Known user is unaffected by deny policy for unknowns.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="deny",
        )

        assert mgr.is_allowed("discord:111", "discord") is True


@pytest.mark.unit
class TestUnknownUserPolicyGuest:
    """T2: unknown user + guest policy → returns True with guest_profile attached."""

    def test_guest_returns_true(self, tmp_path: Path) -> None:
        """ACLManager.is_allowed returns True for unknown user under guest policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="guest",
            guest_profile="fast",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnknownUserPolicyPending:
    """T3: unknown user + pending policy → False + publishes to admin stream."""

    async def test_pending_returns_false(self, tmp_path: Path) -> None:
        """ACLManager.is_allowed returns False for unknown user under pending policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is False

    async def test_pending_publishes_to_admin_stream(self, tmp_path: Path) -> None:
        """notify_pending publishes to relais:admin:pending_users stream.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await mgr.notify_pending(redis_mock, "discord:999", "discord")

        redis_mock.xadd.assert_awaited_once()
        call_args = redis_mock.xadd.call_args
        stream_name = call_args[0][0]
        assert stream_name == "relais:admin:pending_users"

    async def test_pending_payload_contains_user_id(self, tmp_path: Path) -> None:
        """Payload published to pending_users includes the unknown user_id.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await mgr.notify_pending(redis_mock, "discord:999", "discord")

        payload_dict = redis_mock.xadd.call_args[0][1]
        assert "discord:999" in str(payload_dict)


@pytest.mark.unit
class TestUnknownUserPolicyInvalid:
    """Raise ValueError on unsupported policy string."""

    def test_invalid_policy_raises_value_error(self, tmp_path: Path) -> None:
        """ACLManager raises ValueError for unknown policy string.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        with pytest.raises(ValueError, match="unknown_user_policy"):
            ACLManager(
                config_path=users_file,
                unknown_user_policy="allow_all_and_profit",
            )


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
            [("relais:messages:outgoing_pending:discord", [(b"1-0", {"payload": payload})])],
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

        await sentinel._process_outgoing_stream(redis_conn, "discord", shutdown=shutdown)

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
            [("relais:messages:outgoing_pending:discord", [(b"1-0", {"payload": payload})])],
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

        await sentinel._process_outgoing_stream(redis_conn, "discord", shutdown=shutdown)

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
            [("relais:messages:outgoing_pending:discord", [(b"1-0", {"payload": payload})])],
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

        await sentinel._process_outgoing_stream(redis_conn, "discord", shutdown=shutdown)

        redis_conn.xack.assert_awaited_once_with(
            "relais:messages:outgoing_pending:discord",
            "sentinelle_outgoing_group",
            b"1-0",
        )
