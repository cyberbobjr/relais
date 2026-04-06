"""SkillPatcher — atomic write of improved SKILL.md files.

Write protocol:
  1. Write new content to ``{skill_path}.pending`` (never overwrites the live file).
  2. Validate the pending file is readable and non-empty.
  3. Copy the current file to ``{skill_path}.bak`` (filesystem-level rollback source).
  4. Atomically rename ``.pending`` → live file via ``os.replace()`` (POSIX atomic).

On any failure between steps 1-4, the ``.pending`` file is cleaned up and the
original is left untouched.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from forgeron.models import SkillPatch

logger = logging.getLogger(__name__)


class SkillPatcher:
    """Apply and roll back SKILL.md patches atomically.

    Args:
        skills_dir: Root directory containing skill subdirectories.
    """

    def __init__(self, skills_dir: Path | None) -> None:
        self._skills_dir = skills_dir

    def apply(self, skill_path: Path, patch: SkillPatch) -> None:
        """Write ``patch.patched_content`` to *skill_path* atomically.

        Steps:
          1. Write ``.pending`` file.
          2. Validate it (readable, non-empty).
          3. Back up original to ``.bak``.
          4. Atomic rename ``.pending`` → skill_path.

        Args:
            skill_path: Absolute path to the SKILL.md file.
            patch: ``SkillPatch`` record carrying ``patched_content``.

        Raises:
            ValueError: If ``patched_content`` is empty.
            OSError: If the filesystem operations fail.
        """
        if not patch.patched_content.strip():
            raise ValueError(
                f"Refusing to apply empty patch {patch.id} for skill "
                f"'{patch.skill_name}'"
            )

        pending_path = skill_path.with_suffix(".md.pending")
        bak_path = skill_path.with_suffix(".md.bak")

        try:
            # Step 1: write to .pending
            pending_path.write_text(patch.patched_content, encoding="utf-8")
            logger.debug("Wrote pending patch to %s", pending_path)

            # Step 2: validate
            content = pending_path.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError(
                    f"Pending file {pending_path} is empty after write — aborting."
                )

            # Step 3: backup original
            original = skill_path.read_text(encoding="utf-8")
            bak_path.write_text(original, encoding="utf-8")
            logger.debug("Backed up original to %s", bak_path)

            # Step 4: atomic rename
            os.replace(pending_path, skill_path)
            logger.info(
                "Patch %s applied atomically to %s", patch.id, skill_path
            )

        except Exception:
            # Clean up the .pending file so we don't leave rubbish on disk.
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def rollback(self, skill_path: Path, patch: SkillPatch) -> None:
        """Restore *skill_path* to its pre-patch state.

        Prefers the ``.bak`` file if it exists; falls back to
        ``patch.original_content`` from the database.

        Args:
            skill_path: Absolute path to the SKILL.md file.
            patch: ``SkillPatch`` whose ``original_content`` is the rollback target.

        Raises:
            ValueError: If neither the ``.bak`` file nor ``original_content``
                is available.
        """
        bak_path = skill_path.with_suffix(".md.bak")

        if bak_path.exists():
            original = bak_path.read_text(encoding="utf-8")
            logger.info(
                "Rolling back patch %s for skill '%s' from .bak file",
                patch.id,
                patch.skill_name,
            )
        elif patch.original_content:
            original = patch.original_content
            logger.info(
                "Rolling back patch %s for skill '%s' from DB original_content",
                patch.id,
                patch.skill_name,
            )
        else:
            raise ValueError(
                f"Cannot roll back patch {patch.id} for skill '{patch.skill_name}': "
                "no .bak file and no original_content in DB."
            )

        skill_path.write_text(original, encoding="utf-8")
        logger.info(
            "Rollback complete for patch %s — %s restored", patch.id, skill_path
        )
