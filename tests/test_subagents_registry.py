"""Unit tests for atelier.subagents — SubagentSpec + SubagentRegistry.load().

Tests validate:
- SubagentSpec is a frozen dataclass with correct fields
- SubagentRegistry.load() walks the cascade and loads valid YAML files
- Cascade merge: user priority (first occurrence in path order wins)
- Malformed YAML is logged as ERROR and skipped
- Missing required fields raise ValueError (logged + skipped per file)
- File stem must equal name field
- delegation_prompt_for_user generates correct text
- all_names property returns discovered names
- Unknown extra YAML fields are logged as WARNING, not rejected
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_subagent_yaml(directory: Path, name: str, extra: dict | None = None) -> Path:
    """Write a minimal valid subagent YAML file to *directory*.

    Args:
        directory: Target config/atelier/subagents/ directory.
        name: The subagent name (also used as file stem).
        extra: Extra keys to merge into the YAML data.

    Returns:
        Path to the written file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are the {name} agent.",
    }
    if extra:
        data.update(extra)
    path = directory / f"{name}.yaml"
    path.write_text(yaml.dump(data))
    return path


def _make_fake_tool_registry(tools: dict | None = None) -> MagicMock:
    """Return a mock ToolRegistry with optional pre-populated tools.

    Args:
        tools: Dict mapping name -> BaseTool mock.

    Returns:
        A MagicMock that behaves like ToolRegistry.
    """
    registry = MagicMock()
    registry.get = lambda name: (tools or {}).get(name)
    registry.all = lambda: dict(tools or {})
    return registry


# ---------------------------------------------------------------------------
# SubagentSpec — dataclass shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_spec_is_importable() -> None:
    """SubagentSpec must be importable from atelier.subagents."""
    from atelier.subagents import SubagentSpec
    assert SubagentSpec is not None


@pytest.mark.unit
def test_subagent_spec_is_frozen_dataclass() -> None:
    """SubagentSpec must be a frozen dataclass."""
    import dataclasses
    from atelier.subagents import SubagentSpec

    assert dataclasses.is_dataclass(SubagentSpec)
    spec = SubagentSpec(
        name="test",
        description="A test subagent",
        system_prompt="You are a test agent.",
        tools=(),
        delegation_snippet=None,
        source_path=Path("/fake/test.yaml"),
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        spec.name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_subagent_spec_has_required_fields() -> None:
    """SubagentSpec must have all required fields."""
    from atelier.subagents import SubagentSpec
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(SubagentSpec)}
    required = {"name", "description", "system_prompt", "tools", "delegation_snippet", "source_path"}
    assert required.issubset(field_names), f"Missing fields: {required - field_names}"


@pytest.mark.unit
def test_subagent_spec_tools_is_tuple() -> None:
    """SubagentSpec.tools stores raw YAML tokens as a tuple of strings."""
    from atelier.subagents import SubagentSpec

    spec = SubagentSpec(
        name="x",
        description="desc",
        system_prompt="prompt",
        tools=("mcp:fs_*", "inherit", "my_tool"),
        delegation_snippet=None,
        source_path=Path("/fake/x.yaml"),
    )
    assert isinstance(spec.tools, tuple)
    assert spec.tools == ("mcp:fs_*", "inherit", "my_tool")


# ---------------------------------------------------------------------------
# SubagentRegistry.load() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_registry_is_importable() -> None:
    """SubagentRegistry must be importable from atelier.subagents."""
    from atelier.subagents import SubagentRegistry
    assert SubagentRegistry is not None


@pytest.mark.unit
def test_subagent_registry_load_single_file(tmp_path: Path) -> None:
    """load() returns a registry containing one spec from a single valid YAML."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "my-agent")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    assert "my-agent" in registry.all_names


@pytest.mark.unit
def test_subagent_registry_load_empty_when_no_files(tmp_path: Path) -> None:
    """load() returns an empty registry when no YAML files are found."""
    from atelier.subagents import SubagentRegistry

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_cascade_user_priority(tmp_path: Path) -> None:
    """User dir takes priority over project dir for the same subagent name."""
    from atelier.subagents import SubagentRegistry

    user_dir = tmp_path / "user" / "config" / "atelier" / "subagents"
    project_dir = tmp_path / "project" / "config" / "atelier" / "subagents"

    # Both define the same agent; user description should win
    _write_subagent_yaml(user_dir, "shared-agent",
                          extra={"description": "User version"})
    _write_subagent_yaml(project_dir, "shared-agent",
                          extra={"description": "Project version"})

    search_path = [tmp_path / "user", tmp_path / "project"]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    # Only one spec with that name, from user dir
    assert "shared-agent" in registry.all_names
    specs = registry._specs
    spec = next(s for s in specs if s.name == "shared-agent")
    assert spec.description == "User version"


@pytest.mark.unit
def test_subagent_registry_load_multiple_agents(tmp_path: Path) -> None:
    """load() populates registry with all valid agents found across the cascade."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "agent-one")
    _write_subagent_yaml(subagents_dir, "agent-two")
    _write_subagent_yaml(subagents_dir, "agent-three")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset({"agent-one", "agent-two", "agent-three"})


@pytest.mark.unit
def test_subagent_registry_load_populates_tools_tokens(tmp_path: Path) -> None:
    """load() stores raw tool tokens from YAML, not resolved BaseTool instances."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "tool-agent",
                          extra={"tools": ["mcp:fs_*", "inherit", "my_static_tool"]})

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    specs = registry._specs
    spec = next(s for s in specs if s.name == "tool-agent")
    assert "mcp:fs_*" in spec.tools
    assert "inherit" in spec.tools
    assert "my_static_tool" in spec.tools


# ---------------------------------------------------------------------------
# SubagentRegistry.load() — validation: malformed YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_registry_load_skips_malformed_yaml(tmp_path: Path, caplog) -> None:
    """Malformed YAML files are logged as ERROR and skipped; startup continues."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    bad_file = subagents_dir / "broken.yaml"
    bad_file.write_text("name: [unclosed bracket\n  bad: yaml: content\n::::")

    _write_subagent_yaml(subagents_dir, "good-agent")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    # Good agent still loaded
    assert "good-agent" in registry.all_names
    # Error logged for bad file
    assert any("broken" in r.message.lower() or "broken" in str(r) for r in caplog.records)


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_name(tmp_path: Path, caplog) -> None:
    """Files missing the 'name' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    bad = subagents_dir / "no-name.yaml"
    bad.write_text(yaml.dump({
        "description": "Missing name field",
        "system_prompt": "You are a test agent.",
    }))

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()
    assert any("no-name" in r.message or "name" in r.message.lower() for r in caplog.records)


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_description(tmp_path: Path, caplog) -> None:
    """Files missing the 'description' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    bad = subagents_dir / "no-desc.yaml"
    bad.write_text(yaml.dump({
        "name": "no-desc",
        "system_prompt": "You are a test agent.",
    }))

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_system_prompt(tmp_path: Path, caplog) -> None:
    """Files missing the 'system_prompt' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    bad = subagents_dir / "no-prompt.yaml"
    bad.write_text(yaml.dump({
        "name": "no-prompt",
        "description": "Has no system_prompt",
    }))

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_skips_stem_mismatch(tmp_path: Path, caplog) -> None:
    """Files where stem != name are skipped to prevent silent cascade duplicates."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    bad = subagents_dir / "wrong-stem.yaml"
    bad.write_text(yaml.dump({
        "name": "different-name",
        "description": "Stem mismatch",
        "system_prompt": "You are an agent.",
    }))

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()
    assert any("stem" in r.message.lower() or "mismatch" in r.message.lower()
               or "wrong-stem" in r.message for r in caplog.records)


@pytest.mark.unit
def test_subagent_registry_load_warns_on_unknown_fields(tmp_path: Path, caplog) -> None:
    """Files with unknown extra fields emit a WARNING but are still loaded."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)

    _write_subagent_yaml(subagents_dir, "agent-extras",
                          extra={"unknown_field": "some value", "another_extra": 42})

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        caplog.at_level(logging.WARNING),
    ):
        registry = SubagentRegistry.load(tool_registry)

    # Agent is still loaded despite unknown fields
    assert "agent-extras" in registry.all_names
    # Warning about unknown fields
    assert any("unknown" in r.message.lower() or "extra" in r.message.lower()
               or "agent-extras" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# delegation_prompt_for_user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delegation_prompt_for_user_wildcard(tmp_path: Path) -> None:
    """User with ['*'] gets delegation prompt containing all agent names."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "agent-alpha")
    _write_subagent_yaml(subagents_dir, "agent-beta")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": ["*"]})

    assert "agent-alpha" in prompt
    assert "agent-beta" in prompt
    assert "task()" in prompt  # preamble must mention task()


@pytest.mark.unit
def test_delegation_prompt_for_user_empty_when_no_allowed(tmp_path: Path) -> None:
    """User with [] gets an empty delegation prompt."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "agent-alpha")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": []})
    assert prompt == ""


@pytest.mark.unit
def test_delegation_prompt_for_user_uses_delegation_snippet_when_set(tmp_path: Path) -> None:
    """When delegation_snippet is set in YAML, it is used verbatim."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(
        subagents_dir,
        "snip-agent",
        extra={"delegation_snippet": "- **snip-agent**: Custom delegation text here."},
    )

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": ["*"]})
    assert "Custom delegation text here." in prompt


@pytest.mark.unit
def test_delegation_prompt_for_user_auto_generates_snippet(tmp_path: Path) -> None:
    """When delegation_snippet is absent, auto-generate from description first line."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(
        subagents_dir,
        "auto-agent",
        extra={"description": "Reads configuration files.\nSecond line ignored."},
    )

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": ["*"]})
    # Must include auto-agent name and first line of description
    assert "auto-agent" in prompt
    assert "Reads configuration files." in prompt


@pytest.mark.unit
def test_delegation_prompt_for_user_no_field_returns_empty(tmp_path: Path) -> None:
    """User record without allowed_subagents returns empty string."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_subagent_yaml(subagents_dir, "agent-x")

    search_path = [tmp_path]
    tool_registry = _make_fake_tool_registry()

    with patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({})
    assert prompt == ""


# ---------------------------------------------------------------------------
# _parse_subagent_patterns / _matches_patterns boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_patterns_accepts_list() -> None:
    """_parse_subagent_patterns returns a tuple from a list input."""
    from atelier.subagents import _parse_subagent_patterns

    assert _parse_subagent_patterns(["a", "b"]) == ("a", "b")


@pytest.mark.unit
def test_parse_patterns_rejects_non_list() -> None:
    """_parse_subagent_patterns returns () for non-list/tuple inputs."""
    from atelier.subagents import _parse_subagent_patterns

    assert _parse_subagent_patterns(None) == ()
    assert _parse_subagent_patterns("*") == ()
    assert _parse_subagent_patterns(42) == ()
    assert _parse_subagent_patterns({}) == ()


@pytest.mark.unit
def test_parse_patterns_accepts_tuple() -> None:
    """_parse_subagent_patterns accepts a tuple input."""
    from atelier.subagents import _parse_subagent_patterns

    assert _parse_subagent_patterns(("x", "y")) == ("x", "y")
