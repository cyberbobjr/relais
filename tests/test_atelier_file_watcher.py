"""Tests for atelier.main.Atelier — file watcher methods.

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


def _make_atelier_minimal():
    """Build a minimal Atelier instance with all heavy deps mocked.

    Returns:
        A partially-initialised Atelier instance suitable for file watcher tests.
    """
    from atelier.main import Atelier

    fake_profiles = {"default": MagicMock(name="default_profile")}
    fake_mcp_servers = {}
    fake_progress = MagicMock(name="progress_config")

    with (
        patch("atelier.main.load_profiles", return_value=fake_profiles),
        patch("atelier.main.load_for_sdk", return_value=fake_mcp_servers),
        patch("atelier.main.load_display_config", return_value=fake_progress),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp/skills")),
        patch("atelier.main.SubagentRegistry") as mock_registry_cls,
        patch("atelier.main.ToolRegistry") as mock_tool_registry_cls,
        patch("atelier.main.AsyncSqliteSaver"),
        patch("atelier.main.resolve_storage_dir", return_value=Path("/tmp")),
        patch("atelier.main.RedisClient"),
    ):
        mock_registry_cls.load.return_value = MagicMock()
        mock_tool_registry_cls.discover.return_value = MagicMock()
        atelier = Atelier()

    return atelier


# ---------------------------------------------------------------------------
# _config_watch_paths()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atelier_has_config_watch_paths() -> None:
    """Atelier must expose a _config_watch_paths() method."""
    atelier = _make_atelier_minimal()
    assert hasattr(atelier, "_config_watch_paths"), (
        "Atelier must have a _config_watch_paths() method"
    )
    assert callable(atelier._config_watch_paths)


@pytest.mark.unit
def test_atelier_config_watch_paths_returns_list() -> None:
    """_config_watch_paths() returns a list."""
    atelier = _make_atelier_minimal()

    fake_path = Path("/fake/profiles.yaml")
    with patch("atelier.main.resolve_config_path", return_value=fake_path):
        result = atelier._config_watch_paths()

    assert isinstance(result, list), "_config_watch_paths() must return a list"


@pytest.mark.unit
def test_atelier_watch_paths_contains_four_paths() -> None:
    """_config_watch_paths() returns exactly 4 paths when no subagents dirs exist."""
    atelier = _make_atelier_minimal()

    fake_path = Path("/fake/config.yaml")
    # Use a nonexistent base so no config/atelier/subagents/ dir is found, giving exactly 4 paths
    with (
        patch("atelier.main.resolve_config_path", return_value=fake_path),
        patch("common.config_loader.CONFIG_SEARCH_PATH", [Path("/nonexistent-base-xyz")]),
    ):
        paths = atelier._config_watch_paths()

    assert len(paths) == 4, (
        "_config_watch_paths() must return 4 paths "
        f"(profiles.yaml, mcp_servers.yaml, atelier.yaml, native subagents dir), got {len(paths)}"
    )


@pytest.mark.unit
def test_atelier_watch_paths_uses_resolve_config_path() -> None:
    """_config_watch_paths() resolves each path via resolve_config_path()."""
    atelier = _make_atelier_minimal()

    resolved_names: list[str] = []
    fake_path = Path("/fake/config.yaml")

    def capture_resolve(name):
        resolved_names.append(name)
        return fake_path

    with patch("atelier.main.resolve_config_path", side_effect=capture_resolve):
        atelier._config_watch_paths()

    # Should have resolved 3 config files (profiles.yaml, mcp_servers.yaml, atelier.yaml)
    assert len(resolved_names) == 3, (
        f"Expected 3 resolve_config_path calls, got {len(resolved_names)}: {resolved_names}"
    )


# ---------------------------------------------------------------------------
# _start_file_watcher()
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_start_file_watcher_returns_task() -> None:
    """_start_file_watcher() returns an asyncio.Task."""
    atelier = _make_atelier_minimal()

    async def fake_watch_and_reload(paths, reload_fn, label):
        await asyncio.sleep(0)

    fake_path = Path("/fake/config.yaml")
    with (
        patch("atelier.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("atelier.main.resolve_config_path", return_value=fake_path),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = atelier._start_file_watcher()

    assert isinstance(task, asyncio.Task), (
        "_start_file_watcher() must return an asyncio.Task"
    )
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_start_file_watcher_uses_correct_label() -> None:
    """_start_file_watcher() creates a task with label 'atelier'."""
    atelier = _make_atelier_minimal()

    captured_labels: list[str] = []

    async def fake_watch_and_reload(paths, reload_fn, label):
        captured_labels.append(label)

    fake_path = Path("/fake/config.yaml")
    with (
        patch("atelier.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("atelier.main.resolve_config_path", return_value=fake_path),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = atelier._start_file_watcher()
        await task

    assert captured_labels == ["atelier"], (
        "_start_file_watcher() must pass label='atelier' to watch_and_reload"
    )
