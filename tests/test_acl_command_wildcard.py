"""Tests TDD — ACLManager command authorization using user_record= parameter.

actions in roles ONLY governs slash commands:
- ["*"]       → any command allowed
- ["help"]    → only /help allowed
- []          → no command allowed

Normal message sending (action=None) is NOT governed by actions —
it is controlled by access_control.default_mode.

Config format: sentinelle.yaml (access_control + groups only — NO users, NO roles).
Caller passes user_record= to is_allowed() instead of relying on internal lookup.
"""
from pathlib import Path
from textwrap import dedent

import pytest

from common.user_record import UserRecord
from sentinelle.acl import ACLManager


def _write_sentinelle_yaml(tmp_path: Path, content: str) -> Path:
    """Write a sentinelle.yaml file to the given temporary directory.

    Args:
        tmp_path: Pytest temporary directory.
        content: YAML content to write.

    Returns:
        Path to the written file.
    """
    p = tmp_path / "sentinelle.yaml"
    p.write_text(content, encoding="utf-8")
    return p


_SENTINELLE_YAML = dedent("""\
    access_control:
      default_mode: allowlist
    groups: []
""")


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
        prompt_path=None,
    )


def _make_user_record(actions: list[str] | None = None) -> UserRecord:
    """Build a regular user UserRecord.

    Args:
        actions: List of allowed command names. Defaults to ["help"].

    Returns:
        Configured user UserRecord.
    """
    return UserRecord(
        user_id="usr_user",
        display_name="User",
        role="user",
        blocked=False,
        actions=actions if actions is not None else ["help"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        prompt_path=None,
    )


def _make_blocked_record() -> UserRecord:
    """Build a blocked admin UserRecord.

    Returns:
        Blocked UserRecord with admin role.
    """
    return UserRecord(
        user_id="usr_blocked",
        display_name="Blocked Admin",
        role="admin",
        blocked=True,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        prompt_path=None,
    )


@pytest.mark.unit
class TestACLCommandWildcard:
    """Role with ['*'] in actions grants access to any command name."""

    def test_admin_wildcard_allowed_for_clear(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed action='clear'."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        admin = _make_admin_record()

        assert acl.is_allowed(
            "discord:admin001", "discord", user_record=admin, action="clear"
        ) is True

    def test_admin_wildcard_allowed_for_help(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed action='help'."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        admin = _make_admin_record()

        assert acl.is_allowed(
            "discord:admin001", "discord", user_record=admin, action="help"
        ) is True

    def test_admin_wildcard_allowed_for_arbitrary_command(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed any arbitrary command name."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        admin = _make_admin_record()

        assert acl.is_allowed(
            "discord:admin001", "discord", user_record=admin, action="status"
        ) is True

    def test_user_with_explicit_help_allowed_for_help(self, tmp_path: Path) -> None:
        """User with explicit 'help' action is allowed action='help'."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        user = _make_user_record(actions=["help"])

        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user, action="help"
        ) is True

    def test_user_without_wildcard_denied_for_clear(self, tmp_path: Path) -> None:
        """User without '*' wildcard and no 'clear' action is denied action='clear'."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        user = _make_user_record(actions=["help"])

        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user, action="clear"
        ) is False

    def test_user_no_command_action_denied(self, tmp_path: Path) -> None:
        """User with actions=['help'] is denied action='status'."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        user = _make_user_record(actions=["help"])

        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user, action="status"
        ) is False

    def test_normal_message_no_action_allowed_for_known_user(self, tmp_path: Path) -> None:
        """Known user without action= (normal message) is allowed — actions not checked."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        user = _make_user_record()

        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user
        ) is True

    def test_normal_message_no_action_allowed_for_admin(self, tmp_path: Path) -> None:
        """Admin without action= (normal message) is allowed — actions not checked."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        admin = _make_admin_record()

        assert acl.is_allowed(
            "discord:admin001", "discord", user_record=admin
        ) is True

    def test_blocked_user_denied_despite_wildcard(self, tmp_path: Path) -> None:
        """Blocked user is denied even if their role has '*' wildcard."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        blocked = _make_blocked_record()

        assert acl.is_allowed(
            "discord:blocked001", "discord", user_record=blocked, action="clear"
        ) is False

    def test_permissive_mode_allows_any_action(self, tmp_path: Path) -> None:
        """In permissive mode (no config), any action is allowed."""
        acl = ACLManager(config_path=Path("/nonexistent/sentinelle.yaml"))

        assert acl.is_allowed("discord:anyone", "discord", action="clear") is True

    def test_no_user_record_fails_closed_allowlist(self, tmp_path: Path) -> None:
        """Without user_record in allowlist mode, is_allowed returns False (fail-closed)."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)

        # No user_record provided — unknown identity → deny
        assert acl.is_allowed("discord:unknown", "discord") is False

    def test_user_with_empty_actions_denied_all_commands(self, tmp_path: Path) -> None:
        """User with actions=[] is denied every command."""
        cfg = _write_sentinelle_yaml(tmp_path, _SENTINELLE_YAML)
        acl = ACLManager(config_path=cfg)
        user = _make_user_record(actions=[])

        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user, action="clear"
        ) is False
        assert acl.is_allowed(
            "discord:user001", "discord", user_record=user, action="help"
        ) is False

    def test_group_context_still_uses_group_lookup(self, tmp_path: Path) -> None:
        """Group context authorization uses group config, not user_record."""
        content = dedent("""\
            access_control:
              default_mode: allowlist
            groups:
              - channel: telegram
                group_id: "group123"
                allowed: true
                blocked: false
        """)
        cfg = _write_sentinelle_yaml(tmp_path, content)
        acl = ACLManager(config_path=cfg)
        admin = _make_admin_record()

        assert acl.is_allowed(
            "telegram:user001", "telegram",
            context="group", scope_id="group123",
            user_record=admin,
        ) is True
