"""Unit tests for atelier.subagents — tool token resolution.

Tests validate:
- mcp:<glob> token resolves against request_tools by fnmatch
- inherit token yields all request_tools
- bare <name> token resolves via tool_registry.get()
- local:<name> token resolves from pack's local_tools dict
- Mixed tokens work correctly together
- Unknown static tokens are logged as WARNING and dropped
- inherit never widens scope beyond request_tools
- Deduplication across tokens
- Empty tools list yields empty result
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_tool(name: str) -> MagicMock:
    """Return a MagicMock that passes isinstance(obj, BaseTool) checks.

    Args:
        name: Tool name attribute.

    Returns:
        MagicMock with .name set.
    """
    from langchain_core.tools import BaseTool
    m = MagicMock(spec=BaseTool)
    m.name = name
    return m


def _make_fake_tool_registry(tools: dict | None = None) -> MagicMock:
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


def _write_pack(base_dir: Path, name: str, extra: dict | None = None) -> Path:
    """Create a subagent pack directory with subagent.yaml.

    Args:
        base_dir: Parent directory for the pack (``config/atelier/subagents/``).
        name: Subagent name — both the directory name and the ``name`` field.
        extra: Extra YAML fields to merge into the spec.

    Returns:
        Path to the created ``<name>/subagent.yaml`` file.
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


# ---------------------------------------------------------------------------
# _resolve_tool_tokens — unit-level tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_inherit_yields_all_request_tools() -> None:
    """'inherit' token returns all request_tools unchanged."""
    from atelier.subagents import _resolve_tool_tokens

    tool_a = _make_mock_tool("tool_a")
    tool_b = _make_mock_tool("tool_b")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("inherit",),
        request_tools=[tool_a, tool_b],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert tool_a in result
    assert tool_b in result
    assert len(result) == 2


@pytest.mark.unit
def test_resolve_mcp_glob_matches_by_name() -> None:
    """'mcp:<glob>' token filters request_tools by fnmatch on .name."""
    from atelier.subagents import _resolve_tool_tokens

    fs_read = _make_mock_tool("filesystem_read")
    fs_write = _make_mock_tool("filesystem_write")
    git_commit = _make_mock_tool("git_commit")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("mcp:filesystem_*",),
        request_tools=[fs_read, fs_write, git_commit],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert fs_read in result
    assert fs_write in result
    assert git_commit not in result


@pytest.mark.unit
def test_resolve_mcp_glob_star_matches_all() -> None:
    """'mcp:*' token matches all request_tools."""
    from atelier.subagents import _resolve_tool_tokens

    tool_a = _make_mock_tool("anything")
    tool_b = _make_mock_tool("other_thing")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("mcp:*",),
        request_tools=[tool_a, tool_b],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert tool_a in result
    assert tool_b in result


@pytest.mark.unit
def test_resolve_mcp_glob_no_match_returns_empty() -> None:
    """'mcp:<glob>' with no match returns empty list."""
    from atelier.subagents import _resolve_tool_tokens

    tool_a = _make_mock_tool("unrelated_tool")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("mcp:nonexistent_*",),
        request_tools=[tool_a],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert result == []


@pytest.mark.unit
def test_resolve_bare_name_from_tool_registry() -> None:
    """Bare name token resolves via tool_registry.get()."""
    from atelier.subagents import _resolve_tool_tokens

    static_tool = _make_mock_tool("read_config_file")
    registry = _make_fake_tool_registry({"read_config_file": static_tool})

    result = _resolve_tool_tokens(
        tokens=("read_config_file",),
        request_tools=[],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert static_tool in result
    assert len(result) == 1


@pytest.mark.unit
def test_resolve_local_token_from_local_tools() -> None:
    """'local:<name>' token resolves from the local_tools dict."""
    from atelier.subagents import _resolve_tool_tokens

    local_tool = _make_mock_tool("my_search")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("local:my_search",),
        request_tools=[],
        tool_registry=registry,
        local_tools={"my_search": local_tool},
        spec_name="test-agent",
    )

    assert local_tool in result
    assert len(result) == 1


@pytest.mark.unit
def test_resolve_local_token_unknown_name_logs_warning_and_drops(caplog) -> None:
    """'local:<name>' with no matching tool logs WARNING and is dropped."""
    from atelier.subagents import _resolve_tool_tokens

    registry = _make_fake_tool_registry()

    with caplog.at_level(logging.WARNING):
        result = _resolve_tool_tokens(
            tokens=("local:nonexistent",),
            request_tools=[],
            tool_registry=registry,
            local_tools={},
            spec_name="my-agent",
        )

    assert result == []
    assert any(
        "nonexistent" in r.message or "my-agent" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_resolve_unknown_bare_name_logs_warning_and_drops(caplog) -> None:
    """Unknown static tool name is logged as WARNING and dropped."""
    from atelier.subagents import _resolve_tool_tokens

    registry = _make_fake_tool_registry()  # empty, no tools

    with caplog.at_level(logging.WARNING):
        result = _resolve_tool_tokens(
            tokens=("nonexistent_tool",),
            request_tools=[],
            tool_registry=registry,
            local_tools={},
            spec_name="my-agent",
        )

    assert result == []
    assert any(
        "nonexistent_tool" in r.message or "my-agent" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_resolve_mixed_tokens() -> None:
    """Mixed mcp:<glob> + inherit + bare_name + local: all resolve correctly."""
    from atelier.subagents import _resolve_tool_tokens

    fs_read = _make_mock_tool("fs_read")
    fs_write = _make_mock_tool("fs_write")
    git_tool = _make_mock_tool("git_log")
    static_tool = _make_mock_tool("my_static")
    local_tool = _make_mock_tool("local_helper")

    registry = _make_fake_tool_registry({"my_static": static_tool})

    result = _resolve_tool_tokens(
        tokens=("mcp:fs_*", "my_static", "local:local_helper"),
        request_tools=[fs_read, fs_write, git_tool],
        tool_registry=registry,
        local_tools={"local_helper": local_tool},
        spec_name="test-agent",
    )

    assert fs_read in result
    assert fs_write in result
    assert static_tool in result
    assert local_tool in result
    assert git_tool not in result


@pytest.mark.unit
def test_resolve_inherit_does_not_widen_beyond_request_tools() -> None:
    """inherit only yields tools already in request_tools — security boundary."""
    from atelier.subagents import _resolve_tool_tokens

    allowed_tool = _make_mock_tool("allowed")
    blocked_tool = _make_mock_tool("blocked")

    # blocked_tool is in static registry but NOT in request_tools
    registry = _make_fake_tool_registry({"blocked": blocked_tool})

    result = _resolve_tool_tokens(
        tokens=("inherit",),
        request_tools=[allowed_tool],  # only allowed_tool is in scope
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert allowed_tool in result
    assert blocked_tool not in result


@pytest.mark.unit
def test_resolve_deduplicates_tools_from_multiple_tokens() -> None:
    """Same tool matched by two different tokens is included only once."""
    from atelier.subagents import _resolve_tool_tokens

    tool = _make_mock_tool("fs_read")
    registry = _make_fake_tool_registry()

    result = _resolve_tool_tokens(
        tokens=("mcp:fs_read", "inherit"),  # both would include fs_read
        request_tools=[tool],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert result.count(tool) == 1


@pytest.mark.unit
def test_resolve_empty_tokens_returns_empty_list() -> None:
    """Empty tokens tuple always returns empty list."""
    from atelier.subagents import _resolve_tool_tokens

    tool = _make_mock_tool("any_tool")
    registry = _make_fake_tool_registry({"any_tool": tool})

    result = _resolve_tool_tokens(
        tokens=(),
        request_tools=[tool],
        tool_registry=registry,
        local_tools={},
        spec_name="test-agent",
    )

    assert result == []


# ---------------------------------------------------------------------------
# specs_for_user with tool resolution — integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_specs_for_user_resolves_mcp_tokens(tmp_path: Path) -> None:
    """specs_for_user resolves mcp: tokens against request_tools."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "mcp-agent", extra={"tool_tokens": ["mcp:git_*"]})

    git_commit = _make_mock_tool("git_commit")
    git_log = _make_mock_tool("git_log")
    unrelated = _make_mock_tool("fs_read")

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    user_record = {"allowed_subagents": ["*"]}
    specs = subagent_reg.specs_for_user(user_record, request_tools=[git_commit, git_log, unrelated])

    assert len(specs) == 1
    tools = specs[0]["tools"]
    assert git_commit in tools
    assert git_log in tools
    assert unrelated not in tools


@pytest.mark.unit
def test_specs_for_user_resolves_inherit_tokens(tmp_path: Path) -> None:
    """specs_for_user resolves 'inherit' token to all request_tools."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "all-tools-agent", extra={"tool_tokens": ["inherit"]})

    tool_x = _make_mock_tool("tool_x")
    tool_y = _make_mock_tool("tool_y")

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    specs = subagent_reg.specs_for_user(
        {"allowed_subagents": ["*"]},
        request_tools=[tool_x, tool_y],
    )

    tools = specs[0]["tools"]
    assert tool_x in tools
    assert tool_y in tools


@pytest.mark.unit
def test_specs_for_user_resolves_static_name_tokens(tmp_path: Path) -> None:
    """specs_for_user resolves bare name tokens from ToolRegistry."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "static-agent", extra={"tool_tokens": ["read_config"]})

    static_tool = _make_mock_tool("read_config")
    tool_reg = _make_fake_tool_registry({"read_config": static_tool})

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(tool_reg)

    specs = subagent_reg.specs_for_user(
        {"allowed_subagents": ["*"]},
        request_tools=[],
    )

    assert static_tool in specs[0]["tools"]


@pytest.mark.unit
def test_specs_for_user_returns_empty_when_no_allowed(tmp_path: Path) -> None:
    """specs_for_user returns [] when user has no allowed_subagents."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "some-agent")

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    specs = subagent_reg.specs_for_user({"allowed_subagents": []}, request_tools=[])
    assert specs == []


@pytest.mark.unit
def test_specs_for_user_default_request_tools_is_empty_list(tmp_path: Path) -> None:
    """specs_for_user works when request_tools is not provided (defaults to [])."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "no-tools-agent")  # no tool_tokens field

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    # Call without request_tools — should not raise
    specs = subagent_reg.specs_for_user({"allowed_subagents": ["*"]})

    assert len(specs) == 1
    # tools key is omitted when empty
    assert specs[0].get("tools", []) == []


@pytest.mark.unit
def test_specs_for_user_result_dict_has_required_keys(tmp_path: Path) -> None:
    """Each dict returned by specs_for_user has name, description, system_prompt."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "full-agent")

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    specs = subagent_reg.specs_for_user({"allowed_subagents": ["*"]})

    assert len(specs) == 1
    spec = specs[0]
    assert "name" in spec
    assert "description" in spec
    assert "system_prompt" in spec


@pytest.mark.unit
def test_specs_for_user_omits_tools_key_when_empty(tmp_path: Path) -> None:
    """specs_for_user omits the 'tools' key entirely when no tools resolved."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "no-tools-agent")  # no tool_tokens

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    specs = subagent_reg.specs_for_user({"allowed_subagents": ["*"]})

    assert len(specs) == 1
    assert "tools" not in specs[0]


@pytest.mark.unit
def test_specs_for_user_omits_skills_key_when_empty(tmp_path: Path) -> None:
    """specs_for_user omits the 'skills' key entirely when no skills resolved."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "no-skills-agent")  # no skill_tokens

    registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]):
        subagent_reg = SubagentRegistry.load(registry)

    specs = subagent_reg.specs_for_user({"allowed_subagents": ["*"]})

    assert len(specs) == 1
    assert "skills" not in specs[0]
