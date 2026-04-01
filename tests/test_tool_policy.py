"""Unit tests for atelier.tool_policy.ToolPolicy.

Follows strict TDD — these tests were written before the implementation.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(base_dir: Path):
    """Instantiate ToolPolicy with the given base directory.

    Args:
        base_dir: The skills root directory.

    Returns:
        A ToolPolicy instance.
    """
    from atelier.tool_policy import ToolPolicy
    return ToolPolicy(base_dir=base_dir)


def _make_tool(name: str) -> MagicMock:
    """Return a MagicMock that mimics a LangChain BaseTool with the given name.

    Args:
        name: Tool name attribute.

    Returns:
        MagicMock instance with .name set.
    """
    tool = MagicMock()
    tool.name = name
    return tool


# ---------------------------------------------------------------------------
# resolve_skills — empty / None / garbage input
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_skills_none_returns_empty(tmp_path: Path) -> None:
    """resolve_skills(None) returns an empty list."""
    policy = _make_policy(tmp_path)
    assert policy.resolve_skills(None) == []


@pytest.mark.unit
def test_resolve_skills_garbage_returns_empty(tmp_path: Path) -> None:
    """resolve_skills with an integer returns an empty list."""
    policy = _make_policy(tmp_path)
    assert policy.resolve_skills(42) == []


@pytest.mark.unit
def test_resolve_skills_empty_list_returns_empty(tmp_path: Path) -> None:
    """resolve_skills([]) returns an empty list."""
    policy = _make_policy(tmp_path)
    assert policy.resolve_skills([]) == []


# ---------------------------------------------------------------------------
# resolve_skills — wildcard "*"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_skills_wildcard_returns_all_subdirs(tmp_path: Path) -> None:
    """resolve_skills(["*"]) expands to all immediate subdirectories."""
    (tmp_path / "coding").mkdir()
    (tmp_path / "writing").mkdir()
    (tmp_path / "file.txt").touch()  # files must not appear

    policy = _make_policy(tmp_path)
    result = policy.resolve_skills(["*"])

    assert sorted(result) == sorted([
        str(tmp_path / "coding"),
        str(tmp_path / "writing"),
    ])


@pytest.mark.unit
def test_resolve_skills_wildcard_empty_base_returns_empty(tmp_path: Path) -> None:
    """resolve_skills(["*"]) with no subdirectories returns []."""
    policy = _make_policy(tmp_path)
    assert policy.resolve_skills(["*"]) == []


# ---------------------------------------------------------------------------
# resolve_skills — named directory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_skills_valid_name_returns_absolute_path(tmp_path: Path) -> None:
    """resolve_skills with an existing name returns its absolute path."""
    (tmp_path / "coding").mkdir()

    policy = _make_policy(tmp_path)
    result = policy.resolve_skills(["coding"])

    assert result == [str(tmp_path / "coding")]


@pytest.mark.unit
def test_resolve_skills_nonexistent_name_returns_empty(tmp_path: Path) -> None:
    """resolve_skills with a non-existent directory name returns []."""
    policy = _make_policy(tmp_path)
    assert policy.resolve_skills(["ghost"]) == []


# ---------------------------------------------------------------------------
# resolve_skills — path traversal guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_skills_path_traversal_filtered(tmp_path: Path) -> None:
    """resolve_skills filters out path-traversal entries like '../etc'."""
    policy = _make_policy(tmp_path)
    result = policy.resolve_skills(["../etc"])
    assert result == []


@pytest.mark.unit
def test_resolve_skills_absolute_path_filtered(tmp_path: Path) -> None:
    """resolve_skills filters out absolute paths (must be relative names)."""
    policy = _make_policy(tmp_path)
    result = policy.resolve_skills(["/etc/passwd"])
    assert result == []


# ---------------------------------------------------------------------------
# parse_mcp_patterns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_mcp_patterns_none_returns_empty_tuple(tmp_path: Path) -> None:
    """parse_mcp_patterns(None) returns ()."""
    policy = _make_policy(tmp_path)
    assert policy.parse_mcp_patterns(None) == ()


@pytest.mark.unit
def test_parse_mcp_patterns_list_returns_tuple(tmp_path: Path) -> None:
    """parse_mcp_patterns with a list returns a tuple of strings."""
    policy = _make_policy(tmp_path)
    result = policy.parse_mcp_patterns(["fs_*", "web_search"])
    assert result == ("fs_*", "web_search")


@pytest.mark.unit
def test_parse_mcp_patterns_tuple_passthrough(tmp_path: Path) -> None:
    """parse_mcp_patterns with a tuple returns an equivalent tuple."""
    policy = _make_policy(tmp_path)
    result = policy.parse_mcp_patterns(("read_*",))
    assert result == ("read_*",)


@pytest.mark.unit
def test_parse_mcp_patterns_garbage_returns_empty_tuple(tmp_path: Path) -> None:
    """parse_mcp_patterns with a non-iterable returns ()."""
    policy = _make_policy(tmp_path)
    assert policy.parse_mcp_patterns(99) == ()


# ---------------------------------------------------------------------------
# filter_mcp_tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_mcp_tools_empty_patterns_returns_empty(tmp_path: Path) -> None:
    """filter_mcp_tools with None metadata value returns [] (fail-closed)."""
    tools = [_make_tool("fs_read"), _make_tool("web_search")]
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools(tools, None)
    assert result == []


@pytest.mark.unit
def test_filter_mcp_tools_wildcard_returns_all(tmp_path: Path) -> None:
    """filter_mcp_tools with ["*"] returns all tools."""
    tools = [_make_tool("fs_read"), _make_tool("web_search"), _make_tool("git_log")]
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools(tools, ["*"])
    assert result == tools


@pytest.mark.unit
def test_filter_mcp_tools_prefix_pattern(tmp_path: Path) -> None:
    """filter_mcp_tools with 'fs_*' returns only fs-prefixed tools."""
    tools = [_make_tool("fs_read"), _make_tool("fs_write"), _make_tool("web_search")]
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools(tools, ["fs_*"])
    assert [t.name for t in result] == ["fs_read", "fs_write"]


@pytest.mark.unit
def test_filter_mcp_tools_multiple_patterns(tmp_path: Path) -> None:
    """filter_mcp_tools with multiple patterns uses OR semantics."""
    tools = [_make_tool("fs_read"), _make_tool("web_search"), _make_tool("git_log")]
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools(tools, ["fs_*", "git_*"])
    assert [t.name for t in result] == ["fs_read", "git_log"]


@pytest.mark.unit
def test_filter_mcp_tools_no_match_returns_empty(tmp_path: Path) -> None:
    """filter_mcp_tools with a non-matching pattern returns []."""
    tools = [_make_tool("fs_read"), _make_tool("web_search")]
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools(tools, ["db_*"])
    assert result == []


@pytest.mark.unit
def test_filter_mcp_tools_empty_tool_list(tmp_path: Path) -> None:
    """filter_mcp_tools with an empty tool list returns []."""
    policy = _make_policy(tmp_path)
    result = policy.filter_mcp_tools([], ["*"])
    assert result == []
