"""Tests TDD — Portail unknown_user_policy (deny / guest / pending).

Tests written FIRST (RED phase) before any implementation.

Covers three policies declared in config.yaml:
  - deny:    drop silently, no forward to relais:security
  - guest:   stamp synthetic guest identity, forward to relais:security
  - pending: drop + publish to relais:admin:pending_users
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from common.shutdown import GracefulShutdown


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

_USERS_YAML_WITH_GUEST_ROLE = dedent("""\
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
        skills_dirs: ["*"]
        allowed_mcp_tools: ["*"]
      guest:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []
""")

_USERS_YAML_WITHOUT_GUEST_ROLE = dedent("""\
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
        skills_dirs: ["*"]
        allowed_mcp_tools: ["*"]
""")


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write a YAML file to a temporary directory.

    Args:
        tmp_path: pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    p = tmp_path / "users.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    sender_id: str = "discord:unknown999",
    channel: str = "discord",
) -> Envelope:
    """Build a minimal Envelope for unknown-user policy tests.

    Args:
        sender_id: Originating user identifier (should be unknown in registry).
        channel: Originating channel name.

    Returns:
        A valid Envelope instance.
    """
    return Envelope(
        content="hello world",
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        metadata={},
    )


def _make_portail(
    users_yaml_path: Path,
    policy: str,
    guest_profile: str = "fast",
) -> object:
    """Construct a Portail with a real registry, mocked Redis, and given policy.

    Uses ``Portail.__new__`` to bypass ``__init__`` so we can inject precise
    state without connecting to Redis.

    Args:
        users_yaml_path: Path to the users.yaml to use.
        policy: Value for ``_unknown_user_policy`` (deny | guest | pending).
        guest_profile: Value for ``_guest_profile``.

    Returns:
        A Portail instance ready for unit testing.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.role_registry import RoleRegistry

    with patch("portail.main.RedisClient"):
        portail = Portail.__new__(Portail)

    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = False
    portail._dnd_cache_at = 0.0
    portail._unknown_user_policy = policy
    portail._guest_profile = guest_profile
    portail._user_registry = UserRegistry(config_path=users_yaml_path)
    portail._role_registry = RoleRegistry(config_path=users_yaml_path)

    return portail


def _build_redis_conn(payload: str) -> AsyncMock:
    """Return a Redis AsyncMock that yields one message then stops.

    Args:
        payload: JSON-serialised envelope payload to include in the stream.

    Returns:
        Configured AsyncMock for redis_conn.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(
        side_effect=[
            [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
            [],
        ]
    )
    redis_conn.get = AsyncMock(return_value=None)  # DND off
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)
    return redis_conn


def _make_shutdown(*, stop_after: int = 1) -> MagicMock:
    """Return a GracefulShutdown mock that stops after ``stop_after`` False returns.

    Args:
        stop_after: Number of False values before returning True.

    Returns:
        Configured MagicMock for GracefulShutdown.
    """
    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False] * stop_after + [True]
    return shutdown


# ---------------------------------------------------------------------------
# policy=deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deny_policy_drops_unknown_user_message(tmp_path: Path) -> None:
    """deny policy: unknown user message is NOT forwarded to relais:security.

    The message must be silently dropped — no xadd to the security stream.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="deny")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 0, "deny policy must not forward to relais:security"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deny_policy_acks_message(tmp_path: Path) -> None:
    """deny policy: message is ACKed even when dropped.

    The PEL must not accumulate dropped messages.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="deny")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    redis_conn.xack.assert_awaited_once_with(
        "relais:messages:incoming", "portail_group", b"1-0"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deny_policy_does_not_publish_to_pending(tmp_path: Path) -> None:
    """deny policy: no event is published to relais:admin:pending_users.

    The deny policy is silent — no admin notification stream.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="deny")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    pending_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:admin:pending_users"
    ]
    assert len(pending_calls) == 0, "deny policy must not write to pending_users stream"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_deny_policy_does_not_affect_known_users(tmp_path: Path) -> None:
    """deny policy only drops unknown users; known users are forwarded normally.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="deny")
    # admin001 is a known user in the registry
    envelope = _make_envelope(sender_id="discord:admin001")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1, "deny policy must still forward known users"


# ---------------------------------------------------------------------------
# policy=guest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_forwards_unknown_user_to_security(tmp_path: Path) -> None:
    """guest policy: unknown user message IS forwarded to relais:security.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1, "guest policy must forward to relais:security"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_user_role_guest(tmp_path: Path) -> None:
    """guest policy: forwarded envelope has user_role='guest' in metadata.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("user_role") == "guest"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_display_name_guest(tmp_path: Path) -> None:
    """guest policy: forwarded envelope has display_name='Guest' in metadata.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("display_name") == "Guest"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_llm_profile_from_guest_profile(tmp_path: Path) -> None:
    """guest policy: llm_profile is set to the configured guest_profile value.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("llm_profile") == "fast"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_skills_from_guest_role(tmp_path: Path) -> None:
    """guest policy: skills_dirs and allowed_mcp_tools are resolved from the 'guest' role.

    When the 'guest' role exists in roles.yaml, its configuration is applied.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    # 'guest' role in _USERS_YAML_WITH_GUEST_ROLE has empty skills_dirs/allowed_mcp_tools
    assert forwarded["metadata"].get("skills_dirs") == []
    assert forwarded["metadata"].get("allowed_mcp_tools") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_fail_closed_when_guest_role_absent(tmp_path: Path) -> None:
    """guest policy + absent 'guest' role: skills_dirs=[], allowed_mcp_tools=[] (fail-closed).

    When the registry has no 'guest' role, access must be fail-closed:
    both lists are empty rather than absent or unrestricted.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITHOUT_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1, "guest policy must still forward even without guest role"
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("skills_dirs") == [], (
        "fail-closed: skills_dirs must be [] when guest role is absent"
    )
    assert forwarded["metadata"].get("allowed_mcp_tools") == [], (
        "fail-closed: allowed_mcp_tools must be [] when guest role is absent"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_acks_message(tmp_path: Path) -> None:
    """guest policy: message is ACKed after being forwarded.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    redis_conn.xack.assert_awaited_once_with(
        "relais:messages:incoming", "portail_group", b"1-0"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_does_not_publish_to_pending(tmp_path: Path) -> None:
    """guest policy: no event is published to relais:admin:pending_users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_profile="fast")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    pending_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:admin:pending_users"
    ]
    assert len(pending_calls) == 0


# ---------------------------------------------------------------------------
# policy=pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pending_policy_drops_unknown_user_message(tmp_path: Path) -> None:
    """pending policy: unknown user message is NOT forwarded to relais:security.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="pending")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 0, "pending policy must not forward to relais:security"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pending_policy_publishes_to_pending_users_stream(tmp_path: Path) -> None:
    """pending policy: one event is published to relais:admin:pending_users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="pending")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    pending_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:admin:pending_users"
    ]
    assert len(pending_calls) == 1, "pending policy must write exactly one event to pending_users"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pending_policy_publishes_correct_fields(tmp_path: Path) -> None:
    """pending policy: published event contains sender_id, channel, correlation_id, timestamp.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="pending")
    envelope = _make_envelope(sender_id="discord:unknown999", channel="discord")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    pending_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:admin:pending_users"
    ]
    event = pending_calls[0].args[1]
    assert event.get("sender_id") == "discord:unknown999"
    assert event.get("channel") == "discord"
    assert event.get("correlation_id") == envelope.correlation_id
    assert "timestamp" in event


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pending_policy_acks_message(tmp_path: Path) -> None:
    """pending policy: message is ACKed even when dropped.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="pending")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    redis_conn.xack.assert_awaited_once_with(
        "relais:messages:incoming", "portail_group", b"1-0"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pending_policy_does_not_affect_known_users(tmp_path: Path) -> None:
    """pending policy only triggers for unknown users; known users are forwarded normally.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="pending")
    # admin001 is a known user in the registry
    envelope = _make_envelope(sender_id="discord:admin001")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1, "pending policy must still forward known users"

    pending_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:admin:pending_users"
    ]
    assert len(pending_calls) == 0, "known users must NOT be published to pending_users"


# ---------------------------------------------------------------------------
# __init__ loads policy from config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_portail_init_loads_unknown_user_policy_from_config(tmp_path: Path) -> None:
    """Portail.__init__ reads unknown_user_policy and guest_profile from config.yaml.

    Verifies that both attributes are populated at construction time from config.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import yaml

    config_content = {
        "security": {
            "unknown_user_policy": "guest",
            "guest_profile": "precise",
        }
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content), encoding="utf-8")

    with patch("portail.main.RedisClient"), \
         patch("portail.main.resolve_config_path", return_value=config_file):
        from portail.main import Portail
        portail = Portail()

    assert portail._unknown_user_policy == "guest"
    assert portail._guest_profile == "precise"


@pytest.mark.unit
def test_portail_init_defaults_to_deny_when_config_missing() -> None:
    """Portail defaults to 'deny' when config.yaml is absent or lacks security section.

    This ensures fail-closed behaviour at startup.

    Args: (none)
    """
    with patch("portail.main.RedisClient"), \
         patch("portail.main.resolve_config_path", side_effect=FileNotFoundError):
        from portail.main import Portail
        portail = Portail()

    assert portail._unknown_user_policy == "deny"
