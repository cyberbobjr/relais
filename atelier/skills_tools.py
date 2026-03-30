"""Skills InternalTool factory for the Atelier brick.

A "skill" is a knowledge document stored on disk that the LLM can query at
runtime.  Each skill lives in its own subdirectory under ``skills/`` and
contains a ``SKILL.md`` file with structured guidance (prompts, patterns,
code examples, etc.).

Example layout::

    skills/
      python-patterns/
        SKILL.md          ← the skill content
      api-design/
        SKILL.md

Exposes two InternalTools to the agentic loop:
- ``list_skills`` — catalogue of available skills (name + first non-empty line)
- ``read_skill``  — full content of a named SKILL.md file
"""

from __future__ import annotations

import logging
from pathlib import Path

from atelier.internal_tool import InternalTool

logger = logging.getLogger(__name__)


def make_skills_tools(skills_dir: Path) -> list[InternalTool]:
    """Return InternalTool instances for listing and reading skills.

    Both tools operate on ``skills_dir`` (resolved at call time via closure).
    If ``skills_dir`` does not exist the tools still work — ``list_skills``
    returns an empty catalogue and ``read_skill`` returns an error string.

    Args:
        skills_dir: Directory to scan for ``SKILL.md`` files recursively.

    Returns:
        List of two InternalTool instances: ``list_skills`` and ``read_skill``.
    """

    def _list_skills() -> str:
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
        # Guard against path traversal: model-supplied skill_name must be a
        # plain directory name with no separators or parent-dir references.
        if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
            return f"Error: invalid skill name '{skill_name}'."
        if not skills_dir.exists():
            return f"Error: skills directory '{skills_dir}' does not exist."
        candidate = (skills_dir / skill_name / "SKILL.md").resolve()
        skills_dir_resolved = skills_dir.resolve()
        # Ensure the resolved path stays inside skills_dir (catches symlink attacks).
        if not str(candidate).startswith(str(skills_dir_resolved) + "/"):
            return f"Error: skill '{skill_name}' not found."
        if not candidate.exists():
            return f"Error: skill '{skill_name}' not found in {skills_dir}."
        return candidate.read_text(encoding="utf-8")

    list_tool = InternalTool(
        name="list_skills",
        description=(
            "List all available skills with a one-line summary of each. "
            "Call this first to discover what skills exist before reading one."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_list_skills,
    )

    read_tool = InternalTool(
        name="read_skill",
        description=(
            "Read the full content of a skill by its name. "
            "Use list_skills first to find the exact skill name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Exact name of the skill directory (e.g. 'python-patterns').",
                }
            },
            "required": ["skill_name"],
        },
        handler=_read_skill,
    )

    return [list_tool, read_tool]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_nonempty_line(path: Path) -> str:
    """Return the first non-empty, non-whitespace line of a file.

    Reads line-by-line so that large SKILL.md files are not loaded into
    memory just to get the title.

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
