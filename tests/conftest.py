"""Shared pytest fixtures and helpers for the RELAIS test suite."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import patch


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
