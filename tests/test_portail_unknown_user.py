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
    unknown_user_policy: guest
    guest_role: guest
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
    p = tmp_path / "portail.yaml"
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
    guest_role: str = "guest",
) -> object:
    """Construct a Portail with a real registry, mocked Redis, and given policy.

    Uses ``Portail.__new__`` to bypass ``__init__`` so we can inject precise
    state without connecting to Redis.

    Args:
        users_yaml_path: Path to the portail.yaml to use.
        policy: Value for ``_unknown_user_policy`` (deny | guest | pending).
        guest_role: Value for ``_guest_role``.

    Returns:
        A Portail instance ready for unit testing.
    """
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    with patch("portail.main.RedisClient"):
        portail = Portail.__new__(Portail)

    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._unknown_user_policy = policy
    portail._guest_role = guest_role
    portail._user_registry = UserRegistry(config_path=users_yaml_path)

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
    redis_conn.get = AsyncMock(return_value=None)
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
    portail = _make_portail(path, policy="guest", guest_role="guest")
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
    portail = _make_portail(path, policy="guest", guest_role="guest")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("user_record", {}).get("role") == "guest"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_display_name_guest(tmp_path: Path) -> None:
    """guest policy: forwarded envelope has display_name='Guest' in metadata.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_role="guest")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("user_record", {}).get("display_name") == "Guest"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_llm_profile_in_metadata(tmp_path: Path) -> None:
    """guest policy: llm_profile is stamped directly in envelope.metadata.

    With no channel_profile present, llm_profile defaults to "default".
    The guest role's llm_profile field in portail.yaml is ignored.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_role="guest")
    envelope = _make_envelope(sender_id="discord:unknown999")
    redis_conn = _build_redis_conn(envelope.to_json())
    shutdown = _make_shutdown(stop_after=2)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("llm_profile") == "default"
    assert "llm_profile" not in forwarded["metadata"].get("user_record", {})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_guest_policy_stamps_skills_from_guest_role(tmp_path: Path) -> None:
    """guest policy: skills_dirs and allowed_mcp_tools are resolved from the 'guest' role.

    When the 'guest' role exists in roles.yaml, its configuration is applied.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_yaml(tmp_path, _USERS_YAML_WITH_GUEST_ROLE)
    portail = _make_portail(path, policy="guest", guest_role="guest")
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
    user_record = forwarded["metadata"].get("user_record", {})
    assert user_record.get("skills_dirs") == []
    assert user_record.get("allowed_mcp_tools") == []


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
    portail = _make_portail(path, policy="guest", guest_role="guest")
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
    user_record = forwarded["metadata"].get("user_record", {})
    assert user_record.get("skills_dirs") == [], (
        "fail-closed: skills_dirs must be [] when guest role is absent"
    )
    assert user_record.get("allowed_mcp_tools") == [], (
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
    portail = _make_portail(path, policy="guest", guest_role="guest")
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
    portail = _make_portail(path, policy="guest", guest_role="guest")
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
def test_portail_init_loads_unknown_user_policy_from_portail_yaml(tmp_path: Path) -> None:
    """Portail.__init__ reads unknown_user_policy and guest_role from portail.yaml.

    Verifies that both attributes are populated at construction time from UserRegistry
    (which reads portail.yaml top-level fields).

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import yaml

    portail_yaml_content = {
        "unknown_user_policy": "guest",
        "guest_role": "vip",
        "users": {},
        "roles": {},
    }
    portail_yaml_file = tmp_path / "portail.yaml"
    portail_yaml_file.write_text(yaml.dump(portail_yaml_content), encoding="utf-8")

    with patch("portail.main.RedisClient"):
        from portail.main import Portail
        from portail.user_registry import UserRegistry
        portail = Portail.__new__(Portail)
        portail.stream_in = "relais:messages:incoming"
        portail.stream_out = "relais:security"
        portail.group_name = "portail_group"
        portail.consumer_name = "portail_1"
        portail._user_registry = UserRegistry(config_path=portail_yaml_file)
        portail._guest_role = portail._user_registry.guest_role
        portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    assert portail._unknown_user_policy == "guest"
    assert portail._guest_role == "vip"


@pytest.mark.unit
def test_portail_init_defaults_to_deny_when_portail_yaml_missing() -> None:
    """Portail defaults to 'deny' when portail.yaml is absent (permissive mode).

    UserRegistry falls back to permissive mode (deny default) when config is missing.

    Args: (none)
    """
    from portail.user_registry import UserRegistry

    registry = UserRegistry(config_path=Path("/nonexistent/portail.yaml"))

    assert registry.unknown_user_policy == "deny"
    assert registry.guest_role == "guest"
