"""Shared pytest fixtures and helpers for the RELAIS test suite."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import yaml


@contextmanager
def isolated_search_path(
    config_root: Path,
    native_root: Path | None = None,
) -> Generator[None, None, None]:
    """Patch both module-level path constants so no real on-disk pack leaks in.

    Used by subagent registry tests to restrict the config cascade to a
    temporary directory, preventing real user or project subagent packs
    from interfering with test assertions.

    Args:
        config_root: Replacement for ``CONFIG_SEARCH_PATH`` (single-element list).
        native_root: Replacement for ``NATIVE_SUBAGENTS_PATH``.  Defaults to a
            non-existent sub-directory so the native tier contributes nothing.

    Yields:
        None — just enters the patched context.

    Example:
        >>> with isolated_search_path(tmp_path) as _:
        ...     registry = SubagentRegistry.load()
        ...     assert registry.all_names == set()
    """
    if native_root is None:
        native_root = config_root / "_nonexistent_native_subagents_"
    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [config_root]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", native_root),
    ):
        yield


def make_mock_tool(name: str) -> MagicMock:
    """Return a MagicMock that duck-types as a BaseTool.

    Args:
        name: Tool name attribute.

    Returns:
        MagicMock with .name and .run set.
    """
    from langchain_core.tools import BaseTool
    m = MagicMock(spec=BaseTool)
    m.name = name
    return m


def make_fake_tool_registry(tools: dict | None = None) -> MagicMock:
    """Return a mock ToolRegistry.

    Args:
        tools: Dict mapping name -> BaseTool mock.

    Returns:
        MagicMock behaving like ToolRegistry.
    """
    registry = MagicMock()
    registry.get = lambda name: (tools or {}).get(name)
    registry.all = lambda: dict(tools or {})
    return registry


def write_pack(base_dir: Path, name: str, extra: dict | None = None) -> Path:
    """Create a minimal subagent pack directory.

    Args:
        base_dir: Parent directory (``config/atelier/subagents/``).
        name: Subagent name and directory name.
        extra: Extra YAML fields to merge.

    Returns:
        Path to the created ``subagent.yaml``.
    """
    pack_dir = base_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are {name}.",
    }
    if extra:
        data.update(extra)
    yaml_path = pack_dir / "subagent.yaml"
    yaml_path.write_text(yaml.dump(data))
    return yaml_path
