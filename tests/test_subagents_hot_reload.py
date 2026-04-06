"""Unit tests for SubagentRegistry hot-reload in Atelier.

Tests validate:
- Writing a new YAML file and calling reload_config() adds the new spec
- Malformed YAML on reload preserves the previous registry state
- _config_watch_paths() includes subagents directories that exist on disk
- _apply_config() swaps the subagent_registry reference atomically
- reload_config() returns True on success, False on failure
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_subagent_yaml(directory: Path, name: str, extra: dict | None = None) -> Path:
    """Write a minimal valid subagent pack to directory/name/subagent.yaml.

    Args:
        directory: Target directory (the subagents/ folder).
        name: Subagent name; creates a subdirectory with that name.
        extra: Extra YAML fields to merge.

    Returns:
        Path to the written subagent.yaml file.
    """
    pack_dir = directory / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are {name}.",
    }
    if extra:
        data.update(extra)
    path = pack_dir / "subagent.yaml"
    path.write_text(yaml.dump(data))
    return path


def _make_atelier_with_tmp_subagents(tmp_path: Path):
    """Build an Atelier instance whose config cascade points at tmp_path.

    This helper patches CONFIG_SEARCH_PATH to use tmp_path so that
    SubagentRegistry.load() and _config_watch_paths() operate on a
    controlled filesystem rather than the real project directory.

    Args:
        tmp_path: pytest tmp_path fixture value.

    Returns:
        A tuple (atelier, subagents_dir) where subagents_dir is the
        config/atelier/subagents/ directory inside tmp_path.
    """
    from atelier.main import Atelier

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    fake_profiles = {"default": MagicMock(name="default_profile")}
    fake_mcp_servers = {}
    fake_progress = MagicMock(name="progress_config")

    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = MagicMock()

    with (
        patch("atelier.main.load_profiles", return_value=fake_profiles),
        patch("atelier.main.load_for_sdk", return_value=fake_mcp_servers),
        patch("atelier.main.load_progress_config", return_value=fake_progress),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp/skills")),
        patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls),
        patch("atelier.main.resolve_storage_dir", return_value=tmp_path),
        patch("atelier.main.RedisClient"),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.main.resolve_config_path",
              return_value=tmp_path / "config" / "atelier" / "profiles.yaml"),
    ):
        atelier = Atelier()

    return atelier, subagents_dir


# ---------------------------------------------------------------------------
# _config_watch_paths includes subagents dirs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watch_paths_includes_existing_subagents_dir(tmp_path: Path) -> None:
    """_config_watch_paths() includes config/atelier/subagents/ if it exists on disk."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    fake_config = tmp_path / "config" / "profiles.yaml"

    with (
        patch("atelier.main.resolve_config_path", return_value=fake_config),
        patch("common.config_loader.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        paths = atelier._config_watch_paths()

    assert subagents_dir in paths, (
        f"Expected {subagents_dir} in watch paths, got: {paths}"
    )


@pytest.mark.unit
def test_watch_paths_excludes_nonexistent_subagents_dir(tmp_path: Path) -> None:
    """_config_watch_paths() excludes config/atelier/subagents/ when it doesn't exist."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    # Remove the subagents dir
    subagents_dir.rmdir()

    fake_config = tmp_path / "config" / "profiles.yaml"

    with (
        patch("atelier.main.resolve_config_path", return_value=fake_config),
        patch("common.config_loader.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        paths = atelier._config_watch_paths()

    assert subagents_dir not in paths


# ---------------------------------------------------------------------------
# _apply_config swaps subagent_registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_config_swaps_subagent_registry(tmp_path: Path) -> None:
    """_apply_config() replaces _subagent_registry when the key is present."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    new_registry = MagicMock(name="new_subagent_registry")
    cfg = {
        "profiles": {},
        "mcp_servers": {},
        "progress": MagicMock(),
        "streaming_channels": frozenset(),
        "subagent_registry": new_registry,
    }
    atelier._apply_config(cfg)

    assert atelier._subagent_registry is new_registry


@pytest.mark.unit
def test_apply_config_preserves_subagent_registry_when_key_absent(tmp_path: Path) -> None:
    """_apply_config() leaves _subagent_registry unchanged when key is absent."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    old_registry = atelier._subagent_registry
    cfg = {
        "profiles": {},
        "mcp_servers": {},
        "progress": MagicMock(),
        "streaming_channels": frozenset(),
        # no subagent_registry key
    }
    atelier._apply_config(cfg)

    assert atelier._subagent_registry is old_registry


# ---------------------------------------------------------------------------
# reload_config() — adds new spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reload_config_picks_up_new_subagent_yaml(tmp_path: Path) -> None:
    """After adding a YAML file and calling reload_config(), the spec is present."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    # Initially no subagents
    assert atelier._subagent_registry.all_names == frozenset()

    # Write a new subagent YAML
    _write_subagent_yaml(subagents_dir, "new-agent")

    # Trigger reload
    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_progress_config", return_value=MagicMock()),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        result = await atelier.reload_config()

    assert result is True
    assert "new-agent" in atelier._subagent_registry.all_names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reload_config_preserves_registry_when_profiles_fail(tmp_path: Path) -> None:
    """When a loader raises, the previous subagent_registry is preserved."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    # Write a subagent first and load it
    _write_subagent_yaml(subagents_dir, "original-agent")
    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_progress_config", return_value=MagicMock()),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        await atelier.reload_config()

    assert "original-agent" in atelier._subagent_registry.all_names
    previous_registry = atelier._subagent_registry

    # Now force a reload failure
    with patch("atelier.main.load_profiles", side_effect=RuntimeError("bad YAML")):
        result = await atelier.reload_config()

    assert result is False
    assert atelier._subagent_registry is previous_registry


# ---------------------------------------------------------------------------
# _build_config_candidate includes subagent_registry key
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_config_candidate_includes_subagent_registry(tmp_path: Path) -> None:
    """_build_config_candidate() dict includes 'subagent_registry' key."""
    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_progress_config", return_value=MagicMock()),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        candidate = atelier._build_config_candidate()

    assert "subagent_registry" in candidate


@pytest.mark.unit
def test_build_config_candidate_subagent_registry_is_subagent_registry_instance(tmp_path: Path) -> None:
    """The 'subagent_registry' in candidate is a SubagentRegistry instance."""
    from atelier.subagents import SubagentRegistry

    atelier, subagents_dir = _make_atelier_with_tmp_subagents(tmp_path)

    with (
        patch("atelier.main.load_profiles", return_value={}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_progress_config", return_value=MagicMock()),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
    ):
        candidate = atelier._build_config_candidate()

    assert isinstance(candidate["subagent_registry"], SubagentRegistry)
