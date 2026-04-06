"""SkillValidator — post-patch regression monitor.

After a patch is applied, each incoming trace for the same skill is checked
against the rollback threshold.  If the error rate over the post-patch window
exceeds ``config.rollback_error_rate_threshold``, the patch is automatically
rolled back.

Rollback is intentionally conservative:
  - We wait until at least ``config.rollback_window_traces`` post-patch traces
    have accumulated before evaluating.
  - Only traces tagged with the current ``patch.id`` are used.  Traces from
    before the patch (``patch_id IS NULL`` or a different patch ID) are
    excluded so the comparison is fair.
"""

from __future__ import annotations

import logging
from pathlib import Path

from forgeron.config import ForgeonConfig
from forgeron.models import SkillPatch
from forgeron.patch_store import SkillPatchStore
from forgeron.patcher import SkillPatcher
from forgeron.trace_store import SkillTraceStore

logger = logging.getLogger(__name__)


class SkillValidator:
    """Monitor post-patch traces and trigger rollback on regression.

    Args:
        trace_store: Store used to retrieve post-patch traces.
        patch_store: Store used to mark patches as rolled back.
    """

    def __init__(
        self,
        trace_store: SkillTraceStore,
        patch_store: SkillPatchStore,
    ) -> None:
        self._trace_store = trace_store
        self._patch_store = patch_store

    async def check_and_rollback_if_needed(
        self,
        skill_name: str,
        skill_path: Path,
        patch: SkillPatch,
        config: ForgeonConfig,
    ) -> bool:
        """Evaluate post-patch error rate and roll back if regression is detected.

        Only evaluates when at least ``config.rollback_window_traces`` traces
        tagged with ``patch.id`` have been accumulated.

        Args:
            skill_name: Skill being monitored.
            skill_path: Absolute path to the SKILL.md file (for rollback).
            patch: The currently applied ``SkillPatch``.
            config: Forgeron configuration with rollback thresholds.

        Returns:
            ``True`` if a rollback was performed, ``False`` otherwise.
        """
        post_traces = await self._trace_store.get_traces(
            skill_name,
            since_patch_id=patch.id,
            limit=config.rollback_window_traces,
        )

        if len(post_traces) < config.rollback_window_traces:
            logger.debug(
                "skill '%s' patch %s: only %d/%d post-patch traces — waiting",
                skill_name,
                patch.id,
                len(post_traces),
                config.rollback_window_traces,
            )
            return False

        total_calls = sum(t.tool_call_count for t in post_traces)
        if total_calls == 0:
            return False

        post_error_rate = sum(t.tool_error_count for t in post_traces) / total_calls

        # Update the stored post-patch error rate for observability.
        patch.post_patch_error_rate = post_error_rate
        await self._patch_store.save(patch)

        if post_error_rate <= config.rollback_error_rate_threshold:
            logger.debug(
                "skill '%s' patch %s: post-patch error rate %.0f%% ≤ threshold %.0f%% — OK",
                skill_name,
                patch.id,
                post_error_rate * 100,
                config.rollback_error_rate_threshold * 100,
            )
            return False

        # Regression detected — roll back.
        logger.warning(
            "skill '%s' patch %s REGRESSION: post-patch error rate %.0f%% > "
            "threshold %.0f%% — rolling back",
            skill_name,
            patch.id,
            post_error_rate * 100,
            config.rollback_error_rate_threshold * 100,
        )

        patcher = SkillPatcher(skills_dir=config.skills_dir)
        try:
            patcher.rollback(skill_path, patch)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Rollback failed for patch %s skill '%s': %s",
                patch.id,
                skill_name,
                exc,
            )
            return False

        await self._patch_store.mark_rolled_back(patch)
        logger.info(
            "Rollback complete for patch %s skill '%s' "
            "(pre_error_rate=%.0f%% post_error_rate=%.0f%%)",
            patch.id,
            skill_name,
            patch.pre_patch_error_rate * 100,
            post_error_rate * 100,
        )
        return True
