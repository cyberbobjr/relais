"""Tests for AiguilleurManager — adapter lifecycle, discovery, restart, SIGTERM.

RED phase: written before implementation.
"""
from unittest.mock import MagicMock, patch, call

import pytest

from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.base import BaseAiguilleur
from aiguilleur.core.manager import AiguilleurManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_adapter(
    name: str = "discord",
    alive: bool = True,
    restart_count: int = 0,
    max_restarts: int = 5,
) -> MagicMock:
    """Return a MagicMock that satisfies the BaseAiguilleur contract."""
    adapter = MagicMock(spec=BaseAiguilleur)
    adapter.is_alive.return_value = alive
    adapter.config = ChannelConfig(name=name, max_restarts=max_restarts)
    adapter._restart_count = restart_count
    return adapter


# ---------------------------------------------------------------------------
# Adapter loading / discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_manager_skips_disabled_channels():
    """Disabled channels must not have their adapter loaded or started."""
    configs = {
        "discord": ChannelConfig(name="discord", enabled=True),
        "telegram": ChannelConfig(name="telegram", enabled=False),
    }
    mock_discord = _make_mock_adapter("discord")

    with patch("aiguilleur.core.manager.load_channels_config", return_value=configs), \
         patch.object(AiguilleurManager, "_load_adapter", return_value=mock_discord) as mock_load, \
         patch.object(AiguilleurManager, "_supervise"):
        manager = AiguilleurManager()
        manager.run()

    assert mock_load.call_count == 1
    assert mock_load.call_args[0][0] == "discord"


@pytest.mark.unit
def test_manager_loads_adapter_by_convention(tmp_path):
    """_load_adapter discovers the class via aiguilleur.channels.{name}.adapter convention."""
    import types
    from aiguilleur.core.native import NativeAiguilleur

    _MODULE_NAME = "aiguilleur.channels.discord.adapter"

    class FakeDiscordAiguilleur(NativeAiguilleur):
        async def run(self) -> None:
            pass

    # __module__ must match the fake module's __name__ for the discovery filter to pass.
    FakeDiscordAiguilleur.__module__ = _MODULE_NAME

    cfg = ChannelConfig(name="discord", enabled=True)

    # Use a real ModuleType so __name__ is a plain string (MagicMock raises AttributeError
    # on dunder attribute access, breaking the getattr(attr, "__module__") == module.__name__
    # check in _load_adapter).
    fake_module = types.ModuleType(_MODULE_NAME)
    fake_module.FakeDiscordAiguilleur = FakeDiscordAiguilleur  # type: ignore[attr-defined]

    with patch("importlib.import_module", return_value=fake_module) as mock_import:
        manager = AiguilleurManager()
        adapter = manager._load_adapter("discord", cfg)

    mock_import.assert_called_with(_MODULE_NAME)
    assert isinstance(adapter, FakeDiscordAiguilleur)


@pytest.mark.unit
def test_manager_loads_adapter_via_class_path_override():
    """When class_path is set in ChannelConfig, _load_adapter uses it instead of convention."""
    from aiguilleur.core.native import NativeAiguilleur

    class CustomAiguilleur(NativeAiguilleur):
        async def run(self) -> None:
            pass

    cfg = ChannelConfig(
        name="custom",
        enabled=True,
        class_path="mycompany.adapters.custom.CustomAiguilleur",
    )

    with patch("importlib.import_module") as mock_import:
        mock_module = MagicMock()
        mock_module.CustomAiguilleur = CustomAiguilleur
        mock_import.return_value = mock_module

        manager = AiguilleurManager()
        adapter = manager._load_adapter("custom", cfg)

    mock_import.assert_called_with("mycompany.adapters.custom")
    assert isinstance(adapter, CustomAiguilleur)


# ---------------------------------------------------------------------------
# Restart / backoff
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_manager_restarts_crashed_adapter():
    """_check_and_restart calls restart() when an adapter is not alive and under max_restarts."""
    mock_adapter = _make_mock_adapter("discord", alive=False, restart_count=0, max_restarts=5)

    manager = AiguilleurManager()
    manager._adapters = {"discord": mock_adapter}
    manager._running = True

    manager._check_and_restart()

    mock_adapter.restart.assert_called_once()


@pytest.mark.unit
def test_manager_restart_uses_exponential_backoff():
    """Backoff doubles with each restart attempt: 1s, 2s, 4s, …, capped at 30s."""
    mock_adapter = _make_mock_adapter("discord", alive=False, restart_count=3, max_restarts=5)

    manager = AiguilleurManager()
    manager._adapters = {"discord": mock_adapter}
    manager._running = True

    manager._check_and_restart()

    # restart_count=3 → backoff = min(2^3, 30) = 8
    mock_adapter.restart.assert_called_once_with(backoff=8.0)


@pytest.mark.unit
def test_manager_removes_adapter_after_max_restarts():
    """When restart_count >= max_restarts the adapter is removed (no more restart attempts)."""
    mock_adapter = _make_mock_adapter("discord", alive=False, restart_count=5, max_restarts=5)

    manager = AiguilleurManager()
    manager._adapters = {"discord": mock_adapter}
    manager._running = True

    manager._check_and_restart()

    assert "discord" not in manager._adapters
    mock_adapter.restart.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_manager_stop_all_calls_stop_on_each_adapter():
    """_stop_all() calls stop(timeout=8.0) on every adapter and sets _running=False."""
    mock_discord = _make_mock_adapter("discord")
    mock_telegram = _make_mock_adapter("telegram")

    manager = AiguilleurManager()
    manager._adapters = {"discord": mock_discord, "telegram": mock_telegram}
    manager._running = True

    manager._stop_all()

    mock_discord.stop.assert_called_once_with(timeout=8.0)
    mock_telegram.stop.assert_called_once_with(timeout=8.0)
    assert manager._running is False
