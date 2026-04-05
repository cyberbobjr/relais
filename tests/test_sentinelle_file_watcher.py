"""Tests for sentinelle.main.Sentinelle — file watcher methods.

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


def _make_sentinelle_minimal():
    """Build a minimal Sentinelle instance with all heavy deps mocked.

    Returns:
        A partially-initialised Sentinelle instance suitable for file watcher tests.
    """
    from sentinelle.main import Sentinelle

    fake_acl = MagicMock()
    fake_acl._config_path = Path("/fake/sentinelle.yaml")

    with (
        patch("sentinelle.main.ACLManager", return_value=fake_acl),
        patch("sentinelle.main.RedisClient"),
    ):
        sentinelle = Sentinelle()

    return sentinelle


# ---------------------------------------------------------------------------
# _config_watch_paths()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sentinelle_has_config_watch_paths() -> None:
    """Sentinelle must expose a _config_watch_paths() method."""
    sentinelle = _make_sentinelle_minimal()
    assert hasattr(sentinelle, "_config_watch_paths"), (
        "Sentinelle must have a _config_watch_paths() method"
    )
    assert callable(sentinelle._config_watch_paths)


@pytest.mark.unit
def test_sentinelle_config_watch_paths_returns_list() -> None:
    """_config_watch_paths() returns a list."""
    sentinelle = _make_sentinelle_minimal()
    result = sentinelle._config_watch_paths()
    assert isinstance(result, list), "_config_watch_paths() must return a list"


@pytest.mark.unit
def test_sentinelle_watch_paths_contains_config_path() -> None:
    """_config_watch_paths() returns a list containing self._config_path."""
    sentinelle = _make_sentinelle_minimal()
    paths = sentinelle._config_watch_paths()
    assert sentinelle._config_path in paths, (
        "_config_watch_paths() must include self._config_path"
    )


# ---------------------------------------------------------------------------
# _start_file_watcher()
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_start_file_watcher_returns_task() -> None:
    """_start_file_watcher() returns an asyncio.Task."""
    sentinelle = _make_sentinelle_minimal()

    async def fake_watch_and_reload(paths, reload_fn, label):
        await asyncio.sleep(0)

    with (
        patch("sentinelle.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = sentinelle._start_file_watcher()

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
async def test_sentinelle_start_file_watcher_uses_correct_label() -> None:
    """_start_file_watcher() creates a task with label 'sentinelle'."""
    sentinelle = _make_sentinelle_minimal()

    captured_labels: list[str] = []

    async def fake_watch_and_reload(paths, reload_fn, label):
        captured_labels.append(label)

    with (
        patch("sentinelle.main.watch_and_reload", side_effect=fake_watch_and_reload),
        patch("common.config_reload.watchfiles", MagicMock()),
    ):
        task = sentinelle._start_file_watcher()
        await task

    assert captured_labels == ["sentinelle"], (
        "_start_file_watcher() must pass label='sentinelle' to watch_and_reload"
    )
