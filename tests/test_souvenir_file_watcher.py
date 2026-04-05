"""Tests for souvenir.main.Souvenir — file watcher methods.

TDD — tests are written before the implementation.  All tests are unit tests
that mock heavy dependencies and test _config_watch_paths() and _start_file_watcher().

Souvenir has no config files to watch currently, so _config_watch_paths() returns []
and _start_file_watcher() returns None.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_souvenir_minimal():
    """Build a minimal Souvenir instance with all heavy deps mocked.

    Returns:
        A partially-initialised Souvenir instance suitable for file watcher tests.
    """
    from souvenir.main import Souvenir

    with (
        patch("common.brick_base.RedisClient"),
        patch("souvenir.main.LongTermStore"),
        patch("souvenir.main.FileStore"),
        patch("souvenir.main.build_registry", return_value={}),
    ):
        souvenir = Souvenir()

    return souvenir


# ---------------------------------------------------------------------------
# _config_watch_paths()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_has_config_watch_paths() -> None:
    """Souvenir must expose a _config_watch_paths() method."""
    souvenir = _make_souvenir_minimal()
    assert hasattr(souvenir, "_config_watch_paths"), (
        "Souvenir must have a _config_watch_paths() method"
    )
    assert callable(souvenir._config_watch_paths)


@pytest.mark.unit
def test_souvenir_config_watch_paths_returns_empty_list() -> None:
    """_config_watch_paths() returns an empty list (no config files for Souvenir yet)."""
    souvenir = _make_souvenir_minimal()
    result = souvenir._config_watch_paths()
    assert result == [], (
        "_config_watch_paths() must return [] for Souvenir (no config files to watch)"
    )


# ---------------------------------------------------------------------------
# _start_file_watcher()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_has_start_file_watcher() -> None:
    """Souvenir must expose a _start_file_watcher() method."""
    souvenir = _make_souvenir_minimal()
    assert hasattr(souvenir, "_start_file_watcher"), (
        "Souvenir must have a _start_file_watcher() method"
    )
    assert callable(souvenir._start_file_watcher)


@pytest.mark.unit
def test_souvenir_start_file_watcher_returns_none() -> None:
    """_start_file_watcher() returns None when _config_watch_paths() is empty."""
    import asyncio

    souvenir = _make_souvenir_minimal()
    shutdown_event = asyncio.Event()
    result = souvenir._start_file_watcher(shutdown_event)
    assert result is None, (
        "_start_file_watcher() must return None when there are no paths to watch"
    )
