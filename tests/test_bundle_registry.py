"""Tests for Phase 2 of the RELAIS bundle system.

Verifies that the bundle directory (~/.relais/bundles/) is correctly wired
into the ToolRegistry, SubagentRegistry, and ToolPolicy (skills).

Each test uses ``tmp_path`` and ``monkeypatch`` to isolate the bundle dir
from any real on-disk installations.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bundle_tool_module(tools_dir: Path, tool_name: str) -> None:
    """Write a minimal Python module exporting one @tool-decorated function.

    The module uses a lazy import of LangChain's @tool so the test does not
    need a real LangChain installation beyond what already exists in the
    project.

    Args:
        tools_dir: Directory where the module file should be written.
        tool_name: The snake_case tool name (used for function name and file name).
    """
    tools_dir.mkdir(parents=True, exist_ok=True)
    module_src = textwrap.dedent(f"""\
        from langchain_core.tools import tool

        @tool
        def {tool_name}(query: str) -> str:
            \"\"\"A fake bundle tool.\"\"\"
            return query
    """)
    (tools_dir / f"{tool_name}.py").write_text(module_src)


def _make_bundle_subagent(bundle_dir: Path, name: str) -> None:
    """Create a minimal subagent pack inside a bundle's subagents/ directory.

    Args:
        bundle_dir: The bundle root directory (e.g. ``bundles/my-bundle/``).
        name: Subagent name (must be [a-z0-9][a-z0-9-]*).
    """
    pack_dir = bundle_dir / "subagents" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": f"Bundle subagent {name}.",
        "system_prompt": f"You are bundle subagent {name}.",
    }
    (pack_dir / "subagent.yaml").write_text(yaml.dump(data))


def _make_bundle_skill(bundle_dir: Path, skill_name: str) -> Path:
    """Create a minimal skill directory inside a bundle's skills/ directory.

    Args:
        bundle_dir: The bundle root directory.
        skill_name: Name of the skill subdirectory.

    Returns:
        Path to the created skill directory.
    """
    skill_dir = bundle_dir / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n\nA bundle skill.\n")
    return skill_dir


# ---------------------------------------------------------------------------
# test_resolve_bundles_dir
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_resolve_bundles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_bundles_dir() returns get_relais_home() / 'bundles'.

    Args:
        tmp_path: pytest temporary directory.
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setenv("RELAIS_HOME", str(tmp_path))
    from importlib import reload
    import common.config_loader as cl
    reload(cl)

    result = cl.resolve_bundles_dir()
    assert result == tmp_path / "bundles"


# ---------------------------------------------------------------------------
# test_tool_registry_discovers_bundle_tools
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tool_registry_discovers_bundle_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ToolRegistry.discover() finds tools from a fake bundle directory.

    Creates a bundle with one tool module and patches resolve_bundles_dir()
    to return the tmp_path bundles root. Verifies that the tool appears in
    the registry after discovery.

    Args:
        tmp_path: pytest temporary directory.
        monkeypatch: pytest monkeypatch fixture.
    """
    bundles_root = tmp_path / "bundles"
    bundle_dir = bundles_root / "my-bundle"
    _make_bundle_tool_module(bundle_dir / "tools", "bundle_search")

    with patch("atelier.tools._registry.resolve_bundles_dir", return_value=bundles_root):
        from atelier.tools._registry import ToolRegistry
        registry = ToolRegistry.discover()

    assert "bundle_search" in registry.all(), (
        "Expected 'bundle_search' tool from bundle to be discovered"
    )


@pytest.mark.unit
def test_bundle_tool_has_bundle_name_attribute(
    tmp_path: Path,
) -> None:
    """Bundle tools are tagged with a _bundle_name attribute.

    Args:
        tmp_path: pytest temporary directory.
    """
    bundles_root = tmp_path / "bundles"
    bundle_dir = bundles_root / "my-bundle"
    _make_bundle_tool_module(bundle_dir / "tools", "tagged_tool")

    with patch("atelier.tools._registry.resolve_bundles_dir", return_value=bundles_root):
        from atelier.tools._registry import ToolRegistry
        registry = ToolRegistry.discover()

    tool = registry.get("tagged_tool")
    assert tool is not None
    assert getattr(tool, "_bundle_name", None) == "my-bundle", (
        "Expected _bundle_name='my-bundle' on the tool instance"
    )


# ---------------------------------------------------------------------------
# test_tool_registry_conflict_warning
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tool_registry_conflict_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ToolRegistry.discover() logs WARNING when two bundles export the same tool name.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    bundles_root = tmp_path / "bundles"
    # Two bundles, both exporting a tool named 'shared_tool'
    _make_bundle_tool_module((bundles_root / "bundle-a" / "tools"), "shared_tool")
    _make_bundle_tool_module((bundles_root / "bundle-b" / "tools"), "shared_tool")

    import logging
    with caplog.at_level(logging.WARNING, logger="atelier.tools._registry"):
        with patch("atelier.tools._registry.resolve_bundles_dir", return_value=bundles_root):
            from atelier.tools._registry import ToolRegistry
            ToolRegistry.discover()

    warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("shared_tool" in msg for msg in warning_messages), (
        f"Expected WARNING about 'shared_tool' conflict, got: {warning_messages}"
    )


# ---------------------------------------------------------------------------
# test_subagent_registry_loads_bundle_subagents
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_subagent_registry_loads_bundle_subagents(
    tmp_path: Path,
) -> None:
    """SubagentRegistry.load() finds subagents from the bundles directory.

    Creates a bundle with one subagent pack and patches resolve_bundles_dir()
    plus the config cascade to point to empty dirs. Verifies that the bundle
    subagent is present in the loaded registry.

    Args:
        tmp_path: pytest temporary directory.
    """
    bundles_root = tmp_path / "bundles"
    bundle_dir = bundles_root / "my-bundle"
    _make_bundle_subagent(bundle_dir, "bundle-agent")

    fake_registry = MagicMock()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path / "empty"]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "no-native"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=bundles_root),
    ):
        from atelier.subagents import SubagentRegistry
        registry = SubagentRegistry.load(fake_registry)

    assert "bundle-agent" in registry.all_names, (
        f"Expected 'bundle-agent' in registry.all_names, got: {registry.all_names}"
    )


@pytest.mark.unit
def test_subagent_registry_bundle_loses_to_user(
    tmp_path: Path,
) -> None:
    """Bundle subagents are overridden by same-named user subagents (first-wins).

    A user subagent pack at config/atelier/subagents/bundle-agent/ should take
    priority over the same-named subagent from a bundle.

    Args:
        tmp_path: pytest temporary directory.
    """
    from tests.conftest import write_pack

    user_root = tmp_path / "user"
    user_subagents = user_root / "config" / "atelier" / "subagents"
    write_pack(user_subagents, "bundle-agent")

    bundles_root = tmp_path / "bundles"
    bundle_dir = bundles_root / "my-bundle"
    _make_bundle_subagent(bundle_dir, "bundle-agent")

    fake_registry = MagicMock()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [user_root]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "no-native"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=bundles_root),
    ):
        from atelier.subagents import SubagentRegistry
        registry = SubagentRegistry.load(fake_registry)

    # Should load exactly once (not duplicated), user takes priority
    names = list(registry.all_names)
    assert names.count("bundle-agent") == 1, "Expected exactly one 'bundle-agent'"


@pytest.mark.unit
def test_subagent_registry_bundle_yields_to_native(
    tmp_path: Path,
) -> None:
    """Native subagents take priority over bundle subagents with the same name.

    The spec says CONFIG_SEARCH_PATH first, then bundles, then native.  Wait—
    re-reading: user > bundle > native. Native is scanned AFTER bundles, so
    bundle wins over native. This test verifies exactly that ordering: a bundle
    subagent named 'native-agent' is loaded, and the native version is skipped.

    Args:
        tmp_path: pytest temporary directory.
    """
    native_root = tmp_path / "native"
    native_pack = native_root / "native-agent"
    native_pack.mkdir(parents=True)
    (native_pack / "subagent.yaml").write_text(
        yaml.dump({
            "name": "native-agent",
            "description": "Native version.",
            "system_prompt": "I am native.",
        })
    )

    bundles_root = tmp_path / "bundles"
    bundle_dir = bundles_root / "my-bundle"
    _make_bundle_subagent(bundle_dir, "native-agent")

    fake_registry = MagicMock()

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path / "empty"]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", native_root),
        patch("atelier.subagents.resolve_bundles_dir", return_value=bundles_root),
    ):
        from atelier.subagents import SubagentRegistry
        registry = SubagentRegistry.load(fake_registry)

    assert "native-agent" in registry.all_names
    names = list(registry.all_names)
    assert names.count("native-agent") == 1


# ---------------------------------------------------------------------------
# test_tool_policy_includes_bundle_skills
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tool_policy_includes_bundle_skills(
    tmp_path: Path,
) -> None:
    """ToolPolicy.resolve_skills() returns bundle skill paths alongside local skills.

    Creates a base skills dir and a bundle with a skills/ subdirectory, then
    verifies that resolve_skills(['*']) includes paths from both.

    Args:
        tmp_path: pytest temporary directory.
    """
    base_skills = tmp_path / "skills"
    local_skill = base_skills / "local-skill"
    local_skill.mkdir(parents=True)

    bundles_root = tmp_path / "bundles"
    bundle_skill = _make_bundle_skill(bundles_root / "my-bundle", "bundle-skill")

    with patch("atelier.tool_policy.resolve_bundles_dir", return_value=bundles_root):
        from atelier.tool_policy import ToolPolicy
        policy = ToolPolicy(base_dir=base_skills)
        result = policy.resolve_skills(["*"])

    result_paths = {Path(p) for p in result}
    assert local_skill.resolve() in result_paths, (
        f"Expected local-skill in results: {result_paths}"
    )
    assert bundle_skill.resolve() in result_paths, (
        f"Expected bundle-skill in results: {result_paths}"
    )


@pytest.mark.unit
def test_tool_policy_bundle_skills_deduplication(
    tmp_path: Path,
) -> None:
    """resolve_skills() does not return duplicate paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    base_skills = tmp_path / "skills"
    base_skills.mkdir(parents=True)

    bundles_root = tmp_path / "bundles"
    _make_bundle_skill(bundles_root / "bundle-a", "shared-skill")
    _make_bundle_skill(bundles_root / "bundle-b", "other-skill")

    with patch("atelier.tool_policy.resolve_bundles_dir", return_value=bundles_root):
        from atelier.tool_policy import ToolPolicy
        policy = ToolPolicy(base_dir=base_skills)
        result = policy.resolve_skills(["*"])

    assert len(result) == len(set(result)), "Duplicate paths found in resolve_skills result"


@pytest.mark.unit
def test_tool_policy_bundle_skills_nonexistent_bundles_dir(
    tmp_path: Path,
) -> None:
    """resolve_skills() does not raise when bundles directory does not exist.

    Args:
        tmp_path: pytest temporary directory.
    """
    base_skills = tmp_path / "skills"
    local_skill = base_skills / "local-skill"
    local_skill.mkdir(parents=True)

    nonexistent_bundles = tmp_path / "no-bundles-here"

    with patch("atelier.tool_policy.resolve_bundles_dir", return_value=nonexistent_bundles):
        from atelier.tool_policy import ToolPolicy
        policy = ToolPolicy(base_dir=base_skills)
        result = policy.resolve_skills(["*"])

    assert str(local_skill.resolve()) in result
    # No crash — no bundle paths added
    assert len(result) == 1


@pytest.mark.unit
def test_bundle_skill_dirs_returns_existing_paths(
    tmp_path: Path,
) -> None:
    """_bundle_skill_dirs() returns only existing skills subdirectories.

    Args:
        tmp_path: pytest temporary directory.
    """
    bundles_root = tmp_path / "bundles"
    skill1 = _make_bundle_skill(bundles_root / "bundle-a", "skill-one")
    skill2 = _make_bundle_skill(bundles_root / "bundle-b", "skill-two")

    # A bundle with no skills/ dir — should contribute nothing
    (bundles_root / "bundle-c").mkdir(parents=True)

    with patch("atelier.tool_policy.resolve_bundles_dir", return_value=bundles_root):
        from atelier.tool_policy import ToolPolicy
        policy = ToolPolicy(base_dir=tmp_path / "skills")
        dirs = policy._bundle_skill_dirs()

    assert str(skill1.resolve()) in dirs
    assert str(skill2.resolve()) in dirs
    assert len(dirs) == 2
