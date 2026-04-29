"""write_skill tool — create or overwrite a SKILL.md file in the skills directory.

This tool is registered in the skill_designer subagent pack.  It creates the
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

from common.config_loader import resolve_bundles_dir, resolve_skills_dir

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
        skill_path: Optional[str] = None,
    ) -> str:
        """Write the SKILL.md file synchronously.

        Args:
            skill_name: Name of the skill directory (e.g. ``"send-email"``).
            content: Full content of the SKILL.md file.
            overwrite: If ``True``, overwrite an existing SKILL.md.
            skill_path: Absolute path to an existing skill directory override
                (e.g. for bundle-installed skills).  When provided,
                ``resolve_skills_dir()`` is not called.

        Returns:
            Absolute path of the written file on success, or an error message.
        """
        # Validate name
        error = _validate_skill_name(skill_name)
        if error:
            logger.warning("WriteSkillTool: validation failed — %s", error)
            return f"ERROR: {error}"

        if skill_path is not None:
            resolved = Path(skill_path).expanduser().resolve()
            skills_root = resolve_skills_dir().resolve()
            bundles_root = resolve_bundles_dir().resolve()
            allowed_roots = (skills_root, bundles_root)
            if not any(resolved == r or resolved.is_relative_to(r) for r in allowed_roots):
                msg = (
                    f"skill_path '{skill_path}' is outside allowed directories "
                    f"(skills or bundles). Write refused."
                )
                logger.warning("WriteSkillTool: %s", msg)
                return f"ERROR: {msg}"
            # Depth check: skills must be direct children of the skills root or of
            # <bundles_root>/<bundle>/skills/ — nested subdirectories are never loaded
            # by ToolPolicy (iterdir() is one level only).
            is_local = resolved.parent == skills_root
            is_bundle = (
                resolved.parent.name == "skills"
                and resolved.parent.parent.parent == bundles_root
            )
            if not (is_local or is_bundle):
                msg = (
                    f"skill_path '{skill_path}' must be a direct child of the skills "
                    "directory or of a bundle's skills/ directory. "
                    "Nested skill directories are not supported (ToolPolicy only scans one level)."
                )
                logger.warning("WriteSkillTool: %s", msg)
                return f"ERROR: {msg}"
            skill_dir = resolved
        else:
            skills_dir = resolve_skills_dir()
            skill_dir = skills_dir / skill_name
        skill_path_file = skill_dir / "SKILL.md"

        if skill_path_file.exists() and not overwrite:
            msg = (
                f"SKILL.md already exists at {skill_path_file}. "
                "Pass overwrite=true to replace it."
            )
            logger.warning("WriteSkillTool: %s", msg)
            return f"ERROR: {msg}"

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path_file.write_text(content, encoding="utf-8")
            logger.info(
                "WriteSkillTool: wrote skill='%s' path=%s overwrite=%s",
                skill_name,
                skill_path_file,
                overwrite,
            )
            return str(skill_path_file)
        except OSError as exc:
            logger.error(
                "WriteSkillTool: I/O error writing '%s': %s",
                skill_path_file,
                exc,
            )
            return f"ERROR: {exc}"

    async def _arun(
        self,
        skill_name: str,
        content: str,
        overwrite: bool = False,
        skill_path: Optional[str] = None,
    ) -> str:
        """Async wrapper — delegates to the synchronous implementation.

        Args:
            skill_name: Name of the skill directory.
            content: Full content of the SKILL.md file.
            overwrite: If ``True``, overwrite an existing SKILL.md.
            skill_path: Absolute path to an existing skill directory override.

        Returns:
            Absolute path of the written file on success, or an error message.
        """
        return self._run(
            skill_name=skill_name,
            content=content,
            overwrite=overwrite,
            skill_path=skill_path,
        )


# Module-level instance discovered by the SubagentRegistry tool loader.
write_skill = WriteSkillTool()
