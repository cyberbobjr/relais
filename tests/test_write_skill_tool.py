"""Tests for WriteSkillTool — atelier/subagents/skill-designer/tools/write_skill.py.

Covers:
- Valid skill names accepted
- Invalid skill names rejected (empty, too long, bad chars, consecutive hyphens,
  leading/trailing hyphens)
- Existing SKILL.md blocks write unless overwrite=True
- File is written with correct content
- OSError is captured and returned as error string
- _arun delegates to _run
"""

from __future__ import annotations

import importlib.util
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _load_write_skill_module():
    """Load write_skill.py via importlib (directory name has hyphen, not importable normally)."""
    module_path = (
        Path(__file__).parent.parent
        / "atelier" / "subagents" / "skill-designer" / "tools" / "write_skill.py"
    )
    spec = importlib.util.spec_from_file_location("write_skill", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ws_module = _load_write_skill_module()
WriteSkillTool = _ws_module.WriteSkillTool
_validate_skill_name = _ws_module._validate_skill_name


# ---------------------------------------------------------------------------
# _validate_skill_name
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", [
    "a",
    "ab",
    "send-email",
    "my-skill-v2",
    "skill123",
    "a" * 64,
])
def test_validate_skill_name_valid(name: str) -> None:
    assert _validate_skill_name(name) is None


@pytest.mark.unit
@pytest.mark.parametrize("name,reason", [
    ("", "empty"),
    ("a" * 65, "too long"),
    ("-bad", "leading hyphen"),
    ("bad-", "trailing hyphen"),
    ("bad--name", "consecutive hyphens"),
    ("Bad-Name", "uppercase"),
    ("has space", "space"),
    ("has/slash", "slash"),
    ("has.dot", "dot"),
])
def test_validate_skill_name_invalid(name: str, reason: str) -> None:
    result = _validate_skill_name(name)
    assert result is not None, f"Expected error for '{name}' ({reason})"


# ---------------------------------------------------------------------------
# WriteSkillTool._run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_skill_creates_file(tmp_path: Path) -> None:
    """Happy path: creates skill directory and SKILL.md."""
    tool = WriteSkillTool()
    content = "# my-skill\n\nOverview.\n"

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        result = tool._run(skill_name="my-skill", content=content)

    expected_path = tmp_path / "my-skill" / "SKILL.md"
    assert result == str(expected_path)
    assert expected_path.exists()
    assert expected_path.read_text(encoding="utf-8") == content


@pytest.mark.unit
def test_write_skill_rejects_invalid_name(tmp_path: Path) -> None:
    """Invalid skill name returns ERROR string without touching filesystem."""
    tool = WriteSkillTool()

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        result = tool._run(skill_name="Bad Name!", content="# content")

    assert result.startswith("ERROR:")
    assert not (tmp_path / "Bad Name!").exists()


@pytest.mark.unit
def test_write_skill_blocks_overwrite_by_default(tmp_path: Path) -> None:
    """Second write without overwrite=True returns ERROR."""
    tool = WriteSkillTool()
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("old", encoding="utf-8")

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        result = tool._run(skill_name="my-skill", content="new content")

    assert result.startswith("ERROR:")
    # Original file must be untouched
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "old"


@pytest.mark.unit
def test_write_skill_overwrite_flag_replaces_file(tmp_path: Path) -> None:
    """overwrite=True replaces an existing SKILL.md."""
    tool = WriteSkillTool()
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("old", encoding="utf-8")

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        result = tool._run(skill_name="my-skill", content="new content", overwrite=True)

    assert "ERROR" not in result
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "new content"


@pytest.mark.unit
def test_write_skill_captures_oserror(tmp_path: Path) -> None:
    """OSError during write is returned as ERROR string."""
    tool = WriteSkillTool()

    with (
        patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path),
        patch("pathlib.Path.write_text", side_effect=OSError("disk full")),
    ):
        result = tool._run(skill_name="my-skill", content="# content")

    assert result.startswith("ERROR:")
    assert "disk full" in result


# ---------------------------------------------------------------------------
# WriteSkillTool._arun
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_arun_delegates_to_run(tmp_path: Path) -> None:
    """_arun() must return the same result as _run()."""
    tool = WriteSkillTool()
    content = "# async-skill\n"

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        sync_result = tool._run(skill_name="async-skill", content=content)

    # Delete the file so we can write again
    (tmp_path / "async-skill" / "SKILL.md").unlink()

    with patch.object(_ws_module, "resolve_skills_dir", return_value=tmp_path):
        async_result = await tool._arun(skill_name="async-skill", content=content)

    assert async_result == sync_result
