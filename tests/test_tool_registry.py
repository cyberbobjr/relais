"""Unit tests for atelier.tools._registry.ToolRegistry.

Tests validate:
- ToolRegistry.discover() finds @tool-decorated functions in atelier/tools/*.py
- Underscore-prefixed modules are skipped
- get(name) returns the BaseTool or None
- all() returns the full dict
- discover() returns a frozen dataclass (immutable)
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_tool(name: str) -> MagicMock:
    """Return a mock object that passes isinstance(obj, BaseTool) checks.

    Args:
        name: The tool name attribute.

    Returns:
        A MagicMock with .name attribute set.
    """
    from langchain_core.tools import BaseTool
    mock = MagicMock(spec=BaseTool)
    mock.name = name
    return mock


def _make_fake_module(tool_names: list[str], include_non_tool: bool = False) -> types.ModuleType:
    """Build a fake Python module containing BaseTool instances.

    Args:
        tool_names: Names of tools to add to the module.
        include_non_tool: If True, add a non-BaseTool attribute too.

    Returns:
        A fake module with tool attributes set.
    """
    mod = types.ModuleType("fake_module")
    for name in tool_names:
        setattr(mod, name, _make_mock_tool(name))
    if include_non_tool:
        mod.NOT_A_TOOL = "just a string"  # type: ignore[attr-defined]
        mod.some_int = 42  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# ToolRegistry import
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_registry_is_importable() -> None:
    """atelier.tools._registry.ToolRegistry must be importable."""
    from atelier.tools._registry import ToolRegistry
    assert ToolRegistry is not None


@pytest.mark.unit
def test_tool_registry_is_frozen_dataclass() -> None:
    """ToolRegistry must be a frozen dataclass (immutable)."""
    import dataclasses
    from atelier.tools._registry import ToolRegistry
    assert dataclasses.is_dataclass(ToolRegistry)
    # Verify frozen — cannot assign a new attribute
    registry = ToolRegistry(_tools={})
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        registry._tools = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ToolRegistry.discover()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_registry_discover_returns_registry_instance() -> None:
    """ToolRegistry.discover() must return a ToolRegistry instance."""
    from atelier.tools._registry import ToolRegistry

    with patch("pkgutil.iter_modules", return_value=[]):
        registry = ToolRegistry.discover()

    assert isinstance(registry, ToolRegistry)


@pytest.mark.unit
def test_tool_registry_discover_skips_underscore_modules() -> None:
    """discover() must skip modules whose names start with '_'."""
    from atelier.tools._registry import ToolRegistry
    import pkgutil

    # Simulate iter_modules returning one underscore module
    finder = MagicMock()
    fake_infos = [
        pkgutil.ModuleInfo(finder, "_internal", False),
        pkgutil.ModuleInfo(finder, "_registry", False),
    ]

    with (
        patch("pkgutil.iter_modules", return_value=fake_infos),
        patch("importlib.import_module") as mock_import,
    ):
        registry = ToolRegistry.discover()

    mock_import.assert_not_called()
    assert registry.all() == {}


@pytest.mark.unit
def test_tool_registry_discover_finds_tools_in_module() -> None:
    """discover() collects BaseTool instances from non-underscore modules."""
    from atelier.tools._registry import ToolRegistry
    import pkgutil

    fake_tool = _make_mock_tool("my_tool")
    fake_mod = _make_fake_module([], include_non_tool=False)
    fake_mod.my_tool = fake_tool  # type: ignore[attr-defined]

    finder = MagicMock()
    fake_infos = [pkgutil.ModuleInfo(finder, "my_tools", False)]

    with (
        patch("pkgutil.iter_modules", return_value=fake_infos),
        patch("importlib.import_module", return_value=fake_mod),
    ):
        registry = ToolRegistry.discover()

    assert "my_tool" in registry.all()
    assert registry.get("my_tool") is fake_tool


@pytest.mark.unit
def test_tool_registry_discover_ignores_non_tool_attributes() -> None:
    """discover() must not include non-BaseTool module attributes."""
    from atelier.tools._registry import ToolRegistry
    import pkgutil

    fake_tool = _make_mock_tool("real_tool")
    fake_mod = types.ModuleType("fake")
    fake_mod.real_tool = fake_tool  # type: ignore[attr-defined]
    fake_mod.NOT_A_TOOL = "a string"  # type: ignore[attr-defined]
    fake_mod.some_int = 42  # type: ignore[attr-defined]

    finder = MagicMock()
    fake_infos = [pkgutil.ModuleInfo(finder, "tools_module", False)]

    with (
        patch("pkgutil.iter_modules", return_value=fake_infos),
        patch("importlib.import_module", return_value=fake_mod),
    ):
        registry = ToolRegistry.discover()

    assert set(registry.all().keys()) == {"real_tool"}


@pytest.mark.unit
def test_tool_registry_discover_multiple_modules() -> None:
    """discover() aggregates tools from multiple modules."""
    from atelier.tools._registry import ToolRegistry
    import pkgutil

    tool_a = _make_mock_tool("tool_a")
    tool_b = _make_mock_tool("tool_b")
    mod_a = types.ModuleType("mod_a")
    mod_a.tool_a = tool_a  # type: ignore[attr-defined]
    mod_b = types.ModuleType("mod_b")
    mod_b.tool_b = tool_b  # type: ignore[attr-defined]

    finder = MagicMock()
    fake_infos = [
        pkgutil.ModuleInfo(finder, "mod_a", False),
        pkgutil.ModuleInfo(finder, "mod_b", False),
    ]

    call_count = 0

    def fake_import(name: str):
        nonlocal call_count
        if "mod_a" in name:
            return mod_a
        return mod_b

    with (
        patch("pkgutil.iter_modules", return_value=fake_infos),
        patch("importlib.import_module", side_effect=fake_import),
    ):
        registry = ToolRegistry.discover()

    assert "tool_a" in registry.all()
    assert "tool_b" in registry.all()


@pytest.mark.unit
def test_tool_registry_discover_logs_warning_on_import_error(caplog) -> None:
    """discover() logs a warning and skips a module that fails to import."""
    import logging
    from atelier.tools._registry import ToolRegistry
    import pkgutil

    finder = MagicMock()
    fake_infos = [pkgutil.ModuleInfo(finder, "broken_module", False)]

    with (
        patch("pkgutil.iter_modules", return_value=fake_infos),
        patch("importlib.import_module", side_effect=ImportError("broken")),
        caplog.at_level(logging.WARNING),
    ):
        registry = ToolRegistry.discover()

    assert registry.all() == {}
    # Warning must have been logged
    assert any("broken_module" in r.message or "broken" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ToolRegistry.get()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_registry_get_returns_tool_when_present() -> None:
    """get(name) returns the BaseTool when the name is registered."""
    from atelier.tools._registry import ToolRegistry

    fake_tool = _make_mock_tool("fetch_data")
    registry = ToolRegistry(_tools={"fetch_data": fake_tool})

    result = registry.get("fetch_data")
    assert result is fake_tool


@pytest.mark.unit
def test_tool_registry_get_returns_none_when_absent() -> None:
    """get(name) returns None when the name is not registered."""
    from atelier.tools._registry import ToolRegistry

    registry = ToolRegistry(_tools={})
    assert registry.get("nonexistent") is None


# ---------------------------------------------------------------------------
# ToolRegistry.all()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_registry_all_returns_dict() -> None:
    """all() returns a dict mapping name → BaseTool."""
    from atelier.tools._registry import ToolRegistry

    t1 = _make_mock_tool("t1")
    t2 = _make_mock_tool("t2")
    registry = ToolRegistry(_tools={"t1": t1, "t2": t2})

    result = registry.all()
    assert isinstance(result, dict)
    assert result["t1"] is t1
    assert result["t2"] is t2


@pytest.mark.unit
def test_tool_registry_all_empty_when_no_tools() -> None:
    """all() returns an empty dict when no tools were discovered."""
    from atelier.tools._registry import ToolRegistry

    registry = ToolRegistry(_tools={})
    assert registry.all() == {}
