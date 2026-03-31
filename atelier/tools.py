"""LangChain @tool replacements for the Atelier brick's internal tools.

Replaces ``atelier/internal_tool.py`` + ``atelier/skills_tools.py``.
Tool schemas are derived automatically from type hints and docstrings
via LangChain's ``@tool`` decorator (no manual JSON Schema required).

A "skill" is a knowledge document stored on disk that the LLM can query at
runtime.  Each skill lives in its own subdirectory under ``skills/`` and
contains a ``SKILL.md`` file with structured guidance.

Example layout::

    skills/
      python-patterns/
        SKILL.md
      api-design/
        SKILL.md

Exposes two tools to the agentic loop:
- ``list_skills`` — catalogue of available skills (name + first non-empty line)
- ``read_skill``  — full content of a named SKILL.md file
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def make_skills_tools(skills_dir: Path) -> list[StructuredTool]:
    """Return LangChain StructuredTool instances for listing and reading skills.

    Both tools operate on ``skills_dir`` (captured at call time via closure).
    If ``skills_dir`` does not exist the tools still work — ``list_skills``
    returns an empty catalogue and ``read_skill`` returns an error string.

    Args:
        skills_dir: Directory to scan for ``SKILL.md`` files recursively.

    Returns:
        List of two StructuredTool instances: ``list_skills`` and ``read_skill``.
    """

    def _list_skills() -> str:
        """List all available skills with a one-line summary of each.

        Call this first to discover what skills exist before reading one.

        Returns:
            Newline-separated list of skill names with a one-line summary,
            or a message indicating no skills are available.
        """
        if not skills_dir.exists():
            return "No skills directory found."
        entries: list[str] = []
        for skill_file in sorted(skills_dir.rglob("SKILL.md")):
            name = skill_file.parent.name
            first_line = _first_nonempty_line(skill_file)
            entries.append(f"- {name}: {first_line}")
        if not entries:
            return "No skills found."
        return "\n".join(entries)

    def _read_skill(skill_name: str) -> str:
        """Read the full content of a skill by its name.

        Use list_skills first to find the exact skill name.

        Args:
            skill_name: Exact name of the skill directory (e.g. 'python-patterns').

        Returns:
            Full text content of the SKILL.md file, or an error string when
            the skill is not found or the name is invalid.
        """
        # Guard against path traversal: model-supplied skill_name must be a
        # plain directory name with no separators or parent-dir references.
        if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
            return f"Error: invalid skill name '{skill_name}'."
        if not skills_dir.exists():
            return f"Error: skills directory '{skills_dir}' does not exist."
        skills_dir_resolved = skills_dir.resolve()
        # Search recursively so skills can live under subdirectories like
        # auto/ or manual/ (e.g. skills/auto/test-hello/SKILL.md).
        for skill_file in skills_dir_resolved.rglob("SKILL.md"):
            if skill_file.parent.name == skill_name:
                # Ensure resolved path stays inside skills_dir (symlink guard).
                if not str(skill_file.resolve()).startswith(str(skills_dir_resolved) + "/"):
                    continue
                return skill_file.read_text(encoding="utf-8")
        return f"Error: skill '{skill_name}' not found in {skills_dir}."

    list_tool = StructuredTool.from_function(
        func=_list_skills,
        name="list_skills",
        description=(
            "List all available skills with a one-line summary of each. "
            "Call this first to discover what skills exist before reading one."
        ),
    )

    read_tool = StructuredTool.from_function(
        func=_read_skill,
        name="read_skill",
        description=(
            "Read the full content of a skill by its name. "
            "Use list_skills first to find the exact skill name."
        ),
    )

    return [list_tool, read_tool]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_nonempty_line(path: Path) -> str:
    """Return the first non-empty, non-whitespace line of a file.

    Args:
        path: Path to the text file to read.

    Returns:
        First non-empty line stripped of leading/trailing whitespace, or an
        empty string when the file contains only blank lines.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    return stripped
    except OSError as exc:
        logger.warning("Could not read skill file %s: %s", path, exc)
    return ""
