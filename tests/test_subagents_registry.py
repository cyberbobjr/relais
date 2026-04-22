"""Unit tests for atelier.subagents — SubagentSpec + SubagentRegistry.load().

Tests validate:
- SubagentSpec is a frozen dataclass with correct fields (tool_tokens, skill_tokens, pack_dir)
- SubagentRegistry.load() walks the cascade and loads valid pack directories
- Cascade merge: user priority (first occurrence in path order wins)
- Malformed YAML is logged as ERROR and skipped
- Missing required fields raise ValueError (logged + skipped per directory)
- Directory name must equal name field
- delegation_prompt_for_user generates correct text
- all_names property returns discovered names
- Unknown extra YAML fields are logged as WARNING, not rejected
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture(autouse=True)
def _no_real_bundles(tmp_path: Path):
    """Prevent bundle-tier scan from picking up real ~/.relais/bundles/ during unit tests."""
    with patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_no_bundles_"):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pack(base_dir: Path, name: str, extra: dict | None = None) -> Path:
    """Write a minimal valid subagent pack (directory + subagent.yaml) under *base_dir*.

    Args:
        base_dir: Target ``config/atelier/subagents/`` directory.
        name: The subagent name (also used as directory name).
        extra: Extra keys to merge into the YAML data.

    Returns:
        Path to the pack directory.
    """
    pack_dir = base_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are the {name} agent.",
    }
    if extra:
        data.update(extra)
    (pack_dir / "subagent.yaml").write_text(yaml.dump(data))
    return pack_dir


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
        tool_tokens=(),
        skill_tokens=(),
        delegation_snippet=None,
        source_path=Path("/fake/subagent.yaml"),
        pack_dir=Path("/fake/test"),
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        spec.name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_subagent_spec_has_required_fields() -> None:
    """SubagentSpec must have all required fields including tool_tokens, skill_tokens, pack_dir."""
    from atelier.subagents import SubagentSpec
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(SubagentSpec)}
    required = {
        "name", "description", "system_prompt",
        "tool_tokens", "skill_tokens",
        "delegation_snippet", "source_path", "pack_dir",
    }
    assert required.issubset(field_names), f"Missing fields: {required - field_names}"


@pytest.mark.unit
def test_subagent_spec_tool_tokens_is_tuple() -> None:
    """SubagentSpec.tool_tokens stores raw YAML tokens as a tuple of strings."""
    from atelier.subagents import SubagentSpec

    spec = SubagentSpec(
        name="x",
        description="desc",
        system_prompt="prompt",
        tool_tokens=("mcp:fs_*", "inherit", "my_tool"),
        skill_tokens=(),
        delegation_snippet=None,
        source_path=Path("/fake/subagent.yaml"),
        pack_dir=Path("/fake/x"),
    )
    assert isinstance(spec.tool_tokens, tuple)
    assert spec.tool_tokens == ("mcp:fs_*", "inherit", "my_tool")


@pytest.mark.unit
def test_subagent_spec_skill_tokens_is_tuple() -> None:
    """SubagentSpec.skill_tokens stores raw YAML tokens as a tuple of strings."""
    from atelier.subagents import SubagentSpec

    spec = SubagentSpec(
        name="x",
        description="desc",
        system_prompt="prompt",
        tool_tokens=(),
        skill_tokens=("local:my-skill",),
        delegation_snippet=None,
        source_path=Path("/fake/subagent.yaml"),
        pack_dir=Path("/fake/x"),
    )
    assert isinstance(spec.skill_tokens, tuple)
    assert spec.skill_tokens == ("local:my-skill",)


# ---------------------------------------------------------------------------
# SubagentRegistry.load() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_registry_is_importable() -> None:
    """SubagentRegistry must be importable from atelier.subagents."""
    from atelier.subagents import SubagentRegistry
    assert SubagentRegistry is not None


@pytest.mark.unit
def test_subagent_registry_load_single_pack(tmp_path: Path) -> None:
    """load() returns a registry containing one spec from a single valid pack directory."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "my-agent")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert "my-agent" in registry.all_names


@pytest.mark.unit
def test_subagent_registry_load_empty_when_no_directories(tmp_path: Path) -> None:
    """load() returns an empty registry when no pack directories are found."""
    from atelier.subagents import SubagentRegistry

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_ignores_flat_yaml(tmp_path: Path) -> None:
    """load() ignores flat .yaml files at the subagents/ root level."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    # flat YAML at root — old format, must be ignored
    flat = subagents_dir / "flat-agent.yaml"
    flat.write_text(yaml.dump({
        "name": "flat-agent",
        "description": "flat",
        "system_prompt": "you are flat",
    }))

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_cascade_user_priority(tmp_path: Path) -> None:
    """User dir takes priority over project dir for the same subagent name."""
    from atelier.subagents import SubagentRegistry

    user_subagents = tmp_path / "user" / "config" / "atelier" / "subagents"
    project_subagents = tmp_path / "project" / "config" / "atelier" / "subagents"

    # Both define the same agent; user description should win
    _write_pack(user_subagents, "shared-agent", extra={"description": "User version"})
    _write_pack(project_subagents, "shared-agent", extra={"description": "Project version"})

    search_path = [tmp_path / "user", tmp_path / "project"]
    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", search_path),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    # Only one spec with that name, from user dir
    assert "shared-agent" in registry.all_names
    spec = next(s for s in registry._specs if s.name == "shared-agent")
    assert spec.description == "User version"


@pytest.mark.unit
def test_subagent_registry_load_multiple_agents(tmp_path: Path) -> None:
    """load() populates registry with all valid agents found across the cascade."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "agent-one")
    _write_pack(subagents_dir, "agent-two")
    _write_pack(subagents_dir, "agent-three")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset({"agent-one", "agent-two", "agent-three"})


@pytest.mark.unit
def test_subagent_registry_load_stores_tool_tokens(tmp_path: Path) -> None:
    """load() stores raw tool_tokens from YAML on the spec."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "tool-agent",
                extra={"tool_tokens": ["mcp:fs_*", "inherit", "my_static_tool"]})

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    spec = next(s for s in registry._specs if s.name == "tool-agent")
    assert "mcp:fs_*" in spec.tool_tokens
    assert "inherit" in spec.tool_tokens
    assert "my_static_tool" in spec.tool_tokens


@pytest.mark.unit
def test_subagent_registry_load_stores_skill_tokens(tmp_path: Path) -> None:
    """load() stores raw skill_tokens from YAML on the spec."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "skill-agent",
                extra={"skill_tokens": ["local:my-skill"]})

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    spec = next(s for s in registry._specs if s.name == "skill-agent")
    assert "local:my-skill" in spec.skill_tokens


@pytest.mark.unit
def test_subagent_registry_load_sets_pack_dir(tmp_path: Path) -> None:
    """load() sets pack_dir on each spec to the pack directory path."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(subagents_dir, "dir-agent")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    spec = next(s for s in registry._specs if s.name == "dir-agent")
    assert spec.pack_dir == pack_dir


# ---------------------------------------------------------------------------
# SubagentRegistry.load() — validation: malformed YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_registry_load_skips_malformed_yaml(tmp_path: Path, caplog) -> None:
    """Malformed YAML in a pack is logged as ERROR and skipped; startup continues."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"

    bad_pack = subagents_dir / "broken"
    bad_pack.mkdir(parents=True, exist_ok=True)
    (bad_pack / "subagent.yaml").write_text("name: [unclosed bracket\n  bad: yaml: ::::")

    _write_pack(subagents_dir, "good-agent")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    # Good agent still loaded
    assert "good-agent" in registry.all_names
    # Error logged for bad file
    assert any("broken" in r.message.lower() or "broken" in str(r) for r in caplog.records)


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_name(tmp_path: Path, caplog) -> None:
    """Packs missing the 'name' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    bad_pack = subagents_dir / "no-name"
    bad_pack.mkdir(parents=True, exist_ok=True)
    (bad_pack / "subagent.yaml").write_text(yaml.dump({
        "description": "Missing name field",
        "system_prompt": "You are a test agent.",
    }))

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()
    assert any("no-name" in r.message or "name" in r.message.lower() for r in caplog.records)


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_description(tmp_path: Path, caplog) -> None:
    """Packs missing the 'description' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    bad_pack = subagents_dir / "no-desc"
    bad_pack.mkdir(parents=True, exist_ok=True)
    (bad_pack / "subagent.yaml").write_text(yaml.dump({
        "name": "no-desc",
        "system_prompt": "You are a test agent.",
    }))

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_skips_missing_system_prompt(tmp_path: Path, caplog) -> None:
    """Packs missing the 'system_prompt' field are skipped with an ERROR log."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    bad_pack = subagents_dir / "no-prompt"
    bad_pack.mkdir(parents=True, exist_ok=True)
    (bad_pack / "subagent.yaml").write_text(yaml.dump({
        "name": "no-prompt",
        "description": "Has no system_prompt",
    }))

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()


@pytest.mark.unit
def test_subagent_registry_load_skips_directory_name_mismatch(tmp_path: Path, caplog) -> None:
    """Packs where directory name != name field are skipped to prevent silent cascade duplicates."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    # Directory is named "wrong-dir" but name field says "different-name"
    bad_pack = subagents_dir / "wrong-dir"
    bad_pack.mkdir(parents=True, exist_ok=True)
    (bad_pack / "subagent.yaml").write_text(yaml.dump({
        "name": "different-name",
        "description": "Directory name mismatch",
        "system_prompt": "You are an agent.",
    }))

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert registry.all_names == frozenset()
    assert any(
        "wrong-dir" in r.message or "different-name" in r.message
        for r in caplog.records
    )


@pytest.mark.unit
def test_subagent_registry_load_skips_dir_without_yaml(tmp_path: Path, caplog) -> None:
    """Directories without subagent.yaml are silently skipped (DEBUG log)."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    empty_dir = subagents_dir / "no-yaml"
    empty_dir.mkdir(parents=True, exist_ok=True)
    # no subagent.yaml

    _write_pack(subagents_dir, "good-agent")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    assert "good-agent" in registry.all_names
    assert "no-yaml" not in registry.all_names


@pytest.mark.unit
def test_subagent_registry_load_warns_on_unknown_fields(tmp_path: Path, caplog) -> None:
    """Packs with unknown extra fields emit a WARNING but are still loaded."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "agent-extras",
                extra={"unknown_field": "some value", "another_extra": 42})

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
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
    _write_pack(subagents_dir, "agent-alpha")
    _write_pack(subagents_dir, "agent-beta")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
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
    _write_pack(subagents_dir, "agent-alpha")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": []})
    assert prompt == ""


@pytest.mark.unit
def test_delegation_prompt_for_user_uses_delegation_snippet_when_set(tmp_path: Path) -> None:
    """When delegation_snippet is set in YAML, it is used verbatim."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "snip-agent",
        extra={"delegation_snippet": "- **snip-agent**: Custom delegation text here."},
    )

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": ["*"]})
    assert "Custom delegation text here." in prompt


@pytest.mark.unit
def test_delegation_prompt_for_user_auto_generates_snippet(tmp_path: Path) -> None:
    """When delegation_snippet is absent, auto-generate from description first line."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir,
        "auto-agent",
        extra={"description": "Reads configuration files.\nSecond line ignored."},
    )

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        registry = SubagentRegistry.load(tool_registry)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": ["*"]})
    assert "auto-agent" in prompt
    assert "Reads configuration files." in prompt


@pytest.mark.unit
def test_delegation_prompt_for_user_no_field_returns_empty(tmp_path: Path) -> None:
    """User record without allowed_subagents returns empty string."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "agent-x")

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
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
    """_parse_subagent_patterns returns () for non-string/list/tuple inputs."""
    from atelier.subagents import _parse_subagent_patterns

    assert _parse_subagent_patterns(None) == ()
    assert _parse_subagent_patterns("*") == ("*",)  # bare string is a valid single pattern
    assert _parse_subagent_patterns(42) == ()
    assert _parse_subagent_patterns({}) == ()


@pytest.mark.unit
def test_parse_patterns_accepts_tuple() -> None:
    """_parse_subagent_patterns accepts a tuple input."""
    from atelier.subagents import _parse_subagent_patterns

    assert _parse_subagent_patterns(("x", "y")) == ("x", "y")


# ---------------------------------------------------------------------------
# response_format validation (M1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_specs_for_user_response_format_without_type_is_dropped(
    tmp_path: Path, caplog
) -> None:
    """response_format missing 'type' key must be dropped with WARNING, not forwarded."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    # response_format dict deliberately missing the 'type' key
    _write_pack(
        subagents_dir,
        "fmt-agent",
        extra={"response_format": {"schema": {"type": "object"}}},
    )

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.WARNING),
    ):
        registry = SubagentRegistry.load(tool_registry)

    specs = registry.specs_for_user({"allowed_subagents": ["*"]})

    assert len(specs) == 1
    # response_format must NOT be in the entry dict
    assert "response_format" not in specs[0]
    # A WARNING must be logged mentioning the subagent name
    assert any(
        "fmt-agent" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# F-04 — _load_subagent_tier helper (extracted shared logic)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagent_tier_returns_specs_for_valid_packs(tmp_path: Path) -> None:
    """_load_subagent_tier returns one triple per valid pack directory."""
    from atelier.subagents import _load_subagent_tier

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "tier-agent")

    pack_dirs = sorted(p for p in subagents_dir.iterdir() if p.is_dir())
    seen: set[str] = set()
    results = _load_subagent_tier("", pack_dirs, seen)

    assert len(results) == 1
    spec, tools, skills = results[0]
    assert spec.name == "tier-agent"
    assert isinstance(tools, dict)
    assert isinstance(skills, dict)
    assert "tier-agent" in seen


@pytest.mark.unit
def test_load_subagent_tier_skips_already_seen(tmp_path: Path) -> None:
    """_load_subagent_tier skips packs whose name is already in seen_names."""
    from atelier.subagents import _load_subagent_tier

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "existing-agent")

    pack_dirs = sorted(p for p in subagents_dir.iterdir() if p.is_dir())
    seen: set[str] = {"existing-agent"}  # pre-populated — simulates higher tier
    results = _load_subagent_tier("native", pack_dirs, seen)

    assert results == []


@pytest.mark.unit
def test_load_subagent_tier_logs_tier_name_in_info(tmp_path: Path, caplog) -> None:
    """_load_subagent_tier includes the tier label in its INFO log message."""
    from atelier.subagents import _load_subagent_tier

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "native-pack")

    pack_dirs = sorted(p for p in subagents_dir.iterdir() if p.is_dir())
    seen: set[str] = set()

    with caplog.at_level(logging.INFO):
        _load_subagent_tier("native", pack_dirs, seen)

    assert any(
        "native subagent" in r.message and "native-pack" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# F-17 — WARNING on override: user/native subagent shadows bundle subagent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_logs_warning_on_override(tmp_path: Path, caplog) -> None:
    """load() emits WARNING when a user subagent shadows a bundle subagent with the same name.

    A bundle subagent named 'shared-agent' exists alongside a user config subagent
    with the same name.  The user config must win (first-match-wins) AND a WARNING
    must be logged identifying the shadowed bundle pack.
    """
    from atelier.subagents import SubagentRegistry

    # Tier 1 (user config): defines 'shared-agent'
    user_subagents = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(user_subagents, "shared-agent", extra={"description": "User version"})

    # Tier 3 (bundle): also defines 'shared-agent'
    bundle_dir = tmp_path / "bundles" / "my-bundle" / "subagents"
    _write_pack(bundle_dir, "shared-agent", extra={"description": "Bundle version"})

    tool_registry = _make_fake_tool_registry()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch(
            "atelier.subagents.resolve_bundles_dir",
            return_value=tmp_path / "bundles",
        ),
        caplog.at_level(logging.WARNING),
    ):
        registry = SubagentRegistry.load(tool_registry)

    # User version must be the one registered (first-match-wins)
    assert "shared-agent" in registry.all_names
    spec = next(s for s in registry._specs if s.name == "shared-agent")
    assert spec.description == "User version"

    # F-17: a WARNING must be logged about the shadowed bundle subagent
    assert any(
        "shared-agent" in r.message
        and "shadowed" in r.message
        and r.levelno == logging.WARNING
        for r in caplog.records
    ), f"Expected override WARNING, got: {[r.message for r in caplog.records]}"
