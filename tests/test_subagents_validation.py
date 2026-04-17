"""Unit tests for subagent tool token startup validation and degraded state tracking.

Tests validate:
- SubagentSpec has degraded_tokens field defaulting to empty tuple
- validate_module_token returns None for valid tokens, error string for invalid
- SubagentRegistry.load() marks specs with invalid module: tokens as degraded
- SubagentRegistry.load() marks specs with invalid bare <name> tokens as degraded
- mcp:, inherit, local: tokens are skipped during startup validation
- specs_for_user updates degraded_tokens when _resolve_tool_tokens drops tokens at runtime
- degraded_names property returns frozenset of degraded subagent names
- hot-reload (re-load) revalidates all specs
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import (
    isolated_search_path as _isolated_search_path,
    make_fake_tool_registry as _make_fake_tool_registry,
    make_mock_tool as _make_mock_tool,
    write_pack as _write_pack,
)


# ---------------------------------------------------------------------------
# SubagentSpec.degraded_tokens — field existence and default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_spec_has_degraded_tokens_field_defaulting_to_empty() -> None:
    """SubagentSpec has degraded_tokens field defaulting to empty tuple."""
    from atelier.subagents import SubagentSpec

    spec = SubagentSpec(
        name="test-agent",
        description="Test agent",
        system_prompt="You are a test agent.",
        tool_tokens=(),
        skill_tokens=(),
        delegation_snippet=None,
        source_path=Path("/fake/subagent.yaml"),
        pack_dir=Path("/fake"),
    )

    assert hasattr(spec, "degraded_tokens")
    assert spec.degraded_tokens == ()


@pytest.mark.unit
def test_subagent_spec_degraded_tokens_can_be_set_via_replace() -> None:
    """dataclasses.replace() can set degraded_tokens on a frozen SubagentSpec."""
    import dataclasses

    from atelier.subagents import SubagentSpec

    spec = SubagentSpec(
        name="test-agent",
        description="Test agent",
        system_prompt="You are a test agent.",
        tool_tokens=("missing_tool",),
        skill_tokens=(),
        delegation_snippet=None,
        source_path=Path("/fake/subagent.yaml"),
        pack_dir=Path("/fake"),
    )

    updated = dataclasses.replace(spec, degraded_tokens=("missing_tool",))
    assert updated.degraded_tokens == ("missing_tool",)
    # Original is unchanged (frozen)
    assert spec.degraded_tokens == ()


# ---------------------------------------------------------------------------
# validate_module_token — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_module_token_returns_none_for_valid_module() -> None:
    """validate_module_token returns None when module is importable and exports tools."""
    from atelier.subagents_resolver import validate_module_token

    mock_tool = _make_mock_tool("my_tool")

    with patch(
        "atelier.subagents_resolver._load_tools_from_import",
        return_value={"my_tool": mock_tool},
    ):
        result = validate_module_token(
            "aiguilleur.channels.whatsapp.tools", "test-agent"
        )

    assert result is None


@pytest.mark.unit
def test_validate_module_token_returns_error_for_disallowed_prefix() -> None:
    """validate_module_token returns error string for disallowed module prefix."""
    from atelier.subagents_resolver import validate_module_token

    result = validate_module_token("os.path", "test-agent")

    assert result is not None
    assert isinstance(result, str)


@pytest.mark.unit
def test_validate_module_token_returns_error_when_import_fails() -> None:
    """validate_module_token returns error when _load_tools_from_import returns {} (import error)."""
    from atelier.subagents_resolver import validate_module_token

    # _load_tools_from_import returns {} on import failure
    with patch(
        "atelier.subagents_resolver._load_tools_from_import",
        return_value={},
    ):
        result = validate_module_token(
            "atelier.tools.nonexistent_module_xyz", "test-agent"
        )

    assert result is not None


@pytest.mark.unit
def test_validate_module_token_returns_error_when_no_tools_exported() -> None:
    """validate_module_token returns error when module exports zero BaseTools."""
    from atelier.subagents_resolver import validate_module_token

    with patch(
        "atelier.subagents_resolver._load_tools_from_import",
        return_value={},
    ):
        result = validate_module_token("atelier.tools.empty_module", "test-agent")

    assert result is not None


# ---------------------------------------------------------------------------
# SubagentRegistry.load() — startup validation via _validate_tool_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_marks_degraded_for_missing_static_tool(tmp_path: Path, caplog) -> None:
    """load() marks subagent as degraded when a bare name token is not in ToolRegistry."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "my-agent", extra={"tool_tokens": ["nonexistent_tool"]})

    registry = _make_fake_tool_registry()  # empty — nonexistent_tool not present

    with caplog.at_level(logging.WARNING):
        with _isolated_search_path(tmp_path):
            reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "my-agent")
    assert "nonexistent_tool" in spec.degraded_tokens
    assert any(
        "nonexistent_tool" in r.message or "my-agent" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_load_no_degradation_for_valid_static_tool(tmp_path: Path) -> None:
    """load() does not degrade a subagent whose bare name token exists in ToolRegistry."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "good-agent", extra={"tool_tokens": ["known_tool"]})

    static_tool = _make_mock_tool("known_tool")
    registry = _make_fake_tool_registry({"known_tool": static_tool})

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "good-agent")
    assert spec.degraded_tokens == ()


@pytest.mark.unit
def test_load_marks_degraded_for_invalid_module_token(tmp_path: Path, caplog) -> None:
    """load() marks subagent as degraded when module: token references a bad module."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "module-agent",
        extra={"tool_tokens": ["module:atelier.tools.nonexistent_xyz_module"]},
    )

    registry = _make_fake_tool_registry()

    with caplog.at_level(logging.WARNING):
        with _isolated_search_path(tmp_path):
            reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "module-agent")
    assert "module:atelier.tools.nonexistent_xyz_module" in spec.degraded_tokens


@pytest.mark.unit
def test_load_skips_mcp_and_inherit_tokens_for_degradation(tmp_path: Path) -> None:
    """load() never degrades a subagent for mcp: or inherit tokens."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "dynamic-agent",
        extra={"tool_tokens": ["mcp:*", "inherit"]},
    )

    registry = _make_fake_tool_registry()  # empty, but mcp/inherit never checked

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "dynamic-agent")
    assert spec.degraded_tokens == ()


@pytest.mark.unit
def test_load_skips_local_tokens_for_startup_degradation(tmp_path: Path) -> None:
    """load() does not degrade a subagent for local: tokens (validated at runtime)."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "local-agent",
        extra={"tool_tokens": ["local:my_local_tool"]},
    )

    registry = _make_fake_tool_registry()

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "local-agent")
    assert spec.degraded_tokens == ()


@pytest.mark.unit
def test_load_marks_only_the_invalid_tokens_in_mixed_list(tmp_path: Path) -> None:
    """degraded_tokens contains only the invalid tokens, not the valid ones."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "mixed-agent",
        extra={"tool_tokens": ["known_tool", "unknown_tool", "inherit"]},
    )

    static_tool = _make_mock_tool("known_tool")
    registry = _make_fake_tool_registry({"known_tool": static_tool})

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    spec = next(s for s in reg._specs if s.name == "mixed-agent")
    assert "unknown_tool" in spec.degraded_tokens
    assert "known_tool" not in spec.degraded_tokens
    assert "inherit" not in spec.degraded_tokens


# ---------------------------------------------------------------------------
# degraded_names property
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_degraded_names_returns_frozenset_with_degraded_subagents(tmp_path: Path) -> None:
    """degraded_names frozenset contains only subagents with invalid tokens."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "healthy-agent", extra={"tool_tokens": ["known_tool"]})
    _write_pack(subagents_dir, "sick-agent", extra={"tool_tokens": ["missing_tool"]})

    static_tool = _make_mock_tool("known_tool")
    registry = _make_fake_tool_registry({"known_tool": static_tool})

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    assert isinstance(reg.degraded_names, frozenset)
    assert "sick-agent" in reg.degraded_names
    assert "healthy-agent" not in reg.degraded_names


@pytest.mark.unit
def test_degraded_names_empty_when_all_subagents_are_valid(tmp_path: Path) -> None:
    """degraded_names is an empty frozenset when all subagents are valid."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "agent-a", extra={"tool_tokens": ["tool_a"]})
    _write_pack(subagents_dir, "agent-b", extra={"tool_tokens": ["inherit"]})

    tool_a = _make_mock_tool("tool_a")
    registry = _make_fake_tool_registry({"tool_a": tool_a})

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    assert reg.degraded_names == frozenset()


# ---------------------------------------------------------------------------
# specs_for_user — runtime degraded_tokens update
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_specs_for_user_updates_degraded_tokens_when_local_tool_missing(
    tmp_path: Path,
) -> None:
    """specs_for_user sets degraded_tokens on spec when local: tool is not found."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    # local: token references a tool that does not exist in the pack
    _write_pack(
        subagents_dir,
        "runtime-agent",
        extra={"tool_tokens": ["local:missing_tool"]},
    )

    registry = _make_fake_tool_registry()

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    # local: is skipped at startup, so not yet degraded
    spec_before = next(s for s in reg._specs if s.name == "runtime-agent")
    assert "local:missing_tool" not in spec_before.degraded_tokens

    reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    # After runtime call — _runtime_degraded updated (specs tuple is immutable)
    runtime_degraded = reg._runtime_degraded.get("runtime-agent", frozenset())
    assert "local:missing_tool" in runtime_degraded


@pytest.mark.unit
def test_specs_for_user_degraded_names_updated_after_runtime_drop(
    tmp_path: Path,
) -> None:
    """degraded_names reflects runtime drops after specs_for_user is called."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir, "lazy-agent", extra={"tool_tokens": ["local:ghost_tool"]}
    )

    registry = _make_fake_tool_registry()

    with _isolated_search_path(tmp_path):
        reg = SubagentRegistry.load(registry)

    assert "lazy-agent" not in reg.degraded_names  # not yet degraded at startup

    reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert "lazy-agent" in reg.degraded_names


# ---------------------------------------------------------------------------
# hot-reload revalidation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hot_reload_revalidates_and_clears_degradation(tmp_path: Path) -> None:
    """A second load() call re-evaluates and clears degradation when tokens become valid."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "reload-agent", extra={"tool_tokens": ["the_tool"]})

    # First load: tool missing → degraded
    empty_registry = _make_fake_tool_registry()
    with _isolated_search_path(tmp_path):
        reg1 = SubagentRegistry.load(empty_registry)

    spec1 = next(s for s in reg1._specs if s.name == "reload-agent")
    assert "the_tool" in spec1.degraded_tokens

    # Second load (hot-reload): tool now present → not degraded
    the_tool = _make_mock_tool("the_tool")
    full_registry = _make_fake_tool_registry({"the_tool": the_tool})
    with _isolated_search_path(tmp_path):
        reg2 = SubagentRegistry.load(full_registry)

    spec2 = next(s for s in reg2._specs if s.name == "reload-agent")
    assert spec2.degraded_tokens == ()
    assert "reload-agent" not in reg2.degraded_names
