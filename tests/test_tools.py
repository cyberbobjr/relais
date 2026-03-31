"""Unit tests for atelier.tools — LangChain @tool replacements for InternalTool.

These tests validate list_skills and read_skill behaviour, including:
- correct enumeration of skills from a directory tree
- path-traversal protection on read_skill
- behaviour when the skills directory does not exist
- that the functions are LangChain StructuredTool instances
"""

from __future__ import annotations

import pytest

from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def skills_dir(tmp_path: Path) -> Path:
    """Create a small skills tree for testing."""
    # skills/python-patterns/SKILL.md
    (tmp_path / "python-patterns").mkdir()
    (tmp_path / "python-patterns" / "SKILL.md").write_text(
        "# Python Patterns\nThis skill covers Python idioms.\n",
        encoding="utf-8",
    )
    # skills/api-design/SKILL.md
    (tmp_path / "api-design").mkdir()
    (tmp_path / "api-design" / "SKILL.md").write_text(
        "# API Design\nBest practices for REST APIs.\n",
        encoding="utf-8",
    )
    # skills/auto/sub-skill/SKILL.md — nested
    (tmp_path / "auto" / "sub-skill").mkdir(parents=True)
    (tmp_path / "auto" / "sub-skill" / "SKILL.md").write_text(
        "# Sub Skill\nA nested skill.\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Imports (will fail RED until atelier/tools.py exists)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tools_module_imports() -> None:
    """atelier.tools must be importable."""
    from atelier import tools  # noqa: F401


@pytest.mark.unit
def test_make_skills_tools_returns_two_tools(skills_dir: Path) -> None:
    """make_skills_tools must return exactly two tool objects."""
    from atelier.tools import make_skills_tools

    tools = make_skills_tools(skills_dir)
    assert len(tools) == 2


@pytest.mark.unit
def test_tools_are_langchain_structured_tools(skills_dir: Path) -> None:
    """Both returned tools must be LangChain StructuredTool instances."""
    from langchain_core.tools import StructuredTool

    from atelier.tools import make_skills_tools

    tools = make_skills_tools(skills_dir)
    for tool in tools:
        assert isinstance(tool, StructuredTool), (
            f"Expected StructuredTool, got {type(tool)}"
        )


@pytest.mark.unit
def test_list_skills_tool_name(skills_dir: Path) -> None:
    """The first tool must be named 'list_skills'."""
    from atelier.tools import make_skills_tools

    tools = make_skills_tools(skills_dir)
    assert tools[0].name == "list_skills"


@pytest.mark.unit
def test_read_skill_tool_name(skills_dir: Path) -> None:
    """The second tool must be named 'read_skill'."""
    from atelier.tools import make_skills_tools

    tools = make_skills_tools(skills_dir)
    assert tools[1].name == "read_skill"


@pytest.mark.unit
def test_list_skills_returns_all_skills(skills_dir: Path) -> None:
    """list_skills must enumerate all SKILL.md files in the directory tree."""
    from atelier.tools import make_skills_tools

    list_tool = make_skills_tools(skills_dir)[0]
    result: str = list_tool.invoke({})
    assert "python-patterns" in result
    assert "api-design" in result
    assert "sub-skill" in result


@pytest.mark.unit
def test_list_skills_includes_first_line_summary(skills_dir: Path) -> None:
    """list_skills must include the first non-empty line of each SKILL.md."""
    from atelier.tools import make_skills_tools

    list_tool = make_skills_tools(skills_dir)[0]
    result: str = list_tool.invoke({})
    assert "# Python Patterns" in result
    assert "# API Design" in result


@pytest.mark.unit
def test_list_skills_empty_dir(tmp_path: Path) -> None:
    """list_skills returns a 'no skills found' message when dir is empty."""
    from atelier.tools import make_skills_tools

    list_tool = make_skills_tools(tmp_path)[0]
    result: str = list_tool.invoke({})
    assert "No skills" in result


@pytest.mark.unit
def test_list_skills_missing_dir(tmp_path: Path) -> None:
    """list_skills returns graceful message when skills dir does not exist."""
    from atelier.tools import make_skills_tools

    missing = tmp_path / "nonexistent"
    list_tool = make_skills_tools(missing)[0]
    result: str = list_tool.invoke({})
    assert "No skills" in result


@pytest.mark.unit
def test_read_skill_returns_content(skills_dir: Path) -> None:
    """read_skill must return full SKILL.md content for a valid skill name."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "python-patterns"})
    assert "# Python Patterns" in result
    assert "Python idioms" in result


@pytest.mark.unit
def test_read_skill_finds_nested_skill(skills_dir: Path) -> None:
    """read_skill must find skills nested under subdirectories."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "sub-skill"})
    assert "# Sub Skill" in result


@pytest.mark.unit
def test_read_skill_unknown_name(skills_dir: Path) -> None:
    """read_skill returns an error string for an unknown skill name."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "does-not-exist"})
    assert "Error" in result


@pytest.mark.unit
def test_read_skill_rejects_slash(skills_dir: Path) -> None:
    """read_skill must reject names containing '/' (path traversal guard)."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "../../etc/passwd"})
    assert "Error" in result
    assert "invalid" in result.lower()


@pytest.mark.unit
def test_read_skill_rejects_backslash(skills_dir: Path) -> None:
    """read_skill must reject names containing '\\'."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "foo\\bar"})
    assert "Error" in result
    assert "invalid" in result.lower()


@pytest.mark.unit
def test_read_skill_rejects_double_dot(skills_dir: Path) -> None:
    """read_skill must reject names containing '..'."""
    from atelier.tools import make_skills_tools

    read_tool = make_skills_tools(skills_dir)[1]
    result: str = read_tool.invoke({"skill_name": "..hidden"})
    assert "Error" in result
    assert "invalid" in result.lower()


@pytest.mark.unit
def test_read_skill_missing_dir(tmp_path: Path) -> None:
    """read_skill returns an error when skills dir does not exist."""
    from atelier.tools import make_skills_tools

    missing = tmp_path / "nonexistent"
    read_tool = make_skills_tools(missing)[1]
    result: str = read_tool.invoke({"skill_name": "python-patterns"})
    assert "Error" in result
