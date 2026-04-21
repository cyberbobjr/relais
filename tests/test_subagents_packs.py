"""Unit tests for atelier.subagents — pack directory features.

Tests validate:
- local: tool tokens resolved from pack's tools/ directory
- local: skill tokens resolved from pack's skills/ directory
- importlib module isolation (no sys.modules insertion)
- Broken tool module doesn't block other modules or the pack
- Path traversal guard on local: skill tokens
- Skill discovery from skills/ subdirectories
- Multiple broken modules still load others
- Tool name collision across modules logs WARNING
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_tool_registry(tools: dict | None = None) -> MagicMock:
    """Return a mock ToolRegistry with .get() and .all() methods.

    Args:
        tools: Optional dict mapping tool name to tool object.

    Returns:
        MagicMock mimicking ToolRegistry.
    """
    registry = MagicMock()
    registry.get = lambda name: (tools or {}).get(name)
    registry.all = lambda: dict(tools or {})
    return registry


def _write_pack(
    base_dir: Path,
    name: str,
    yaml_extra: dict | None = None,
) -> Path:
    """Create a minimal subagent pack directory.

    Args:
        base_dir: The ``config/atelier/subagents/`` directory.
        name: Subagent name — both directory name and ``name`` YAML field.
        yaml_extra: Extra YAML fields to merge into ``subagent.yaml``.

    Returns:
        Path to the created pack directory.
    """
    pack_dir = base_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are {name}.",
    }
    if yaml_extra:
        data.update(yaml_extra)
    (pack_dir / "subagent.yaml").write_text(yaml.dump(data))
    return pack_dir


def _write_tool_module(pack_dir: Path, stem: str, content: str) -> Path:
    """Write a Python tool module inside ``<pack_dir>/tools/``.

    Args:
        pack_dir: The subagent pack directory.
        stem: Module filename without ``.py`` extension.
        content: Python source code string.

    Returns:
        Path to the written ``.py`` file.
    """
    tools_dir = pack_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    py_file = tools_dir / f"{stem}.py"
    py_file.write_text(dedent(content))
    return py_file


def _write_skill_dir(pack_dir: Path, skill_name: str) -> Path:
    """Create a minimal skill directory inside ``<pack_dir>/skills/``.

    Args:
        pack_dir: The subagent pack directory.
        skill_name: Name of the skill directory to create.

    Returns:
        Path to the created skill directory.
    """
    skill_dir = pack_dir / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n\nTest skill.\n")
    return skill_dir


def _load_registry(tmp_path: Path, tool_registry: MagicMock | None = None):
    """Load a SubagentRegistry using tmp_path as the sole config search path.

    Args:
        tmp_path: Temporary directory root (acts as the config cascade root).
        tool_registry: Optional mock ToolRegistry; a default empty one is used if None.

    Returns:
        A loaded SubagentRegistry.
    """
    from atelier.subagents import SubagentRegistry

    if tool_registry is None:
        tool_registry = _make_fake_tool_registry()
    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        return SubagentRegistry.load(tool_registry)


# ---------------------------------------------------------------------------
# Local tool loading (tools/*.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_tool_module_loaded_and_accessible(tmp_path: Path) -> None:
    """A BaseTool-duck-typed object from tools/*.py is loaded into local_tools."""
    from atelier.subagents import _load_local_tools

    pack_dir = tmp_path / "my-agent"
    pack_dir.mkdir()
    _write_tool_module(
        pack_dir, "search",
        """
        class SearchTool:
            name = "search_web"
            def run(self, query: str) -> str:
                return query
        search_web = SearchTool()
        """
    )

    tools = _load_local_tools(pack_dir, "my-agent")

    assert "search_web" in tools
    assert tools["search_web"].name == "search_web"
    assert callable(tools["search_web"].run)


@pytest.mark.unit
def test_local_tool_no_tools_dir_returns_empty(tmp_path: Path) -> None:
    """_load_local_tools returns {} when no tools/ directory exists."""
    from atelier.subagents import _load_local_tools

    pack_dir = tmp_path / "no-tools"
    pack_dir.mkdir()

    result = _load_local_tools(pack_dir, "no-tools")

    assert result == {}


@pytest.mark.unit
def test_broken_tool_module_logged_and_skipped(tmp_path: Path, caplog) -> None:
    """A syntax-error module is logged as ERROR and skipped; others still load."""
    from atelier.subagents import _load_local_tools

    pack_dir = tmp_path / "mixed-agent"
    pack_dir.mkdir()

    _write_tool_module(pack_dir, "broken", "this is not python !!! @@@")
    _write_tool_module(
        pack_dir, "ok",
        """
        class OkTool:
            name = "ok_tool"
            def run(self, x): return x
        ok_tool = OkTool()
        """
    )

    with caplog.at_level(logging.ERROR):
        tools = _load_local_tools(pack_dir, "mixed-agent")

    assert "ok_tool" in tools
    assert any("broken" in r.message for r in caplog.records if r.levelno == logging.ERROR)


@pytest.mark.unit
def test_importlib_isolation_no_sys_modules_insertion(tmp_path: Path) -> None:
    """Loaded tool modules must NOT appear in sys.modules."""
    from atelier.subagents import _load_tools_from_module

    pack_dir = tmp_path / "isolated-agent"
    pack_dir.mkdir()
    py_file = _write_tool_module(
        pack_dir, "my_tools",
        """
        class MyTool:
            name = "my_tool"
            def run(self, x): return x
        my_tool = MyTool()
        """
    )

    synthetic_name = "relais_subagent_isolated-agent_my_tools"
    keys_before = set(sys.modules.keys())

    _load_tools_from_module(py_file, "isolated-agent")

    new_keys = set(sys.modules.keys()) - keys_before
    assert synthetic_name not in new_keys, (
        f"Module '{synthetic_name}' should not be inserted into sys.modules"
    )


@pytest.mark.unit
def test_tool_name_collision_across_modules_logs_warning(tmp_path: Path, caplog) -> None:
    """When two modules export the same tool name, a WARNING is logged."""
    from atelier.subagents import _load_local_tools

    pack_dir = tmp_path / "collision-agent"
    pack_dir.mkdir()

    # Both modules define a tool with name "shared_tool"
    _write_tool_module(
        pack_dir, "a_tools",
        """
        class ATool:
            name = "shared_tool"
            def run(self, x): return "a"
        shared_tool = ATool()
        """
    )
    _write_tool_module(
        pack_dir, "b_tools",
        """
        class BTool:
            name = "shared_tool"
            def run(self, x): return "b"
        shared_tool = BTool()
        """
    )

    with caplog.at_level(logging.WARNING):
        tools = _load_local_tools(pack_dir, "collision-agent")

    assert "shared_tool" in tools
    assert any(
        "shared_tool" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.unit
def test_private_attributes_not_collected_as_tools(tmp_path: Path) -> None:
    """Module-level attributes starting with _ are never collected as tools."""
    from atelier.subagents import _load_tools_from_module

    pack_dir = tmp_path / "priv-agent"
    pack_dir.mkdir()
    py_file = _write_tool_module(
        pack_dir, "priv",
        """
        class _PrivateTool:
            name = "_private"
            def run(self, x): return x
        _private = _PrivateTool()
        """
    )

    tools = _load_tools_from_module(py_file, "priv-agent")

    assert "_private" not in tools
    assert tools == {}


# ---------------------------------------------------------------------------
# local: tool tokens in specs_for_user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_specs_for_user_resolves_local_tool_token(tmp_path: Path) -> None:
    """specs_for_user resolves 'local:<name>' to the tool from the pack's tools/."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(
        subagents_dir, "tool-pack",
        yaml_extra={"tool_tokens": ["local:search_web"]},
    )
    _write_tool_module(
        pack_dir, "search",
        """
        class SearchTool:
            name = "search_web"
            def run(self, q): return q
        search_web = SearchTool()
        """
    )

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    tools = specs[0].get("tools", [])
    assert any(getattr(t, "name", None) == "search_web" for t in tools)


@pytest.mark.unit
def test_specs_for_user_unknown_local_tool_token_dropped(tmp_path: Path, caplog) -> None:
    """'local:nonexistent' is dropped with a WARNING; spec is still returned."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir, "missing-tool",
        yaml_extra={"tool_tokens": ["local:nonexistent"]},
    )

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
        caplog.at_level(logging.WARNING),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    assert "tools" not in specs[0]
    assert any("nonexistent" in r.message for r in caplog.records if r.levelno == logging.WARNING)


# ---------------------------------------------------------------------------
# local: skill tokens in specs_for_user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_specs_for_user_resolves_local_skill_token(tmp_path: Path) -> None:
    """specs_for_user resolves 'local:<name>' to the skill's absolute path."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(
        subagents_dir, "skill-pack",
        yaml_extra={"skill_tokens": ["local:my-skill"]},
    )
    skill_dir = _write_skill_dir(pack_dir, "my-skill")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    skills = specs[0].get("skills", [])
    assert len(skills) == 1
    # SkillsMiddleware expects the *parent* directory containing skill subdirs,
    # not the individual skill directory itself.
    assert str(skill_dir.parent.resolve()) == skills[0]


@pytest.mark.unit
def test_specs_for_user_skill_missing_dir_dropped(tmp_path: Path, caplog) -> None:
    """'local:missing-skill' is dropped with WARNING when directory absent."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir, "no-skill-dir",
        yaml_extra={"skill_tokens": ["local:missing-skill"]},
    )

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
        caplog.at_level(logging.WARNING),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    assert "skills" not in specs[0]
    assert any(
        "missing-skill" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_specs_for_user_multiple_skills_all_resolved(tmp_path: Path) -> None:
    """Multiple 'local:' skill tokens all resolve to their respective paths."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(
        subagents_dir, "multi-skill",
        yaml_extra={"skill_tokens": ["local:alpha", "local:beta"]},
    )
    alpha_dir = _write_skill_dir(pack_dir, "alpha")
    beta_dir = _write_skill_dir(pack_dir, "beta")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    skills = specs[0].get("skills", [])
    # Both alpha and beta share the same parent skills/ dir; SkillsMiddleware
    # receives the parent directory and discovers all skill subdirs inside it.
    assert len(skills) == 1
    assert str(alpha_dir.parent.resolve()) in skills


# ---------------------------------------------------------------------------
# Path traversal guard (via production path — specs_for_user)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_traversal_skill_token_dropped_by_specs_for_user(
    tmp_path: Path, caplog
) -> None:
    """A 'local:../../etc/passwd' skill token is dropped: the traversal name is not
    a key in the dict built from the filesystem, so it resolves to None via WARNING.
    """
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(
        subagents_dir, "traversal-agent",
        yaml_extra={"skill_tokens": ["local:../../etc/passwd"]},
    )
    # Create a valid skill so we know the skills/ dir exists
    _write_skill_dir(pack_dir, "real-skill")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
        caplog.at_level(logging.WARNING),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    # Traversal token must not appear in the skills list
    assert "skills" not in specs[0] or not any(
        "etc" in str(s) for s in specs[0].get("skills", [])
    )
    assert any(
        "../../etc/passwd" in r.message or "../../etc" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_empty_local_skill_token_dropped_by_specs_for_user(
    tmp_path: Path, caplog
) -> None:
    """'local:' with empty skill name is dropped with WARNING via specs_for_user."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(
        subagents_dir, "empty-skill-token",
        yaml_extra={"skill_tokens": ["local:"]},
    )

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
        caplog.at_level(logging.WARNING),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    specs = reg.specs_for_user({"allowed_subagents": ["*"]}, request_tools=[])

    assert len(specs) == 1
    assert "skills" not in specs[0]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Skill discovery at load time
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_discovers_skills_in_skills_dir(tmp_path: Path) -> None:
    """SubagentRegistry.load discovers all skill/ subdirectories automatically."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    pack_dir = _write_pack(subagents_dir, "auto-skill")
    _write_skill_dir(pack_dir, "skill-a")
    _write_skill_dir(pack_dir, "skill-b")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    # Check internal registry has both skills discovered
    internal_skills = reg._local_skills_by_subagent.get("auto-skill", {})
    assert "skill-a" in internal_skills
    assert "skill-b" in internal_skills


@pytest.mark.unit
def test_load_skills_not_discovered_without_skills_dir(tmp_path: Path) -> None:
    """_local_skills_by_subagent is empty when no skills/ dir exists."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    _write_pack(subagents_dir, "no-skill-pack")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    internal_skills = reg._local_skills_by_subagent.get("no-skill-pack", {})
    assert internal_skills == {}


# ---------------------------------------------------------------------------
# Pack isolation / error containment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_broken_pack_does_not_block_other_packs(tmp_path: Path, caplog) -> None:
    """A pack with missing required fields is skipped; other packs still load."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"

    # Broken pack — missing system_prompt
    broken_dir = subagents_dir / "broken-agent"
    broken_dir.mkdir(parents=True)
    (broken_dir / "subagent.yaml").write_text(yaml.dump({
        "name": "broken-agent",
        "description": "Missing system_prompt",
    }))

    # Valid pack
    _write_pack(subagents_dir, "ok-agent")

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    assert "ok-agent" in reg.all_names
    assert "broken-agent" not in reg.all_names
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.unit
def test_pack_dir_name_mismatch_skipped(tmp_path: Path, caplog) -> None:
    """Directory name != name field causes the pack to be skipped with ERROR."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"

    # Directory named "dir-a" but name: field says "other-name"
    mismatch_dir = subagents_dir / "dir-a"
    mismatch_dir.mkdir(parents=True)
    (mismatch_dir / "subagent.yaml").write_text(yaml.dump({
        "name": "other-name",
        "description": "Mismatch test",
        "system_prompt": "Test prompt.",
    }))

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        caplog.at_level(logging.ERROR),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    assert "dir-a" not in reg.all_names
    assert "other-name" not in reg.all_names
    assert any("dir-a" in r.message or "other-name" in r.message
               for r in caplog.records if r.levelno == logging.ERROR)


@pytest.mark.unit
def test_multiple_broken_tool_modules_others_still_load(tmp_path: Path, caplog) -> None:
    """Two broken tool modules are skipped; the third still loads."""
    from atelier.subagents import _load_local_tools

    pack_dir = tmp_path / "resilient-agent"
    pack_dir.mkdir()

    _write_tool_module(pack_dir, "bad1", "syntax error !!!")
    _write_tool_module(pack_dir, "bad2", "raise RuntimeError('boom')")
    _write_tool_module(
        pack_dir, "good",
        """
        class GoodTool:
            name = "good_tool"
            def run(self, x): return x
        good_tool = GoodTool()
        """
    )

    with caplog.at_level(logging.ERROR):
        tools = _load_local_tools(pack_dir, "resilient-agent")

    assert "good_tool" in tools
    error_messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_messages) >= 2  # both bad modules logged


# ---------------------------------------------------------------------------
# Flat YAML file ignored
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_flat_yaml_file_in_subagents_dir_ignored(tmp_path: Path) -> None:
    """A flat .yaml file (old format) directly in subagents/ is not loaded."""
    from atelier.subagents import SubagentRegistry

    subagents_dir = tmp_path / "config" / "atelier" / "subagents"
    subagents_dir.mkdir(parents=True)

    # Old flat format: subagents/my-agent.yaml (not a directory)
    (subagents_dir / "my-agent.yaml").write_text(yaml.dump({
        "name": "my-agent",
        "description": "Old flat format",
        "system_prompt": "Old prompt.",
    }))

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
        patch("atelier.subagents.resolve_bundles_dir", return_value=tmp_path / "_nonexistent_bundles_"),
    ):
        reg = SubagentRegistry.load(_make_fake_tool_registry())

    assert "my-agent" not in reg.all_names
    assert len(reg._specs) == 0
