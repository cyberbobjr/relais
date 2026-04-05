"""Tests for sentinelle.main.Sentinelle — hot-reload via _load(), reload_config(),
and the Redis Pub/Sub listener _config_reload_listener().

TDD — tests are written before the implementation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINELLE_YAML_V1 = dedent("""\
    access_control:
      default_mode: allowlist

    groups: []
""")

_SENTINELLE_YAML_V2 = dedent("""\
    access_control:
      default_mode: blocklist

    groups:
      - channel: discord
        group_id: "server001"
        allowed: true
        blocked: false
""")

_SENTINELLE_YAML_BROKEN = "access_control: [bad yaml: {"


def _make_sentinelle_with_yaml(yaml_content: str, tmp_path: Path):
    """Build a Sentinelle instance with a temporary sentinelle.yaml.

    Args:
        yaml_content: YAML text to write as sentinelle.yaml.
        tmp_path: pytest tmp_path fixture directory.

    Returns:
        Tuple of (Sentinelle instance, config file path).
    """
    from sentinelle.main import Sentinelle

    config_file = tmp_path / "sentinelle.yaml"
    config_file.write_text(yaml_content, encoding="utf-8")

    with MagicMock():
        pass  # no-op, just to avoid any import side effects

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.client = MagicMock()
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.stream_commands = "relais:commands"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"
    sentinelle.outgoing_group_name = "sentinelle_outgoing_group"
    sentinelle.outgoing_consumer_name = "sentinelle_outgoing_1"
    sentinelle._config_lock = asyncio.Lock()
    sentinelle._config_path = config_file
    sentinelle._load()

    return sentinelle, config_file


# ---------------------------------------------------------------------------
# _load() — initial load
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sentinelle_load_sets_acl_manager(tmp_path: Path) -> None:
    """_load() constructs an ACLManager from the config file."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)
    assert sentinelle._acl is not None


@pytest.mark.unit
def test_sentinelle_load_acl_in_allowlist_mode(tmp_path: Path) -> None:
    """_load() reads the allowlist mode from sentinelle.yaml."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)
    # allowlist mode — user_record=None → denied
    assert not sentinelle._acl.is_allowed(
        "discord:user1", "discord", user_record=None
    )


# ---------------------------------------------------------------------------
# reload_config() — success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_reload_config_returns_true_on_success(tmp_path: Path) -> None:
    """reload_config() returns True when the YAML file is valid."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    result = await sentinelle.reload_config()

    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_reload_config_updates_acl(tmp_path: Path) -> None:
    """reload_config() installs a new ACLManager with updated configuration."""
    sentinelle, config_file = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    old_acl = sentinelle._acl
    config_file.write_text(_SENTINELLE_YAML_V2, encoding="utf-8")

    result = await sentinelle.reload_config()

    assert result is True
    assert sentinelle._acl is not old_acl, "A new ACLManager instance must be installed"


# ---------------------------------------------------------------------------
# reload_config() — failure path (broken YAML)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_reload_config_returns_false_on_broken_yaml(tmp_path: Path) -> None:
    """reload_config() returns False when the YAML file is malformed."""
    sentinelle, config_file = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    config_file.write_text(_SENTINELLE_YAML_BROKEN, encoding="utf-8")

    result = await sentinelle.reload_config()

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_reload_config_preserves_old_acl_on_failure(tmp_path: Path) -> None:
    """On reload failure, the previous ACLManager instance is kept intact."""
    sentinelle, config_file = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    old_acl = sentinelle._acl
    config_file.write_text(_SENTINELLE_YAML_BROKEN, encoding="utf-8")

    await sentinelle.reload_config()

    assert sentinelle._acl is old_acl, "ACLManager must not be replaced on failure"


# ---------------------------------------------------------------------------
# _config_reload_listener — pub/sub
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_listener_calls_reload_on_reload_message(tmp_path: Path) -> None:
    """_config_reload_listener calls reload_config() when it receives 'reload'."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    sentinelle.reload_config = fake_reload

    messages = [
        {"type": "message", "data": b"reload"},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await sentinelle._config_reload_listener(mock_redis)

    assert reload_called == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_listener_ignores_non_reload_messages(tmp_path: Path) -> None:
    """_config_reload_listener does NOT call reload_config for irrelevant payloads."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    sentinelle.reload_config = fake_reload

    messages = [
        {"type": "message", "data": b"RELOAD"},
        {"type": "subscribe", "data": 1},
        {"type": "message", "data": b"restart"},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await sentinelle._config_reload_listener(mock_redis)

    assert reload_called == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_listener_subscribes_to_correct_channel(tmp_path: Path) -> None:
    """_config_reload_listener subscribes to relais:config:reload:sentinelle."""
    sentinelle, _ = _make_sentinelle_with_yaml(_SENTINELLE_YAML_V1, tmp_path)

    subscribe_calls: list[str] = []

    async def fake_listen():
        return
        yield

    mock_pubsub = AsyncMock()

    async def capture_subscribe(channel: str) -> None:
        subscribe_calls.append(channel)

    mock_pubsub.subscribe = capture_subscribe
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await sentinelle._config_reload_listener(mock_redis)

    assert "relais:config:reload:sentinelle" in subscribe_calls
