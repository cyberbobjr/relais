"""Tests for portail.user_registry.UserRegistry.

TDD — tests cover the new 9-field UserRecord, to_dict/from_dict round-trip,
fully-resolved records (role data merged in), and build_guest_record().

Config file format is now portail.yaml (users + roles + guest_role).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from common.user_record import UserRecord
from portail.user_registry import UserRegistry


# ---------------------------------------------------------------------------
# Shared YAML fixtures (portail.yaml format)
# ---------------------------------------------------------------------------

_PORTAIL_YAML = dedent("""\
    unknown_user_policy: deny
    guest_role: guest

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
        prompt_path: "users/discord_user002.md"
        identifiers:
          discord:
            dm: "user002"

    roles:
      admin:
        actions: ["*"]
        skills_dirs: ["*"]
        allowed_mcp_tools: ["*"]
        prompt_path: null
      user:
        actions: ["send"]
        skills_dirs: []
        allowed_mcp_tools: []
        prompt_path: "roles/user.md"
      guest:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []
        prompt_path: null
""")


def _write_portail_yaml(tmp_path: Path, content: str = _PORTAIL_YAML) -> Path:
    """Write a portail.yaml file to the given temporary directory.

    Args:
        tmp_path: Pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    p = tmp_path / "portail.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# UserRecord dataclass — 8 new fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_record_is_frozen() -> None:
    """UserRecord must be immutable (frozen dataclass).

    Assigning to any field must raise AttributeError or TypeError.
    """
    record = UserRecord(
        user_id="usr_alice",
        display_name="Alice",
        role="user",
        blocked=False,
        actions=["send"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        prompt_path=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        record.role = "admin"  # type: ignore[misc]


@pytest.mark.unit
def test_user_record_has_all_required_fields() -> None:
    """UserRecord must expose all required fields including role_prompt_path.

    Fields: display_name, role, blocked, actions, skills_dirs,
    allowed_mcp_tools, prompt_path, role_prompt_path.
    llm_profile is NOT a UserRecord field — it lives in envelope.metadata.
    """
    record = UserRecord(
        user_id="usr_bob",
        display_name="Bob",
        role="admin",
        blocked=False,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        prompt_path="users/bob.md",
        role_prompt_path="roles/admin.md",
    )
    assert record.display_name == "Bob"
    assert record.role == "admin"
    assert record.blocked is False
    assert record.actions == ["*"]
    assert record.skills_dirs == ["*"]
    assert record.allowed_mcp_tools == ["*"]
    assert record.prompt_path == "users/bob.md"
    assert record.role_prompt_path == "roles/admin.md"
    assert not hasattr(record, "llm_profile")


@pytest.mark.unit
def test_user_record_prompt_path_default_is_none() -> None:
    """UserRecord.prompt_path defaults to None."""
    record = UserRecord(
        user_id="usr_bob",
        display_name="Bob",
        role="user",
        blocked=False,
        actions=[],
        skills_dirs=[],
        allowed_mcp_tools=[],
        prompt_path=None,
    )
    assert record.prompt_path is None


# ---------------------------------------------------------------------------
# UserRecord.to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_record_to_dict_contains_all_fields() -> None:
    """to_dict() must serialize all fields including role_prompt_path."""
    record = UserRecord(
        user_id="usr_alice",
        display_name="Alice",
        role="admin",
        blocked=False,
        actions=["*"],
        skills_dirs=["code"],
        allowed_mcp_tools=["search__*"],
        prompt_path="users/alice.md",
        role_prompt_path="roles/admin.md",
    )
    d = record.to_dict()

    assert d["display_name"] == "Alice"
    assert d["role"] == "admin"
    assert d["blocked"] is False
    assert d["actions"] == ["*"]
    assert d["skills_dirs"] == ["code"]
    assert d["allowed_mcp_tools"] == ["search__*"]
    assert "llm_profile" not in d
    assert d["prompt_path"] == "users/alice.md"
    assert d["role_prompt_path"] == "roles/admin.md"


@pytest.mark.unit
def test_user_record_from_dict_round_trip() -> None:
    """from_dict(to_dict(record)) must produce an identical record."""
    original = UserRecord(
        user_id="usr_carol",
        display_name="Carol",
        role="user",
        blocked=True,
        actions=["send"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        prompt_path=None,
    )
    restored = UserRecord.from_dict(original.to_dict())

    assert restored == original


@pytest.mark.unit
def test_user_record_from_dict_handles_none_prompt_path() -> None:
    """from_dict must correctly reconstruct prompt_path=None."""
    d = {
        "display_name": "Dave",
        "role": "user",
        "blocked": False,
        "actions": [],
        "skills_dirs": [],
        "allowed_mcp_tools": [],
        "prompt_path": None,
    }
    record = UserRecord.from_dict(d)
    assert record.prompt_path is None
    assert not hasattr(record, "llm_profile")


# ---------------------------------------------------------------------------
# UserRegistry.resolve_user — fully-resolved UserRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_returns_user_record_for_known_user(tmp_path: Path) -> None:
    """resolve_user returns a UserRecord for a sender_id listed in portail.yaml.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert isinstance(result, UserRecord)


@pytest.mark.unit
def test_resolve_user_merges_role_actions(tmp_path: Path) -> None:
    """resolve_user returns a UserRecord with actions merged from the role config.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.actions == ["*"]


@pytest.mark.unit
def test_resolve_user_merges_skills_dirs(tmp_path: Path) -> None:
    """resolve_user returns a UserRecord with skills_dirs merged from the role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.skills_dirs == ["*"]


@pytest.mark.unit
def test_resolve_user_merges_allowed_mcp_tools(tmp_path: Path) -> None:
    """resolve_user returns a UserRecord with allowed_mcp_tools from the role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.allowed_mcp_tools == ["*"]


@pytest.mark.unit
def test_resolve_user_record_has_no_llm_profile_field(tmp_path: Path) -> None:
    """UserRecord returned by resolve_user must NOT contain llm_profile.

    llm_profile is now stamped directly into envelope.metadata by Portail,
    not carried inside UserRecord.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert not hasattr(result, "llm_profile")


@pytest.mark.unit
def test_resolve_user_prompt_path_user_overrides_role(tmp_path: Path) -> None:
    """User-level prompt_path takes priority over role-level prompt_path.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    # usr_with_prompt has prompt_path = "users/discord_user002.md", role=user
    # role user has prompt_path = "roles/user.md"
    result = registry.resolve_user("discord:user002", "discord")

    assert result is not None
    assert result.prompt_path == "users/discord_user002.md"


@pytest.mark.unit
def test_resolve_user_prompt_path_does_not_fall_back_to_role(tmp_path: Path) -> None:
    """prompt_path is None when user has no override; role_prompt_path carries the role path.

    prompt_path must NOT fall back to the role-level path — it stays None.
    The role path is available separately on role_prompt_path.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    # usr_user001 has no prompt_path, role=user, role user has "roles/user.md"
    result = registry.resolve_user("discord:user001", "discord")

    assert result is not None
    assert result.prompt_path is None
    assert result.role_prompt_path == "roles/user.md"


@pytest.mark.unit
def test_resolve_user_prompt_path_none_when_neither_set(tmp_path: Path) -> None:
    """prompt_path is None when neither user nor role specifies one.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    # admin role has prompt_path=null in fixture
    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.prompt_path is None


@pytest.mark.unit
def test_resolve_user_correct_display_name(tmp_path: Path) -> None:
    """resolve_user returns UserRecord with the correct display_name field.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
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
    path = _write_portail_yaml(tmp_path)
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
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord")

    assert result is not None
    assert result.blocked is False


# ---------------------------------------------------------------------------
# UserRegistry.resolve_user — unknown user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_returns_none_for_unknown_user(tmp_path: Path) -> None:
    """resolve_user returns None for a sender_id not listed in portail.yaml.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:9999999", "discord")

    assert result is None


@pytest.mark.unit
def test_resolve_user_returns_none_for_unknown_channel(tmp_path: Path) -> None:
    """resolve_user returns None when user exists on another channel but not this one.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
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
    path = _write_portail_yaml(tmp_path)
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
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:admin001", "discord", context="server")

    assert result is not None
    assert result.role == "admin"


# ---------------------------------------------------------------------------
# UserRegistry — sender_index fast lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_user_by_sender_index_format(tmp_path: Path) -> None:
    """resolve_user works with 'channel:raw_id' sender_id format (telegram).

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("telegram:admin001", "telegram")

    assert result is not None
    assert result.role == "admin"


# ---------------------------------------------------------------------------
# UserRegistry.build_guest_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_guest_record_returns_user_record(tmp_path: Path) -> None:
    """build_guest_record returns a UserRecord instance.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    assert isinstance(result, UserRecord)


@pytest.mark.unit
def test_build_guest_record_has_guest_role(tmp_path: Path) -> None:
    """build_guest_record returns record with role='guest'.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    assert result.role == "guest"


@pytest.mark.unit
def test_build_guest_record_has_no_llm_profile(tmp_path: Path) -> None:
    """build_guest_record returns a record without llm_profile field.

    llm_profile is now stamped directly into envelope.metadata by Portail.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    assert not hasattr(result, "llm_profile")


@pytest.mark.unit
def test_build_guest_record_empty_actions(tmp_path: Path) -> None:
    """build_guest_record returns record with empty actions (no slash commands).

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    assert result.actions == []


@pytest.mark.unit
def test_build_guest_record_not_blocked(tmp_path: Path) -> None:
    """build_guest_record returns record with blocked=False.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    assert result.blocked is False


@pytest.mark.unit
def test_build_guest_record_inherits_guest_role_skills(tmp_path: Path) -> None:
    """build_guest_record uses skills_dirs from the guest role config.

    When the guest role has skills_dirs=[], the record must reflect that.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.build_guest_record()

    # guest role in fixture has skills_dirs: []
    assert result.skills_dirs == []


@pytest.mark.unit
def test_build_guest_record_without_config_returns_minimal_record() -> None:
    """build_guest_record works even without a config file (permissive mode).

    Must return a valid UserRecord with sensible defaults.
    """
    registry = UserRegistry(config_path=Path("/nonexistent/portail.yaml"))

    result = registry.build_guest_record()

    assert isinstance(result, UserRecord)
    assert result.role == "guest"
    assert not hasattr(result, "llm_profile")
    assert result.blocked is False


# ---------------------------------------------------------------------------
# UserRegistry — permissive mode (missing portail.yaml)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_permissive_mode_returns_none_for_any_user() -> None:
    """When portail.yaml is absent, resolve_user returns None (no crash).

    The registry must not raise even when the config file does not exist.
    """
    registry = UserRegistry(config_path=Path("/nonexistent/portail.yaml"))

    result = registry.resolve_user("discord:anyone", "discord")

    assert result is None


@pytest.mark.unit
def test_permissive_mode_does_not_raise_on_init() -> None:
    """UserRegistry init must not raise when portail.yaml is absent."""
    registry = UserRegistry(config_path=Path("/nonexistent/path/portail.yaml"))
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
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("", "discord")

    assert result is None


@pytest.mark.unit
def test_resolve_user_no_colon_in_sender_id_returns_none(tmp_path: Path) -> None:
    """resolve_user returns None when sender_id has no 'channel:id' separator.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("malformed_sender", "discord")

    assert result is None


@pytest.mark.unit
def test_resolve_user_path_traversal_rejected(tmp_path: Path) -> None:
    """prompt_path with directory traversal (../) is rejected.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    content = dedent("""\
        unknown_user_policy: deny
        guest_role: guest
        users:
          usr_traversal:
            display_name: "Traversal User"
            role: user
            blocked: false
            prompt_path: "../../etc/passwd"
            identifiers:
              discord:
                dm: "traversal001"
        roles:
          user:
            actions: []
            skills_dirs: []
            allowed_mcp_tools: []
            prompt_path: null
    """)
    path = _write_portail_yaml(tmp_path, content)
    registry = UserRegistry(config_path=path)

    result = registry.resolve_user("discord:traversal001", "discord")

    assert result is not None
    assert result.prompt_path is None
