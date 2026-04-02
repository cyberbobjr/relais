"""Tests TDD — ACLManager command authorization (Phase 2 GREEN).

actions in roles ONLY governs slash commands:
- ["*"]       → any command allowed
- ["help"]    → only /help allowed
- []          → no command allowed

Normal message sending (action=None) is NOT governed by actions —
it is controlled by access_control.default_mode and unknown_user_policy.
"""
from pathlib import Path
from textwrap import dedent

import pytest

from sentinelle.acl import ACLManager


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(content, encoding="utf-8")
    return p


_YAML_ROLES = dedent("""\
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
        actions: ["help"]
""")


@pytest.mark.unit
class TestACLCommandWildcard:
    """Role with ["*"] in actions grants access to any command name."""

    def test_admin_wildcard_allowed_for_clear(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed action='clear'."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:admin001", "discord", action="clear") is True

    def test_admin_wildcard_allowed_for_help(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed action='help'."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:admin001", "discord", action="help") is True

    def test_admin_wildcard_allowed_for_arbitrary_command(self, tmp_path: Path) -> None:
        """Admin (has '*') is allowed any arbitrary command name."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:admin001", "discord", action="status") is True

    def test_user_with_explicit_help_allowed_for_help(self, tmp_path: Path) -> None:
        """User with explicit 'help' action is allowed action='help'."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:user001", "discord", action="help") is True

    def test_user_without_wildcard_denied_for_clear(self, tmp_path: Path) -> None:
        """User without '*' wildcard and no 'clear' action is denied action='clear'."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:user001", "discord", action="clear") is False

    def test_user_no_command_action_denied(self, tmp_path: Path) -> None:
        """User with actions=['help'] is denied action='status'."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:user001", "discord", action="status") is False

    def test_normal_message_no_action_allowed_for_known_user(self, tmp_path: Path) -> None:
        """Known user without action= (normal message) is allowed — actions not checked."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:user001", "discord") is True

    def test_normal_message_no_action_allowed_for_admin(self, tmp_path: Path) -> None:
        """Admin without action= (normal message) is allowed — actions not checked."""
        cfg = _write_yaml(tmp_path, _YAML_ROLES)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:admin001", "discord") is True

    def test_blocked_user_denied_despite_wildcard(self, tmp_path: Path) -> None:
        """Blocked user is denied even if their role has '*' wildcard."""
        yaml_blocked = dedent("""\
            access_control:
              default_mode: allowlist
            groups: []
            users:
              usr_blocked_admin:
                display_name: "Blocked Admin"
                role: admin
                blocked: true
                identifiers:
                  discord:
                    dm: "blocked001"
            roles:
              admin:
                actions: ["*"]
        """)
        cfg = _write_yaml(tmp_path, yaml_blocked)
        acl = ACLManager(config_path=cfg)

        assert acl.is_allowed("discord:blocked001", "discord", action="clear") is False

    def test_permissive_mode_allows_any_action(self, tmp_path: Path) -> None:
        """In permissive mode (no config), any action is allowed."""
        acl = ACLManager(config_path=Path("/nonexistent/users.yaml"))

        assert acl.is_allowed("discord:anyone", "discord", action="clear") is True
