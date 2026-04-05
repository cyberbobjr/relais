"""Tests for portail.main.Portail — hot-reload via _load(), reload_config(), and
the Redis Pub/Sub listener _config_reload_listener().

TDD — all tests are written before the implementation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PORTAIL_YAML_V1 = dedent("""\
    unknown_user_policy: deny
    guest_role: guest
    users:
      usr_alice:
        display_name: Alice
        role: admin
        identifiers:
          discord:
            dm: "alice001"
    roles:
      admin:
        actions: ["*"]
        skills_dirs: []
        allowed_mcp_tools: []
        allowed_subagents: []
""")

_PORTAIL_YAML_V2 = dedent("""\
    unknown_user_policy: guest
    guest_role: guest
    users:
      usr_alice:
        display_name: Alice
        role: admin
        identifiers:
          discord:
            dm: "alice001"
      usr_bob:
        display_name: Bob
        role: user
        identifiers:
          discord:
            dm: "bob002"
    roles:
      admin:
        actions: ["*"]
        skills_dirs: []
        allowed_mcp_tools: []
        allowed_subagents: []
      user:
        actions: ["chat"]
        skills_dirs: []
        allowed_mcp_tools: []
        allowed_subagents: []
""")

_PORTAIL_YAML_BROKEN = "unknown_user_policy: [invalid yaml: {"


def _make_portail_with_yaml(yaml_content: str, tmp_path: Path):
    """Build a Portail instance with a temporary portail.yaml.

    Args:
        yaml_content: YAML text to write as portail.yaml.
        tmp_path: pytest tmp_path fixture directory.

    Returns:
        A Portail instance with mocked Redis client.
    """
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    config_file = tmp_path / "portail.yaml"
    config_file.write_text(yaml_content, encoding="utf-8")

    with patch.object(Portail, "__init__", lambda self: None):
        portail = Portail.__new__(Portail)
        portail.client = MagicMock()
        portail.stream_in = "relais:messages:incoming"
        portail.stream_out = "relais:security"
        portail.group_name = "portail_group"
        portail.consumer_name = "portail_1"
        portail._config_lock = asyncio.Lock()
        portail._config_path = config_file
        portail._load()

    return portail, config_file


# ---------------------------------------------------------------------------
# _load() — initial load
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_portail_load_sets_user_registry(tmp_path: Path) -> None:
    """_load() constructs a UserRegistry that resolves known users."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    assert portail._user_registry is not None
    record = portail._user_registry.resolve_user("discord:alice001", "discord")
    assert record is not None
    assert record.display_name == "Alice"


@pytest.mark.unit
def test_portail_load_sets_guest_role(tmp_path: Path) -> None:
    """_load() reads guest_role from YAML into self._guest_role."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)
    assert portail._guest_role == "guest"


@pytest.mark.unit
def test_portail_load_sets_unknown_user_policy(tmp_path: Path) -> None:
    """_load() reads unknown_user_policy from YAML into self._unknown_user_policy."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)
    assert portail._unknown_user_policy == "deny"


# ---------------------------------------------------------------------------
# reload_config() — success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_returns_true_on_success(tmp_path: Path) -> None:
    """reload_config() returns True when the YAML file is valid."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    result = await portail.reload_config()

    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_updates_user_registry(tmp_path: Path) -> None:
    """reload_config() updates _user_registry so new users are resolvable."""
    portail, config_file = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    # Bob does NOT exist in V1
    assert portail._user_registry.resolve_user("discord:bob002", "discord") is None

    # Overwrite config with V2 (adds Bob)
    config_file.write_text(_PORTAIL_YAML_V2, encoding="utf-8")

    result = await portail.reload_config()

    assert result is True
    record = portail._user_registry.resolve_user("discord:bob002", "discord")
    assert record is not None
    assert record.display_name == "Bob"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_updates_unknown_user_policy(tmp_path: Path) -> None:
    """reload_config() updates _unknown_user_policy to match new YAML."""
    portail, config_file = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)
    assert portail._unknown_user_policy == "deny"

    config_file.write_text(_PORTAIL_YAML_V2, encoding="utf-8")
    await portail.reload_config()

    assert portail._unknown_user_policy == "guest"


# ---------------------------------------------------------------------------
# reload_config() — failure path (broken YAML)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_returns_false_on_broken_yaml(tmp_path: Path) -> None:
    """reload_config() returns False when the YAML file is malformed."""
    portail, config_file = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    config_file.write_text(_PORTAIL_YAML_BROKEN, encoding="utf-8")

    result = await portail.reload_config()

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_preserves_old_registry_on_failure(tmp_path: Path) -> None:
    """On reload failure, the previous UserRegistry is kept intact."""
    portail, config_file = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    old_registry = portail._user_registry
    config_file.write_text(_PORTAIL_YAML_BROKEN, encoding="utf-8")

    await portail.reload_config()

    # Registry object must be the same instance — not replaced
    assert portail._user_registry is old_registry


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_reload_config_preserves_policy_on_failure(tmp_path: Path) -> None:
    """On reload failure, _unknown_user_policy is not changed."""
    portail, config_file = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    config_file.write_text(_PORTAIL_YAML_BROKEN, encoding="utf-8")
    await portail.reload_config()

    assert portail._unknown_user_policy == "deny"


# ---------------------------------------------------------------------------
# _config_reload_listener — pub/sub integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_listener_calls_reload_on_reload_message(tmp_path: Path) -> None:
    """_config_reload_listener calls reload_config() when it receives 'reload'."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    portail.reload_config = fake_reload

    # Build a mock pubsub that yields one 'reload' message then stops
    mock_pubsub = AsyncMock()
    mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
    mock_pubsub.__aexit__ = AsyncMock(return_value=None)

    messages = [
        {"type": "message", "data": b"reload"},
    ]
    msg_iter = iter(messages)

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await portail._config_reload_listener(mock_redis)

    assert reload_called == [True], "reload_config must be called exactly once"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_listener_ignores_non_reload_messages(tmp_path: Path) -> None:
    """_config_reload_listener does NOT call reload_config for non-'reload' payloads."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    portail.reload_config = fake_reload

    messages = [
        {"type": "message", "data": b"restart"},
        {"type": "message", "data": b"RELOAD"},  # case-sensitive
        {"type": "subscribe", "data": 1},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await portail._config_reload_listener(mock_redis)

    assert reload_called == [], "reload_config must NOT be called for non-'reload' payloads"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_listener_subscribes_to_correct_channel(tmp_path: Path) -> None:
    """_config_reload_listener subscribes to relais:config:reload:portail."""
    portail, _ = _make_portail_with_yaml(_PORTAIL_YAML_V1, tmp_path)

    subscribe_calls: list[str] = []

    async def fake_listen():
        return
        yield  # make it a generator

    mock_pubsub = AsyncMock()

    async def capture_subscribe(channel: str) -> None:
        subscribe_calls.append(channel)

    mock_pubsub.subscribe = capture_subscribe
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await portail._config_reload_listener(mock_redis)

    assert "relais:config:reload:portail" in subscribe_calls
