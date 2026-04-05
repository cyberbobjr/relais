"""Tests for portail.main.Portail — enrichment stamps user_record dict.

TDD — tests verify the new _enrich_envelope behaviour: a single
``user_record`` dict is stamped into envelope.context[CTX_PORTAIL] instead
of individual keys.  Config format is portail.yaml (users + roles).
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL
from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "hello world",
    sender_id: str = "discord:admin001",
    channel: str = "discord",
    context: dict | None = None,
) -> Envelope:
    """Build a minimal Envelope for Portail enrichment tests.

    Args:
        content: Message body.
        sender_id: Originating user identifier.
        channel: Originating channel.
        context: Optional context dict (namespaced by brick).

    Returns:
        A valid Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-001",
        context=context or {},
    )


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
      usr_user001:
        display_name: "Regular User"
        role: user
        blocked: false
        prompt_path: "users/discord_user001.md"
        identifiers:
          discord:
            dm: "user001"

    roles:
      admin:
        actions: ["*"]
        skills_dirs: ["*"]
        allowed_mcp_tools: ["*"]
        prompt_path: null
      user:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []
        prompt_path: null
      guest:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []
        prompt_path: null
""")


def _write_portail_yaml(tmp_path: Path, content: str = _PORTAIL_YAML) -> Path:
    """Write a portail.yaml file to the given temporary directory.

    Args:
        tmp_path: pytest temporary directory fixture.
        content: YAML content to write.

    Returns:
        Path to the created file.
    """
    p = tmp_path / "portail.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_portail(portail_yaml_path: Path | None = None):
    """Construct a Portail instance with a real UserRegistry and mocked Redis.

    Args:
        portail_yaml_path: Optional path to portail.yaml for the registry.

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

        if portail_yaml_path is not None:
            portail._user_registry = UserRegistry(config_path=portail_yaml_path)
        else:
            portail._user_registry = UserRegistry(
                config_path=Path("/nonexistent/portail.yaml")
            )
        portail._guest_role = portail._user_registry.guest_role
        portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    return portail


# ---------------------------------------------------------------------------
# _enrich_envelope — stamps user_record dict for known users
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_stamps_user_record_key(tmp_path: Path) -> None:
    """_enrich_envelope stamps a 'user_record' key into envelope.context[CTX_PORTAIL].

    The value must be a dict (JSON-serialisable).

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert CTX_PORTAIL in envelope.context
    assert "user_record" in envelope.context[CTX_PORTAIL]
    assert isinstance(envelope.context[CTX_PORTAIL]["user_record"], dict)


@pytest.mark.unit
def test_enrich_envelope_user_record_role(tmp_path: Path) -> None:
    """user_record['role'] must equal the user's role from portail.yaml.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["role"] == "admin"


@pytest.mark.unit
def test_enrich_envelope_user_record_display_name(tmp_path: Path) -> None:
    """user_record['display_name'] must match the configured display name.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["display_name"] == "Admin User"


@pytest.mark.unit
def test_enrich_envelope_user_record_actions_wildcard_for_admin(tmp_path: Path) -> None:
    """user_record['actions'] equals ['*'] for admin role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["actions"] == ["*"]


@pytest.mark.unit
def test_enrich_envelope_user_record_skills_dirs_for_admin(tmp_path: Path) -> None:
    """user_record['skills_dirs'] equals ['*'] for admin role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["skills_dirs"] == ["*"]


@pytest.mark.unit
def test_enrich_envelope_user_record_mcp_tools_for_admin(tmp_path: Path) -> None:
    """user_record['allowed_mcp_tools'] equals ['*'] for admin role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["allowed_mcp_tools"] == ["*"]


@pytest.mark.unit
def test_enrich_envelope_user_record_prompt_path_when_set(tmp_path: Path) -> None:
    """user_record['prompt_path'] equals the user-level prompt path when present.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    # user001 has prompt_path="users/discord_user001.md"
    envelope = _make_envelope(sender_id="discord:user001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["prompt_path"] == "users/discord_user001.md"


@pytest.mark.unit
def test_enrich_envelope_user_record_prompt_path_none_for_admin(tmp_path: Path) -> None:
    """user_record['prompt_path'] is None when neither user nor role sets it.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["prompt_path"] is None


@pytest.mark.unit
def test_enrich_envelope_user_record_empty_skills_for_user_role(tmp_path: Path) -> None:
    """user_record['skills_dirs'] equals [] for the 'user' role.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:user001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["skills_dirs"] == []
    assert envelope.context[CTX_PORTAIL]["user_record"]["allowed_mcp_tools"] == []


# ---------------------------------------------------------------------------
# _enrich_envelope — individual legacy keys must NOT be present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_no_legacy_user_role_key(tmp_path: Path) -> None:
    """_enrich_envelope must NOT stamp 'user_role' as a top-level metadata key.

    All user data is now under 'user_record'.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert "user_role" not in envelope.context.get(CTX_PORTAIL, {})


@pytest.mark.unit
def test_enrich_envelope_no_legacy_display_name_key(tmp_path: Path) -> None:
    """_enrich_envelope must NOT stamp 'display_name' as a top-level metadata key.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert "display_name" not in envelope.context.get(CTX_PORTAIL, {})


@pytest.mark.unit
def test_enrich_envelope_no_legacy_skills_dirs_key(tmp_path: Path) -> None:
    """_enrich_envelope must NOT stamp 'skills_dirs' as a top-level metadata key.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert "skills_dirs" not in envelope.context.get(CTX_PORTAIL, {})


@pytest.mark.unit
def test_enrich_envelope_no_legacy_custom_prompt_path_key(tmp_path: Path) -> None:
    """_enrich_envelope must NOT stamp 'custom_prompt_path' as top-level.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:user001", channel="discord")

    portail._enrich_envelope(envelope)

    assert "custom_prompt_path" not in envelope.context.get(CTX_PORTAIL, {})


# ---------------------------------------------------------------------------
# _enrich_envelope — llm_profile resolution (still at top level for routing)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_llm_profile_uses_channel_profile(tmp_path: Path) -> None:
    """envelope.context[CTX_PORTAIL]['llm_profile'] uses channel_profile when present.

    The channel_profile stamped by Aiguilleur in CTX_AIGUILLEUR is resolved to llm_profile
    in CTX_PORTAIL (not inside user_record).

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(
        sender_id="discord:admin001",
        channel="discord",
        context={CTX_AIGUILLEUR: {"channel_profile": "coder"}},
    )

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["llm_profile"] == "coder"
    assert "llm_profile" not in envelope.context[CTX_PORTAIL].get("user_record", {})


@pytest.mark.unit
def test_enrich_envelope_llm_profile_defaults_to_default(tmp_path: Path) -> None:
    """envelope.context[CTX_PORTAIL]['llm_profile'] defaults to 'default' when channel_profile absent.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context[CTX_PORTAIL]["llm_profile"] == "default"
    assert "llm_profile" not in envelope.context[CTX_PORTAIL].get("user_record", {})


# ---------------------------------------------------------------------------
# _enrich_envelope — unknown user (no user_record stamped)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_unknown_user_no_user_record(tmp_path: Path) -> None:
    """_enrich_envelope does NOT stamp user_record for unknown users.

    Downstream (Portail's _process_stream) applies the unknown_user_policy.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")

    portail._enrich_envelope(envelope)

    assert CTX_PORTAIL not in envelope.context


# ---------------------------------------------------------------------------
# _apply_guest_stamps — stamps user_record for guest policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_guest_stamps_sets_user_record(tmp_path: Path) -> None:
    """_apply_guest_stamps stamps 'user_record' dict for guest users.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")

    portail._apply_guest_stamps(envelope)

    assert CTX_PORTAIL in envelope.context
    assert envelope.context[CTX_PORTAIL]["user_record"]["role"] == "guest"


@pytest.mark.unit
def test_apply_guest_stamps_uses_guest_role(tmp_path: Path) -> None:
    """_apply_guest_stamps uses the configured guest_role; llm_profile falls back to 'default'.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    path = _write_portail_yaml(tmp_path)
    portail = _make_portail(path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")

    portail._apply_guest_stamps(envelope)

    assert envelope.context[CTX_PORTAIL]["user_record"]["role"] == "guest"
    assert envelope.context[CTX_PORTAIL]["llm_profile"] == "default"
    assert "llm_profile" not in envelope.context[CTX_PORTAIL].get("user_record", {})


# ---------------------------------------------------------------------------
# _process_stream — integration checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_enriches_with_user_record(tmp_path: Path) -> None:
    """_run_stream_loop enriches metadata with user_record and forwards to relais:security.

    The forwarded envelope must contain a 'user_record' dict with role and llm_profile.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import asyncio
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    path = _write_portail_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._user_registry = UserRegistry(config_path=path)
    portail._guest_role = portail._user_registry.guest_role
    portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    spec = portail.stream_specs()[0]
    shutdown_event = asyncio.Event()
    try:
        await portail._run_stream_loop(spec, redis_conn, shutdown_event)
    except asyncio.CancelledError:
        pass

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 1
    forwarded = json.loads(security_calls[0].args[1]["payload"])
    portail_ctx = forwarded["context"].get("portail", {})
    ur = portail_ctx.get("user_record")
    assert ur is not None
    assert ur["role"] == "admin"
    assert "llm_profile" not in ur
    assert portail_ctx["llm_profile"] == "default"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_check_uses_user_record_key(tmp_path: Path) -> None:
    """_run_stream_loop checks for 'user_record' (not 'user_role') to detect known users.

    An envelope with a valid user must be forwarded; the check must use
    'user_record' presence.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import asyncio
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    path = _write_portail_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._user_registry = UserRegistry(config_path=path)
    portail._guest_role = portail._user_registry.guest_role
    portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    spec = portail.stream_specs()[0]
    shutdown_event = asyncio.Event()
    try:
        await portail._run_stream_loop(spec, redis_conn, shutdown_event)
    except asyncio.CancelledError:
        pass

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    # Known user → forwarded (not dropped)
    assert len(security_calls) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_unknown_user_dropped_by_deny_policy(tmp_path: Path) -> None:
    """Unknown users are dropped when unknown_user_policy='deny' (default).

    With the deny policy, Portail blocks unknown senders before Sentinelle.
    The message is ACKed but not forwarded to relais:security.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import asyncio
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    path = _write_portail_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:9999999", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._user_registry = UserRegistry(config_path=path)
    portail._guest_role = portail._user_registry.guest_role
    portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    spec = portail.stream_specs()[0]
    shutdown_event = asyncio.Event()
    try:
        await portail._run_stream_loop(spec, redis_conn, shutdown_event)
    except asyncio.CancelledError:
        pass

    security_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:security"
    ]
    assert len(security_calls) == 0, "deny policy must drop unknown users"
    redis_conn.xack.assert_awaited_once_with(
        "relais:messages:incoming", "portail_group", b"1-0"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_stream_calls_update_active_sessions(tmp_path: Path) -> None:
    """_run_stream_loop calls _update_active_sessions with correct fields.

    The Redis HSET must be called with last_seen, channel, and session_id.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    import asyncio
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    path = _write_portail_yaml(tmp_path)
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")
    payload = envelope.to_json()

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:messages:incoming", [(b"1-0", {"payload": payload})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xadd = AsyncMock(return_value=b"2-0")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.hset = AsyncMock(return_value=1)
    redis_conn.expire = AsyncMock(return_value=1)

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._user_registry = UserRegistry(config_path=path)
    portail._guest_role = portail._user_registry.guest_role
    portail._unknown_user_policy = portail._user_registry.unknown_user_policy

    spec = portail.stream_specs()[0]
    shutdown_event = asyncio.Event()
    try:
        await portail._run_stream_loop(spec, redis_conn, shutdown_event)
    except asyncio.CancelledError:
        pass

    hset_calls = redis_conn.hset.await_args_list
    assert len(hset_calls) >= 1
    first_call = hset_calls[0]
    key_arg = first_call.args[0]
    assert "relais:active_sessions:" in key_arg
    mapping = first_call.kwargs.get("mapping", {})
    assert "last_seen" in mapping
    assert "channel" in mapping
    assert "session_id" in mapping
