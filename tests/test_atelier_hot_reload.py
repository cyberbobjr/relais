"""Tests for atelier.main.Atelier — hot-reload via _load(), reload_config(),
and the Redis Pub/Sub listener _config_reload_listener().

TDD — tests are written before the implementation.  All tests mock heavy
dependencies (profile_loader, mcp_loader, display_config, channel_config).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from atelier.display_config import DisplayConfig
from atelier.soul_assembler import AssemblyResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_atelier_minimal():
    """Build a minimal Atelier instance with all heavy deps mocked.

    Returns:
        A partially-initialised Atelier instance suitable for hot-reload tests.
    """
    from atelier.main import Atelier

    # Fake profile objects
    fake_profiles = {"default": MagicMock(name="default_profile")}
    fake_mcp_servers = {}
    fake_display = MagicMock(name="display_config")
    fake_streaming_channels = frozenset(["telegram"])

    with (
        patch("atelier.main.load_profiles", return_value=fake_profiles),
        patch("atelier.main.load_for_sdk", return_value=fake_mcp_servers),
        patch("atelier.main.load_display_config", return_value=fake_display),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp/skills")),
        patch("atelier.main.SubagentRegistry") as mock_registry_cls,
        patch("atelier.main.AsyncSqliteSaver"),
        patch("atelier.main.resolve_storage_dir", return_value=Path("/tmp")),
        patch("atelier.main.RedisClient"),
    ):
        mock_registry_cls.discover.return_value = MagicMock()
        atelier = Atelier()

    # Install reload lock if not present (may have been set by __init__)
    if not hasattr(atelier, "_config_lock"):
        atelier._config_lock = asyncio.Lock()

    return atelier


# ---------------------------------------------------------------------------
# _load() — method exists and is callable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atelier_has_load_method() -> None:
    """Atelier must expose a _load() method."""
    atelier = _make_atelier_minimal()
    assert hasattr(atelier, "_load"), "Atelier must have a _load() method"
    assert callable(atelier._load)


@pytest.mark.unit
def test_atelier_load_reloads_profiles() -> None:
    """_load() replaces _profiles with freshly loaded data."""
    atelier = _make_atelier_minimal()

    new_profiles = {"fast": MagicMock(name="fast_profile")}
    with patch("atelier.main.load_profiles", return_value=new_profiles):
        atelier._load()

    assert atelier._profiles is new_profiles


@pytest.mark.unit
def test_atelier_load_reloads_mcp_servers() -> None:
    """_load() replaces _mcp_servers_default with freshly loaded data."""
    atelier = _make_atelier_minimal()

    new_servers = {"my_server": {"command": "uvx"}}
    with patch("atelier.main.load_for_sdk", return_value=new_servers):
        atelier._load()

    assert atelier._mcp_servers_default is new_servers


@pytest.mark.unit
def test_atelier_load_reloads_display_config() -> None:
    """_load() replaces _display_config with freshly loaded data."""
    atelier = _make_atelier_minimal()

    new_progress = MagicMock(name="new_progress")
    with patch("atelier.main.load_display_config", return_value=new_progress):
        atelier._load()

    assert atelier._display_config is new_progress


@pytest.mark.unit
def test_atelier_streaming_flag_read_from_envelope_context() -> None:
    """Atelier reads streaming capability from context["aiguilleur"]["streaming"], not from aiguilleur.yaml."""
    from common.contexts import CTX_AIGUILLEUR

    atelier = _make_atelier_minimal()

    # Envelope with streaming=True stamped by Aiguilleur
    env_streaming = MagicMock()
    env_streaming.context = {CTX_AIGUILLEUR: {"streaming": True}}
    aig_ctx = env_streaming.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx.get("streaming", False) is True

    # Envelope with streaming=False
    env_no_stream = MagicMock()
    env_no_stream.context = {CTX_AIGUILLEUR: {"streaming": False}}
    aig_ctx2 = env_no_stream.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx2.get("streaming", False) is False

    # Envelope without any aiguilleur context (older adapter) → safe default
    env_legacy = MagicMock()
    env_legacy.context = {}
    aig_ctx3 = env_legacy.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx3.get("streaming", False) is False

    # Atelier no longer has _streaming_capable_channels
    assert not hasattr(atelier, "_streaming_capable_channels")


# ---------------------------------------------------------------------------
# Streaming flag — envelope code path in _run_stream_loop
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_passes_stream_callback_when_envelope_streaming_true() -> None:
    """When context[aiguilleur][streaming]=True, AgentExecutor.execute receives a non-None stream_callback."""
    from pathlib import Path
    from common.envelope import Envelope
    from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL

    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_display_config", return_value=DisplayConfig()),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp/skills")),
        patch("atelier.main.SubagentRegistry") as mock_registry_cls,
        patch("atelier.main.AsyncSqliteSaver"),
        patch("atelier.main.resolve_storage_dir", return_value=Path("/tmp")),
        patch("atelier.main.RedisClient"),
    ):
        mock_registry_cls.discover.return_value = MagicMock()
        from atelier.main import Atelier
        atelier = Atelier()

    profile_mock = MagicMock()
    profile_mock.model = "anthropic:claude-sonnet-4-6"
    profile_mock.max_turns = 5
    atelier._profiles = {"default": profile_mock}

    from common.envelope_actions import ACTION_MESSAGE_INCOMING
    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-stream",
        correlation_id="corr-stream-001",
        action=ACTION_MESSAGE_INCOMING,
        context={
            CTX_AIGUILLEUR: {"streaming": True, "reply_to": "999"},
            CTX_PORTAIL: {
                "llm_profile": "default",
                "user_record": {
                    "role": "user",
                    "prompt_path": None,
                    "skills_dirs": [],
                    "allowed_mcp_tools": [],
                    "display_name": "Alice",
                    "blocked": False,
                    "actions": ["send"],
                },
            },
        },
    )

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:tasks", [("1234567890-0", {"payload": envelope.to_json()})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xack = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.publish = AsyncMock()

    execute_kwargs: list[dict] = []

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=profile_mock),
        patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()

        async def capture_execute(**kwargs):
            execute_kwargs.append(kwargs)
            return MagicMock(reply_text="reply")

        mock_instance.execute = capture_execute
        MockExec.return_value = mock_instance

        try:
            await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
        except asyncio.CancelledError:
            pass

    assert execute_kwargs, "AgentExecutor.execute should have been called"
    assert execute_kwargs[0].get("stream_callback") is not None, (
        "stream_callback must be non-None when envelope has streaming=True"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_passes_no_stream_callback_when_envelope_streaming_false() -> None:
    """When context[aiguilleur][streaming]=False, AgentExecutor.execute receives stream_callback=None."""
    from pathlib import Path
    from common.envelope import Envelope
    from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL

    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_display_config", return_value=DisplayConfig()),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp/skills")),
        patch("atelier.main.SubagentRegistry") as mock_registry_cls,
        patch("atelier.main.AsyncSqliteSaver"),
        patch("atelier.main.resolve_storage_dir", return_value=Path("/tmp")),
        patch("atelier.main.RedisClient"),
    ):
        mock_registry_cls.discover.return_value = MagicMock()
        from atelier.main import Atelier
        atelier = Atelier()

    profile_mock = MagicMock()
    profile_mock.model = "anthropic:claude-sonnet-4-6"
    profile_mock.max_turns = 5
    atelier._profiles = {"default": profile_mock}

    from common.envelope_actions import ACTION_MESSAGE_INCOMING
    envelope = Envelope(
        content="hello",
        sender_id="discord:111",
        channel="discord",
        session_id="sess-nostream",
        correlation_id="corr-nostream-001",
        action=ACTION_MESSAGE_INCOMING,
        context={
            CTX_AIGUILLEUR: {"streaming": False, "reply_to": "999"},
            CTX_PORTAIL: {
                "llm_profile": "default",
                "user_record": {
                    "role": "user",
                    "prompt_path": None,
                    "skills_dirs": [],
                    "allowed_mcp_tools": [],
                    "display_name": "Alice",
                    "blocked": False,
                    "actions": ["send"],
                },
            },
        },
    )

    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        [("relais:tasks", [("1234567890-0", {"payload": envelope.to_json()})])],
        asyncio.CancelledError(),
    ])
    redis_conn.xack = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.publish = AsyncMock()

    execute_kwargs: list[dict] = []

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=profile_mock),
        patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()

        async def capture_execute(**kwargs):
            execute_kwargs.append(kwargs)
            return MagicMock(reply_text="reply")

        mock_instance.execute = capture_execute
        MockExec.return_value = mock_instance

        try:
            await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
        except asyncio.CancelledError:
            pass

    assert execute_kwargs, "AgentExecutor.execute should have been called"
    assert execute_kwargs[0].get("stream_callback") is None, (
        "stream_callback must be None when envelope has streaming=False"
    )


# ---------------------------------------------------------------------------
# reload_config() — success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reload_config_returns_true_on_success() -> None:
    """reload_config() returns True when all loaders succeed."""
    atelier = _make_atelier_minimal()

    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_display_config", return_value=DisplayConfig()),
    ):
        result = await atelier.reload_config()

    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reload_config_applies_new_profiles() -> None:
    """reload_config() updates _profiles after successful reload."""
    atelier = _make_atelier_minimal()

    fresh_profiles = {"coder": MagicMock(name="coder_profile")}
    with (
        patch("atelier.main.load_profiles", return_value=fresh_profiles),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_display_config", return_value=DisplayConfig()),
    ):
        await atelier.reload_config()

    assert atelier._profiles is fresh_profiles


# ---------------------------------------------------------------------------
# reload_config() — failure path (loader raises)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reload_config_returns_false_when_loader_raises() -> None:
    """reload_config() returns False when any loader raises an exception."""
    atelier = _make_atelier_minimal()
    old_profiles = atelier._profiles

    with patch("atelier.main.load_profiles", side_effect=FileNotFoundError("profiles.yaml missing")):
        result = await atelier.reload_config()

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reload_config_preserves_profiles_on_failure() -> None:
    """On reload failure, _profiles is not replaced."""
    atelier = _make_atelier_minimal()
    old_profiles = atelier._profiles

    with patch("atelier.main.load_profiles", side_effect=RuntimeError("bad YAML")):
        await atelier.reload_config()

    assert atelier._profiles is old_profiles


# ---------------------------------------------------------------------------
# _config_reload_listener — pub/sub
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_listener_calls_reload_on_reload_message() -> None:
    """_config_reload_listener calls reload_config() when it receives 'reload'."""
    atelier = _make_atelier_minimal()

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    atelier.reload_config = fake_reload

    messages = [{"type": "message", "data": b"reload"}]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await atelier._config_reload_listener(mock_redis)

    assert reload_called == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_listener_ignores_non_reload_messages() -> None:
    """_config_reload_listener ignores messages that are not exactly 'reload'."""
    atelier = _make_atelier_minimal()

    reload_called: list[bool] = []

    async def fake_reload():
        reload_called.append(True)
        return True

    atelier.reload_config = fake_reload

    messages = [
        {"type": "message", "data": b"RELOAD"},
        {"type": "subscribe", "data": 1},
        {"type": "message", "data": b"stop"},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    await atelier._config_reload_listener(mock_redis)

    assert reload_called == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_listener_subscribes_to_correct_channel() -> None:
    """_config_reload_listener subscribes to relais:config:reload:atelier."""
    atelier = _make_atelier_minimal()

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

    await atelier._config_reload_listener(mock_redis)

    assert "relais:config:reload:atelier" in subscribe_calls
