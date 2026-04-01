"""Tests for portail.main.Portail — enrichment, DND, command drop, session tracking.

TDD — tests written BEFORE implementation changes (RED phase for new behaviour).
These tests verify the _enrich_envelope method and its integration in _process_stream.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "hello world",
    sender_id: str = "discord:admin001",
    channel: str = "discord",
    metadata: dict | None = None,
) -> Envelope:
    """Build a minimal Envelope for Portail enrichment tests.

    Args:
        content: Message body.
        sender_id: Originating user identifier.
        channel: Originating channel.
        metadata: Optional extra metadata fields.

    Returns:
        A valid Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        metadata=metadata or {},
    )


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
      usr_user001:
        display_name: "Regular User"
        role: user
        blocked: false
        custom_prompt_path: "users/discord_user001.md"
        identifiers:
          discord:
            dm: "user001"
    roles:
      admin:
        actions: ["send", "admin"]
      user:
        actions: ["send"]
""")


def _write_users_yaml(tmp_path: Path, content: str = _USERS_YAML) -> Path:
    """Write a users.yaml file to the given temporary directory.

    Args:
        tmp_path: pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    p = tmp_path / "users.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_portail_with_registry(users_yaml_path: Path | None = None):
    """Construct a Portail instance with a real UserRegistry and mocked Redis.

    Args:
        users_yaml_path: Optional path to users.yaml for the registry.

    Returns:
        A Portail instance ready for unit testing.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry

    with patch("portail.main.RedisClient"):
        portail = Portail.__new__(Portail)
        portail.stream_in = "relais:messages:incoming"
        portail.stream_out = "relais:security"
        portail.group_name = "portail_group"
        portail.consumer_name = "portail_1"
        portail._dnd_cached = False
        portail._dnd_cache_at = 0.0

        if users_yaml_path is not None:
            portail._user_registry = UserRegistry(config_path=users_yaml_path)
        else:
            portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    return portail


# ---------------------------------------------------------------------------
# _enrich_envelope — known user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_stamps_user_role(tmp_path: Path) -> None:
    """_enrich_envelope stamps user_role into envelope.metadata for known users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.metadata.get("user_role") == "admin"


@pytest.mark.unit
def test_enrich_envelope_stamps_display_name(tmp_path: Path) -> None:
    """_enrich_envelope stamps display_name into envelope.metadata for known users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.metadata.get("display_name") == "Admin User"


@pytest.mark.unit
def test_enrich_envelope_stamps_custom_prompt_path_when_present(tmp_path: Path) -> None:
    """_enrich_envelope stamps custom_prompt_path when it is set in the registry.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:user001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.metadata.get("custom_prompt_path") == "users/discord_user001.md"


@pytest.mark.unit
def test_enrich_envelope_does_not_stamp_custom_prompt_path_when_none(tmp_path: Path) -> None:
    """_enrich_envelope does NOT add custom_prompt_path key when UserRecord has None.

    Keys with None values must not be stamped — absent is cleaner than null.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    # admin001 has no custom_prompt_path
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert "custom_prompt_path" not in envelope.metadata


# ---------------------------------------------------------------------------
# _enrich_envelope — llm_profile resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_llm_profile_uses_channel_profile(tmp_path: Path) -> None:
    """_enrich_envelope sets llm_profile from channel_profile when present.

    Priority: channel_profile (from Aiguilleur) > 'default'.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(
        sender_id="discord:admin001",
        channel="discord",
        metadata={"channel_profile": "fast"},
    )

    portail._enrich_envelope(envelope)

    assert envelope.metadata["llm_profile"] == "fast"


@pytest.mark.unit
def test_enrich_envelope_llm_profile_defaults_to_default_when_no_channel_profile(
    tmp_path: Path,
) -> None:
    """_enrich_envelope sets llm_profile='default' when channel_profile is absent.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.metadata["llm_profile"] == "default"


@pytest.mark.unit
def test_enrich_envelope_llm_profile_fallback_when_channel_profile_is_none(
    tmp_path: Path,
) -> None:
    """_enrich_envelope uses 'default' when channel_profile is explicitly None.

    A None value for channel_profile must not be propagated to llm_profile.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(
        sender_id="discord:admin001",
        channel="discord",
        metadata={"channel_profile": None},
    )

    portail._enrich_envelope(envelope)

    assert envelope.metadata["llm_profile"] == "default"


# ---------------------------------------------------------------------------
# _enrich_envelope — unknown user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_unknown_user_does_not_stamp_user_role(tmp_path: Path) -> None:
    """_enrich_envelope leaves user identity fields absent when user is not found.

    For unknown senders, no user_role, display_name, or custom_prompt_path
    should be added.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")

    portail._enrich_envelope(envelope)

    assert "user_role" not in envelope.metadata
    assert "display_name" not in envelope.metadata


@pytest.mark.unit
def test_enrich_envelope_unknown_user_still_stamps_llm_profile(tmp_path: Path) -> None:
    """_enrich_envelope stamps llm_profile even for unknown users.

    The llm_profile must be resolved regardless of whether the user is known.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_users_yaml(tmp_path)
    portail = _make_portail_with_registry(path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.metadata.get("llm_profile") == "default"


# ---------------------------------------------------------------------------
# _process_stream — enrichment integrated in pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_enriches_and_forwards_to_security(tmp_path: Path) -> None:
    """_process_stream enriches metadata and forwards to relais:security.

    The forwarded envelope must contain user_role and llm_profile.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    path = _write_users_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=None)   # DND off
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=path)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    # Find the xadd call to relais:security
    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    assert forwarded["metadata"].get("user_role") == "admin"
    assert forwarded["metadata"].get("llm_profile") == "default"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_drops_dnd_message(tmp_path: Path) -> None:
    """DND mode: message is not forwarded to relais:security.

    The message must be dropped (and ACKed) when DND is active.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=b"1")   # DND active
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_drops_command_message(tmp_path: Path) -> None:
    """Command messages (/clear etc.) are not forwarded to relais:security.

    Commands are delegated to Commandant and must be dropped by Portail.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    envelope = _make_envelope(
        content="/clear",
        sender_id="discord:admin001",
        channel="discord",
    )
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=None)   # DND off
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_calls_update_active_sessions(tmp_path: Path) -> None:
    """_process_stream calls _update_active_sessions (not the old _update_session).

    The Redis HSET must be called with last_seen, channel, and session_id fields.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    path = _write_users_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=None)   # DND off
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=path)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    # HSET must have been called with the session key and a mapping dict
    hset_calls = redis_conn.hset.await_args_list
    assert len(hset_calls) >= 1
    first_call = hset_calls[0]
    key_arg = first_call.args[0]
    assert "relais:active_sessions:" in key_arg
    mapping = first_call.kwargs.get("mapping", {})
    assert "last_seen" in mapping
    assert "channel" in mapping
    assert "session_id" in mapping


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_acks_every_message_on_dnd_drop(tmp_path: Path) -> None:
    """Every consumed message is ACKed regardless of processing outcome.

    XACK must be called in the finally block even for DND drops.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=b"1")   # DND active → drop
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    await portail._process_stream(redis_conn, shutdown=shutdown)

    redis_conn.xack.assert_awaited_once_with(
        "relais:messages:incoming", "portail_group", b"1-0"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_unknown_user_still_forwards(tmp_path: Path) -> None:
    """Unknown users are still forwarded to relais:security (ACL decides fate).

    Portail only enriches — it does not block unknown users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.main import Portail
    from common.user_registry import UserRegistry
    from common.shutdown import GracefulShutdown

    path = _write_users_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        [],
    ])
    redis_conn.get = AsyncMock(return_value=None)   # DND off
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    shutdown = MagicMock(spec=GracefulShutdown)
    shutdown.is_stopping.side_effect = [False, False, True]

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=path)

    await portail._process_stream(redis_conn, shutdown=shutdown)

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1
