"""Tests for Aiguilleur hot-reload of the 'profile' field in aiguilleur.yaml.

TDD RED phase — all tests are written before the implementation exists.

Tests cover:
- ProfileRef thread safety
- _reload_channel_profiles() updating ProfileRef values
- ProfileRef identity preserved across reload
- No adapter restart triggered on soft-field change
- Warning logged on hard-field change (type, class_path, enabled, command)
- Unknown channel names in new config are silently ignored
- Discord adapter reads profile dynamically (not from __init__ cache)
- Graceful degradation on bad YAML
- Graceful degradation when watchfiles is not installed
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from aiguilleur.channel_config import ChannelConfig, load_channels_config
from aiguilleur.core.manager import AiguilleurManager


# ---------------------------------------------------------------------------
# Phase 1 — ProfileRef tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_ref_initial_value():
    """ProfileRef.profile returns the value passed to __init__."""
    from aiguilleur.channel_config import ProfileRef

    ref = ProfileRef("fast")
    assert ref.profile == "fast"


@pytest.mark.unit
def test_profile_ref_initial_none():
    """ProfileRef.profile can be None."""
    from aiguilleur.channel_config import ProfileRef

    ref = ProfileRef(None)
    assert ref.profile is None


@pytest.mark.unit
def test_profile_ref_update():
    """ProfileRef.update() changes the stored profile."""
    from aiguilleur.channel_config import ProfileRef

    ref = ProfileRef("fast")
    ref.update("precise")
    assert ref.profile == "precise"


@pytest.mark.unit
def test_profile_ref_update_to_none():
    """ProfileRef.update(None) sets profile to None."""
    from aiguilleur.channel_config import ProfileRef

    ref = ProfileRef("fast")
    ref.update(None)
    assert ref.profile is None


@pytest.mark.unit
def test_profile_ref_update_thread_safe():
    """Two threads concurrently reading and writing ProfileRef raise no exceptions."""
    from aiguilleur.channel_config import ProfileRef

    ref = ProfileRef("fast")
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            while not stop.is_set():
                ref.update("fast")
                ref.update("precise")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                _ = ref.profile
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_w.start()
    t_r.start()

    time.sleep(0.1)
    stop.set()
    t_w.join(timeout=2)
    t_r.join(timeout=2)

    assert errors == [], f"Thread safety violation: {errors}"


@pytest.mark.unit
def test_channel_config_exposes_profile_ref():
    """ChannelConfig has a profile_ref attribute of type ProfileRef."""
    from aiguilleur.channel_config import ProfileRef

    cfg = ChannelConfig(name="discord", profile="fast")
    assert hasattr(cfg, "profile_ref")
    assert isinstance(cfg.profile_ref, ProfileRef)
    assert cfg.profile_ref.profile == "fast"


@pytest.mark.unit
def test_channel_config_profile_ref_none_when_profile_absent():
    """ChannelConfig.profile_ref.profile is None when profile is not set."""
    from aiguilleur.channel_config import ProfileRef

    cfg = ChannelConfig(name="discord")
    assert isinstance(cfg.profile_ref, ProfileRef)
    assert cfg.profile_ref.profile is None


@pytest.mark.unit
def test_load_channels_config_creates_profile_ref():
    """load_channels_config() constructs a ProfileRef for each channel."""
    import tempfile
    from pathlib import Path
    from aiguilleur.channel_config import ProfileRef

    yaml_content = """
channels:
  discord:
    enabled: true
    streaming: false
    profile: fast
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert isinstance(configs["discord"].profile_ref, ProfileRef)
    assert configs["discord"].profile_ref.profile == "fast"


# ---------------------------------------------------------------------------
# Phase 3 — _reload_channel_profiles() tests
# ---------------------------------------------------------------------------


def _make_mock_adapter_with_config(profile: str | None = "fast") -> MagicMock:
    """Return a MagicMock adapter whose config has a real ProfileRef.

    Args:
        profile: Initial profile value for the adapter's ChannelConfig.

    Returns:
        MagicMock with a real ChannelConfig (including ProfileRef) as .config.
    """
    from aiguilleur.channel_config import ProfileRef

    cfg = ChannelConfig(name="discord", profile=profile, type="native", enabled=True)
    adapter = MagicMock()
    adapter.config = cfg
    return adapter


@pytest.mark.unit
def test_reload_updates_profile_ref():
    """_reload_channel_profiles() updates the ProfileRef when profile changes."""
    adapter = _make_mock_adapter_with_config(profile="fast")
    original_ref = adapter.config.profile_ref

    new_configs = {
        "discord": ChannelConfig(name="discord", profile="precise", type="native", enabled=True)
    }

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    with patch("aiguilleur.core.manager.load_channels_config", return_value=new_configs):
        manager._reload_channel_profiles()

    assert original_ref.profile == "precise"


@pytest.mark.unit
def test_reload_preserves_profile_ref_identity():
    """The ProfileRef object identity is preserved across a reload."""
    adapter = _make_mock_adapter_with_config(profile="fast")
    original_ref_id = id(adapter.config.profile_ref)

    new_configs = {
        "discord": ChannelConfig(name="discord", profile="precise", type="native", enabled=True)
    }

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    with patch("aiguilleur.core.manager.load_channels_config", return_value=new_configs):
        manager._reload_channel_profiles()

    assert id(adapter.config.profile_ref) == original_ref_id


@pytest.mark.unit
def test_reload_no_adapter_restart():
    """_reload_channel_profiles() does not call start, stop, or restart on adapters."""
    adapter = _make_mock_adapter_with_config(profile="fast")
    adapter.start = MagicMock()
    adapter.stop = MagicMock()
    adapter.restart = MagicMock()

    new_configs = {
        "discord": ChannelConfig(name="discord", profile="precise", type="native", enabled=True)
    }

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    with patch("aiguilleur.core.manager.load_channels_config", return_value=new_configs):
        manager._reload_channel_profiles()

    adapter.start.assert_not_called()
    adapter.stop.assert_not_called()
    adapter.restart.assert_not_called()


@pytest.mark.unit
def test_reload_warns_on_hard_field_change(caplog):
    """_reload_channel_profiles() logs a warning when a hard field like 'type' changes."""
    import logging

    adapter = _make_mock_adapter_with_config(profile="fast")
    # Original config: type="native"
    new_configs = {
        "discord": ChannelConfig(name="discord", profile="fast", type="external", enabled=True)
    }

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    with patch("aiguilleur.core.manager.load_channels_config", return_value=new_configs):
        with caplog.at_level(logging.WARNING):
            manager._reload_channel_profiles()

    assert any("restart required" in record.message for record in caplog.records), (
        f"Expected 'restart required' warning, got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.unit
def test_reload_ignores_unknown_channels():
    """_reload_channel_profiles() ignores channels present in the new config but not in _adapters."""
    adapter = _make_mock_adapter_with_config(profile="fast")

    new_configs = {
        "discord": ChannelConfig(name="discord", profile="fast"),
        "telegram": ChannelConfig(name="telegram", profile="precise"),  # unknown
    }

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    # Should not raise KeyError
    with patch("aiguilleur.core.manager.load_channels_config", return_value=new_configs):
        manager._reload_channel_profiles()


@pytest.mark.unit
def test_reload_fails_gracefully_on_bad_yaml(caplog):
    """_reload_channel_profiles() logs an error and preserves state on YAML parse failure."""
    import logging
    import yaml

    adapter = _make_mock_adapter_with_config(profile="fast")
    original_profile = adapter.config.profile_ref.profile

    manager = AiguilleurManager()
    manager._adapters = {"discord": adapter}
    manager._reload_lock = threading.Lock()

    with patch(
        "aiguilleur.core.manager.load_channels_config",
        side_effect=yaml.YAMLError("bad yaml"),
    ):
        with caplog.at_level(logging.ERROR):
            manager._reload_channel_profiles()

    # profile_ref must be unchanged
    assert adapter.config.profile_ref.profile == original_profile
    assert any("reload failed" in record.message for record in caplog.records), (
        f"Expected 'reload failed' error log, got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Phase 4 — File watcher tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watcher_degrades_without_watchfiles(caplog):
    """_start_config_watcher() logs a warning and does not raise when watchfiles is absent."""
    import logging

    manager = AiguilleurManager()
    manager._shutdown_event = threading.Event()
    manager._reload_lock = threading.Lock()
    manager._adapters = {}

    with patch.dict(sys.modules, {"watchfiles": None}):
        with caplog.at_level(logging.WARNING):
            manager._start_config_watcher()

    assert any(
        "watchfiles" in record.message.lower() or "hot-reload disabled" in record.message.lower()
        for record in caplog.records
    ), f"Expected watchfiles warning, got: {[r.message for r in caplog.records]}"


@pytest.mark.unit
def test_watcher_starts_daemon_thread_when_watchfiles_available(tmp_path):
    """_start_config_watcher() starts a daemon thread when watchfiles is importable."""
    import types

    channels_yaml = tmp_path / "aiguilleur.yaml"
    channels_yaml.write_text("channels: {}\n")

    # Barrier: watch() blocks until the test releases it, letting us verify the thread exists
    watch_started = threading.Event()
    allow_exit = threading.Event()

    def fake_watch(path: str, stop_event: threading.Event):
        """Signal that the thread has started, then yield nothing and exit."""
        watch_started.set()
        allow_exit.wait(timeout=2)
        return iter([])

    watchfiles_stub = types.ModuleType("watchfiles")
    watchfiles_stub.watch = fake_watch  # type: ignore[attr-defined]

    manager = AiguilleurManager()
    manager._shutdown_event = threading.Event()
    manager._reload_lock = threading.Lock()
    manager._adapters = {}

    with patch.dict(sys.modules, {"watchfiles": watchfiles_stub}):
        with patch("aiguilleur.core.manager.resolve_config_path", return_value=channels_yaml):
            manager._start_config_watcher()

    # Wait until the thread has actually entered fake_watch
    started = watch_started.wait(timeout=2)
    assert started, "Watcher thread did not start within 2 seconds"

    # Inspect threads while watcher is still running (blocked in fake_watch)
    watcher_threads = [
        t for t in threading.enumerate() if "config-watcher" in t.name
    ]
    assert len(watcher_threads) >= 1, f"No config-watcher thread found among: {[t.name for t in threading.enumerate()]}"
    assert watcher_threads[0].daemon is True

    # Release the watcher so it can exit cleanly
    allow_exit.set()


# ---------------------------------------------------------------------------
# Phase 2 — Discord adapter dynamic profile reads
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discord_reads_profile_dynamically():
    """_RelaisDiscordClient stamps the current profile_ref.profile at message time, not init time."""
    import asyncio
    from unittest.mock import AsyncMock, PropertyMock

    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient
    from aiguilleur.channel_config import ProfileRef
    from common.contexts import CTX_AIGUILLEUR

    # Build a config whose profile_ref we can update later
    cfg = ChannelConfig(name="discord", profile="fast", streaming=False)
    assert cfg.profile_ref.profile == "fast"

    # Create client bypassing __init__ (avoid Discord intents + Redis setup)
    client = _RelaisDiscordClient.__new__(_RelaisDiscordClient)
    client._adapter = None  # use _channel_config fallback path
    client._channel_config = cfg
    client._redis_conn = AsyncMock()
    client._redis_conn.xadd = AsyncMock()
    client._typing_tasks = {}
    client._stop_event = None
    client.stream_in = "relais:messages:incoming"
    client.stream_out = "relais:messages:outgoing:discord"
    client.group_name = "discord_relay_group"
    client.consumer_name = "discord_test"

    # Stub discord.Client internals
    mock_loop = MagicMock()
    mock_loop.create_task.side_effect = lambda coro: (coro.close(), MagicMock())[1]
    client.loop = mock_loop

    # Stub user/message
    mock_user = MagicMock()
    mock_user.id = 999
    mock_user.name = "TestBot"
    type(client).user = PropertyMock(return_value=mock_user)

    import discord as discord_lib
    mock_message = MagicMock(spec=discord_lib.Message)
    mock_message.author.id = 111
    mock_message.author.name = "Alice"
    mock_message.content = f"<@999> Hello"
    mock_message.channel = MagicMock(spec=discord_lib.DMChannel)
    mock_message.channel.id = 777
    mock_message.mentions = [mock_user]

    captured_profiles: list[str | None] = []

    async def fake_xadd(stream: str, data: dict) -> None:
        import json
        payload = json.loads(data["payload"])
        captured_profiles.append(
            payload["context"].get(CTX_AIGUILLEUR, {}).get("channel_profile")
        )

    client._redis_conn.xadd.side_effect = fake_xadd

    loop = asyncio.new_event_loop()
    try:
        # First on_message → should stamp "fast"
        loop.run_until_complete(client.on_message(mock_message))

        # Now update the profile_ref
        cfg.profile_ref.update("precise")

        # Second on_message → should stamp "precise"
        loop.run_until_complete(client.on_message(mock_message))
    finally:
        loop.close()

    assert len(captured_profiles) == 2, f"Expected 2 captures, got {len(captured_profiles)}"
    assert captured_profiles[0] == "fast", f"First stamp should be 'fast', got {captured_profiles[0]}"
    assert captured_profiles[1] == "precise", f"Second stamp should be 'precise', got {captured_profiles[1]}"


@pytest.mark.unit
def test_discord_reads_prompt_path_dynamically():
    """_RelaisDiscordClient stamps prompt_path/streaming from live adapter.config after reload."""
    import asyncio
    from unittest.mock import AsyncMock, PropertyMock, MagicMock as MM

    from aiguilleur.channels.discord.adapter import _RelaisDiscordClient
    from common.contexts import CTX_AIGUILLEUR

    # Initial config — no prompt_path
    cfg_v1 = ChannelConfig(name="discord", profile="fast", streaming=False, prompt_path=None)

    # Fake adapter holding a swappable .config attribute
    fake_adapter = MM()
    fake_adapter.config = cfg_v1

    client = _RelaisDiscordClient.__new__(_RelaisDiscordClient)
    client._adapter = fake_adapter
    client._channel_config = None  # adapter path should be used, not this
    client._redis_conn = AsyncMock()
    client._redis_conn.xadd = AsyncMock()
    client._typing_tasks = {}
    client._stop_event = None
    client.stream_in = "relais:messages:incoming"
    client.stream_out = "relais:messages:outgoing:discord"
    client.group_name = "discord_relay_group"
    client.consumer_name = "discord_test"

    mock_loop = MM()
    mock_loop.create_task.side_effect = lambda coro: (coro.close(), MM())[1]
    client.loop = mock_loop

    mock_user = MM()
    mock_user.id = 999
    mock_user.name = "TestBot"
    type(client).user = PropertyMock(return_value=mock_user)

    import discord as discord_lib
    mock_message = MM(spec=discord_lib.Message)
    mock_message.author.id = 111
    mock_message.author.name = "Alice"
    mock_message.content = "<@999> Hello"
    mock_message.channel = MM(spec=discord_lib.DMChannel)
    mock_message.channel.id = 777
    mock_message.mentions = [mock_user]

    captured: list[dict] = []

    async def fake_xadd(stream: str, data: dict) -> None:
        import json
        payload = json.loads(data["payload"])
        captured.append(payload["context"].get(CTX_AIGUILLEUR, {}))

    client._redis_conn.xadd.side_effect = fake_xadd

    loop = asyncio.new_event_loop()
    try:
        # First message — no prompt_path, streaming=False
        loop.run_until_complete(client.on_message(mock_message))

        # Simulate hot-reload: manager replaced adapter.config with new frozen cfg
        cfg_v2 = ChannelConfig(
            name="discord",
            profile="fast",
            streaming=True,
            prompt_path="channels/discord_default.md",
            profile_ref=cfg_v1.profile_ref,
        )
        fake_adapter.config = cfg_v2

        # Second message — should pick up new prompt_path and streaming
        loop.run_until_complete(client.on_message(mock_message))
    finally:
        loop.close()

    assert len(captured) == 2
    assert captured[0]["channel_prompt_path"] is None
    assert captured[0]["streaming"] is False
    assert captured[1]["channel_prompt_path"] == "channels/discord_default.md"
    assert captured[1]["streaming"] is True
