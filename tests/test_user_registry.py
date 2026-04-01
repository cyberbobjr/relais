"""Tests for common.user_registry.UserRegistry.

TDD — tests written BEFORE implementation (RED phase).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from common.user_registry import UserRecord, UserRegistry


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
      usr_with_prompt:
        display_name: "User With Prompt"
        role: user
        blocked: false
        custom_prompt_path: "users/discord_user002.md"
        identifiers:
          discord:
            dm: "user002"
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


# ---------------------------------------------------------------------------
# UserRecord dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_record_is_frozen() -> None:
    """UserRecord must be immutable (frozen dataclass).

    Assigning to any field must raise AttributeError or TypeError.
    """
    record = UserRecord(display_name="Alice", role="user", custom_prompt_path=None, blocked=False)
    with pytest.raises((AttributeError, TypeError)):
        record.role = "admin"  # type: ignore[misc]


@pytest.mark.unit
def test_user_record_custom_prompt_path_default_is_none() -> None:
    """UserRecord.custom_prompt_path defaults to None.

    When not supplied it must be None, not some sentinel.
    """
    record = UserRecord(display_name="Bob", role="user", custom_prompt_path=None, blocked=False)
    assert record.custom_prompt_path is None


# ---------------------------------------------------------------------------
# UserRegistry.resolve_user — known user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_returns_user_record_for_known_user(tmp_path: Path) -> None:
    """resolve_user returns a UserRecord for a sender_id listed in users.yaml.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert isinstance(result, UserRecord)


@pytest.mark.unit
def test_resolve_user_correct_display_name(tmp_path: Path) -> None:
    """resolve_user returns UserRecord with the correct display_name field.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.display_name == "Admin User"


@pytest.mark.unit
def test_resolve_user_correct_role(tmp_path: Path) -> None:
    """resolve_user returns UserRecord with the correct role field.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:user001", "discord")

    assert result is not None
    assert result.role == "user"


@pytest.mark.unit
def test_resolve_user_blocked_flag(tmp_path: Path) -> None:
    """resolve_user correctly parses the blocked flag.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.blocked is False


# ---------------------------------------------------------------------------
# UserRegistry.resolve_user — custom_prompt_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_custom_prompt_path_when_present(tmp_path: Path) -> None:
    """resolve_user returns UserRecord with custom_prompt_path when set in YAML.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:user002", "discord")

    assert result is not None
    assert result.custom_prompt_path == "users/discord_user002.md"


@pytest.mark.unit
def test_resolve_user_custom_prompt_path_is_none_when_absent(tmp_path: Path) -> None:
    """resolve_user returns UserRecord with custom_prompt_path=None when key absent.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:user001", "discord")

    assert result is not None
    assert result.custom_prompt_path is None


@pytest.mark.unit
def test_resolve_user_custom_prompt_path_is_none_when_null_in_yaml(tmp_path: Path) -> None:
    """resolve_user returns custom_prompt_path=None when YAML value is null.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    content = dedent("""\
        access_control:
          default_mode: allowlist
        groups: []
        users:
          usr_null_prompt:
            display_name: "Null Prompt User"
            role: user
            blocked: false
            custom_prompt_path: null
            identifiers:
              discord:
                dm: "user003"
        roles:
          user:
            actions: ["send"]
    """)
    path = _write_users_yaml(tmp_path, content)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:user003", "discord")

    assert result is not None
    assert result.custom_prompt_path is None


# ---------------------------------------------------------------------------
# UserRegistry.resolve_user — unknown user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_returns_none_for_unknown_user(tmp_path: Path) -> None:
    """resolve_user returns None for a sender_id not listed in users.yaml.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:9999999", "discord")

    assert result is None


@pytest.mark.unit
def test_resolve_user_returns_none_for_unknown_channel(tmp_path: Path) -> None:
    """resolve_user returns None when user exists on another channel but not this one.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    # user001 is only in discord, not in telegram
    result = registry.resolve_user("discord:user001", "telegram")

    assert result is None


# ---------------------------------------------------------------------------
# UserRegistry — context parameter (dm vs server)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_with_dm_context(tmp_path: Path) -> None:
    """resolve_user finds a user by sender_id with default dm context.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord", context="dm")

    assert result is not None
    assert result.role == "admin"


@pytest.mark.unit
def test_resolve_user_with_server_context(tmp_path: Path) -> None:
    """resolve_user finds a user by sender_id with server context.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord", context="server")

    assert result is not None
    assert result.role == "admin"


# ---------------------------------------------------------------------------
# UserRegistry — sender_index fast lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_by_sender_index_format(tmp_path: Path) -> None:
    """resolve_user works with 'channel:raw_id' sender_id format.

    The sender_index stores keys in 'channel:raw_id' format for O(1) lookup.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("telegram:admin001", "telegram")

    assert result is not None
    assert result.role == "admin"


# ---------------------------------------------------------------------------
# UserRegistry.reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reload_picks_up_new_users(tmp_path: Path) -> None:
    """reload() re-reads users.yaml and makes new users visible.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    assert registry.resolve_user("discord:newuser", "discord") is None

    updated = dedent("""\
        access_control:
          default_mode: allowlist
        groups: []
        users:
          usr_newuser:
            display_name: "New User"
            role: user
            blocked: false
            identifiers:
              discord:
                dm: "newuser"
        roles:
          user:
            actions: ["send"]
    """)
    path.write_text(updated, encoding="utf-8")
    registry.reload()

    result = registry.resolve_user("discord:newuser", "discord")
    assert result is not None
    assert result.display_name == "New User"


@pytest.mark.unit
def test_reload_removes_deleted_users(tmp_path: Path) -> None:
    """reload() clears users that disappeared from the updated file.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    assert registry.resolve_user("discord:user001", "discord") is not None

    minimal = dedent("""\
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
    path.write_text(minimal, encoding="utf-8")
    registry.reload()

    assert registry.resolve_user("discord:user001", "discord") is None


# ---------------------------------------------------------------------------
# UserRegistry — permissive mode (missing users.yaml)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_permissive_mode_returns_none_for_any_user() -> None:
    """When users.yaml is absent, resolve_user returns None (permissive — no crash).

    The registry must not raise even when the config file does not exist.
    """
    registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    result = registry.resolve_user("discord:anyone", "discord")

    assert result is None


@pytest.mark.unit
def test_permissive_mode_does_not_raise_on_init() -> None:
    """UserRegistry init must not raise when users.yaml is absent.

    Permissive mode means missing config is not an error.
    """
    # Must not raise
    registry = UserRegistry(config_path=Path("/nonexistent/path/users.yaml"))
    assert registry is not None


# ---------------------------------------------------------------------------
# UserRegistry — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_empty_sender_id_returns_none(tmp_path: Path) -> None:
    """resolve_user returns None gracefully for an empty sender_id string.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("", "discord")

    assert result is None


@pytest.mark.unit
def test_resolve_user_no_colon_in_sender_id_returns_none(tmp_path: Path) -> None:
    """resolve_user returns None when sender_id has no 'channel:id' separator.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("malformed_sender", "discord")

    assert result is None
