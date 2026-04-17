"""write_skill tool — create or overwrite a SKILL.md file in the skills directory.

This tool is registered in the skill-designer subagent pack.  It creates the
skill directory if it does not exist and writes the SKILL.md content provided
by the subagent.

Security: the skill name is validated against the agentskills.io name rules
before any file-system operation.  Path traversal is blocked by rejecting
names that contain slashes, dots, or other special characters.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import BaseTool

from common.config_loader import resolve_skills_dir

logger = logging.getLogger(__name__)

# agentskills.io name rules: 1-64 chars, lowercase a-z, digits, hyphens.
# Must not start or end with hyphen, no consecutive hyphens.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_NO_CONSECUTIVE_HYPHENS = re.compile(r"--")


def _validate_skill_name(name: str) -> str | None:
    """Return None if valid, or an error message string if invalid.

    Args:
        name: Proposed skill directory name.

    Returns:
        None when the name is valid; an error string otherwise.
    """
    if not name:
        return "Skill name must not be empty."
    if len(name) > 64:
        return f"Skill name too long: {len(name)} chars (max 64)."
    if not _NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}': must be lowercase letters, digits, "
            "and hyphens; must not start or end with a hyphen."
        )
    if _NO_CONSECUTIVE_HYPHENS.search(name):
        return f"Invalid skill name '{name}': must not contain consecutive hyphens (--)."
    return None


class WriteSkillTool(BaseTool):
    """Create or overwrite a SKILL.md file under the resolved skills directory.

    The tool validates the skill name, creates the skill directory if needed,
    and writes the provided content.  It refuses to overwrite an existing
    SKILL.md unless ``overwrite=True`` is passed.

    Attributes:
        name: Tool name as exposed to the LLM.
        description: Tool description used in the agent's tool listing.
    """

    name: str = "write_skill"
    description: str = (
        "Create a new SKILL.md file in the skills directory. "
        "Provide the skill_name (directory name, lowercase-hyphens) and the "
        "full SKILL.md content as a string. Pass overwrite=true to replace an "
        "existing skill. Returns the absolute path of the written file, or an "
        "error message."
    )

    def _run(
        self,
        skill_name: str,
        content: str,
        overwrite: bool = False,
    ) -> str:
        """Write the SKILL.md file synchronously.

        Args:
            skill_name: Name of the skill directory (e.g. ``"send-email"``).
            content: Full content of the SKILL.md file.
            overwrite: If ``True``, overwrite an existing SKILL.md.

        Returns:
            Absolute path of the written file on success, or an error message.
        """
        # Validate name
        error = _validate_skill_name(skill_name)
        if error:
            logger.warning("WriteSkillTool: validation failed — %s", error)
            return f"ERROR: {error}"

        skills_dir = resolve_skills_dir()
        skill_dir = skills_dir / skill_name
        skill_path = skill_dir / "SKILL.md"

        if skill_path.exists() and not overwrite:
            msg = (
                f"SKILL.md already exists at {skill_path}. "
                "Pass overwrite=true to replace it."
            )
            logger.warning("WriteSkillTool: %s", msg)
            return f"ERROR: {msg}"

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(content, encoding="utf-8")
            logger.info(
                "WriteSkillTool: wrote skill='%s' path=%s overwrite=%s",
                skill_name,
                skill_path,
                overwrite,
            )
            return str(skill_path)
        except OSError as exc:
            logger.error(
                "WriteSkillTool: I/O error writing '%s': %s",
                skill_path,
                exc,
            )
            return f"ERROR: {exc}"

    async def _arun(
        self,
        skill_name: str,
        content: str,
        overwrite: bool = False,
    ) -> str:
        """Async wrapper — delegates to the synchronous implementation.

        Args:
            skill_name: Name of the skill directory.
            content: Full content of the SKILL.md file.
            overwrite: If ``True``, overwrite an existing SKILL.md.

        Returns:
            Absolute path of the written file on success, or an error message.
        """
        return self._run(skill_name=skill_name, content=content, overwrite=overwrite)


# Module-level instance discovered by the SubagentRegistry tool loader.
write_skill = WriteSkillTool()
