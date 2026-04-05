"""Tests for portail.main.Portail — file watcher methods.

TDD — tests are written before the implementation.  All tests are unit tests
that mock heavy dependencies and test _config_watch_paths() and _start_file_watcher().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_portail_minimal():
    """Build a minimal Portail instance with all heavy deps mocked.

    Returns:
        A partially-initialised Portail instance suitable for file watcher tests.
    """
    from portail.main import Portail

    fake_registry = MagicMock()
    fake_registry._config_path = Path("/fake/portail.yaml")
    fake_registry.guest_role = "guest"
    fake_registry.unknown_user_policy = "deny"

    with (
        patch("portail.main.UserRegistry", return_value=fake_registry),
        patch("portail.main.RedisClient"),
    ):
        portail = Portail()

    return portail


# ---------------------------------------------------------------------------
# _config_watch_paths()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_portail_has_config_watch_paths() -> None:
    """Portail must expose a _config_watch_paths() method."""
    portail = _make_portail_minimal()
    assert hasattr(portail, "_config_watch_paths"), (
        "Portail must have a _config_watch_paths() method"
    )
    assert callable(portail._config_watch_paths)


@pytest.mark.unit
def test_portail_config_watch_paths_returns_list() -> None:
    """_config_watch_paths() returns a list."""
    portail = _make_portail_minimal()
    result = portail._config_watch_paths()
    assert isinstance(result, list), "_config_watch_paths() must return a list"


@pytest.mark.unit
def test_portail_watch_paths_contains_config_path() -> None:
    """_config_watch_paths() returns a list containing self._config_path."""
    portail = _make_portail_minimal()
    paths = portail._config_watch_paths()
    assert portail._config_path in paths, (
        "_config_watch_paths() must include self._config_path"
    )


# ---------------------------------------------------------------------------
# _start_file_watcher()
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_start_file_watcher_returns_task() -> None:
    """_start_file_watcher() returns an asyncio.Task."""
    portail = _make_portail_minimal()

    # watch_and_reload runs indefinitely — we need to mock it
    async def fake_watch_and_reload(paths, reload_fn, label):
        await asyncio.sleep(0)  # yield control once then return

    with (
        patch("portail.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = portail._start_file_watcher()

    assert isinstance(task, asyncio.Task), (
        "_start_file_watcher() must return an asyncio.Task"
    )
    # Cancel to avoid dangling task warnings
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_start_file_watcher_uses_correct_label() -> None:
    """_start_file_watcher() creates a task with label 'portail'."""
    portail = _make_portail_minimal()

    captured_labels: list[str] = []

    async def fake_watch_and_reload(paths, reload_fn, label):
        captured_labels.append(label)

    with (
        patch("portail.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = portail._start_file_watcher()
        await task  # complete the task

    assert captured_labels == ["portail"], (
        "_start_file_watcher() must pass label='portail' to watch_and_reload"
    )
