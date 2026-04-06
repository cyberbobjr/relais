"""Tests for SkillValidator — regression detection and rollback (Étape 3)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from forgeron.config import ForgeonConfig
from forgeron.models import SkillPatch, SkillTrace
from forgeron.patch_store import SkillPatchStore
from forgeron.trace_store import SkillTraceStore
from forgeron.validator import SkillValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def stores(tmp_path: Path):
    """In-memory trace + patch stores backed by a temp SQLite file."""
    db = tmp_path / "test.db"
    ts = SkillTraceStore(db_path=db)
    ps = SkillPatchStore(db_path=db)
    await ts._create_tables()
    yield ts, ps
    await ts.close()
    await ps.close()


def _cfg(rollback_threshold: float = 0.2, window: int = 3) -> ForgeonConfig:
    return ForgeonConfig(
        min_traces_before_analysis=5,
        min_error_rate=0.3,
        rollback_error_rate_threshold=rollback_threshold,
        rollback_window_traces=window,
        patch_mode=True,
        annotation_mode=False,
        skills_dir=None,
    )


def _patch_record(skill_name: str, patch_id: str | None = None) -> SkillPatch:
    p = SkillPatch(
        skill_name=skill_name,
        original_content="# Old",
        patched_content="# New",
        diff="",
        rationale="test",
        trigger_correlation_id="corr-1",
        pre_patch_error_rate=0.5,
        status="applied",
    )
    if patch_id is not None:
        p.id = patch_id
    return p


def _trace(skill: str, calls: int, errors: int, patch_id: str) -> SkillTrace:
    return SkillTrace(
        skill_name=skill,
        correlation_id="c",
        tool_call_count=calls,
        tool_error_count=errors,
        messages_raw=json.dumps([]),
        patch_id=patch_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_rollback_when_not_enough_post_traces(
    stores, tmp_path: Path
) -> None:
    ts, ps = stores
    patch = _patch_record("my-skill")

    # Only 2 post-patch traces, window requires 3
    for _ in range(2):
        await ts.add_trace(_trace("my-skill", calls=1, errors=1, patch_id=patch.id))

    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# New", encoding="utf-8")

    validator = SkillValidator(ts, ps)
    result = await validator.check_and_rollback_if_needed(
        "my-skill", skill_file, patch, _cfg(window=3)
    )
    assert result is False
    assert skill_file.read_text() == "# New"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_rollback_when_error_rate_within_threshold(
    stores, tmp_path: Path
) -> None:
    ts, ps = stores
    patch = _patch_record("my-skill")
    await ps.save(patch)

    # 3 traces with 10% error rate — below 20% threshold
    for _ in range(3):
        await ts.add_trace(_trace("my-skill", calls=10, errors=1, patch_id=patch.id))

    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# New", encoding="utf-8")

    validator = SkillValidator(ts, ps)
    result = await validator.check_and_rollback_if_needed(
        "my-skill", skill_file, patch, _cfg(rollback_threshold=0.2, window=3)
    )
    assert result is False
    assert skill_file.read_text() == "# New"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rollback_triggered_on_regression(stores, tmp_path: Path) -> None:
    ts, ps = stores
    patch = _patch_record("my-skill")
    patch.original_content = "# Original"
    await ps.save(patch)

    # 3 traces with 50% error rate — above 20% threshold
    for _ in range(3):
        await ts.add_trace(_trace("my-skill", calls=2, errors=1, patch_id=patch.id))

    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# New", encoding="utf-8")

    validator = SkillValidator(ts, ps)
    result = await validator.check_and_rollback_if_needed(
        "my-skill", skill_file, patch, _cfg(rollback_threshold=0.2, window=3)
    )
    assert result is True
    # Skill file must be restored to original content
    assert skill_file.read_text() == "# Original"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_patch_marked_rolled_back_after_regression(
    stores, tmp_path: Path
) -> None:
    ts, ps = stores
    patch = _patch_record("my-skill")
    patch.original_content = "# Original"
    await ps.save(patch)

    for _ in range(3):
        await ts.add_trace(_trace("my-skill", calls=1, errors=1, patch_id=patch.id))

    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# New", encoding="utf-8")

    validator = SkillValidator(ts, ps)
    await validator.check_and_rollback_if_needed(
        "my-skill", skill_file, patch, _cfg(rollback_threshold=0.2, window=3)
    )

    updated = await ps.get_applied_patch("my-skill")
    assert updated is None  # no longer "applied" or "validated"
