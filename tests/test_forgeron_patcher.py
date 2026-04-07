"""Tests for SkillPatcher — atomic write and rollback (Step 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeron.models import SkillPatch
from forgeron.patcher import SkillPatcher


def _patch(skill_name: str, original: str, patched: str) -> SkillPatch:
    return SkillPatch(
        skill_name=skill_name,
        original_content=original,
        patched_content=patched,
        diff="",
        rationale="test",
        trigger_correlation_id="corr-1",
        pre_patch_error_rate=0.5,
    )


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_writes_patched_content(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Old content", encoding="utf-8")

    patch = _patch("my-skill", "# Old content", "# New content")
    patcher = SkillPatcher(skills_dir=tmp_path)
    patcher.apply(skill_file, patch)

    assert skill_file.read_text(encoding="utf-8") == "# New content"


@pytest.mark.unit
def test_apply_creates_bak_file(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Original", encoding="utf-8")

    patch = _patch("my-skill", "# Original", "# Improved")
    patcher = SkillPatcher(skills_dir=tmp_path)
    patcher.apply(skill_file, patch)

    bak = tmp_path / "SKILL.md.bak"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == "# Original"


@pytest.mark.unit
def test_apply_leaves_no_pending_file(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Old", encoding="utf-8")

    patch = _patch("my-skill", "# Old", "# New")
    patcher = SkillPatcher(skills_dir=tmp_path)
    patcher.apply(skill_file, patch)

    pending = tmp_path / "SKILL.md.pending"
    assert not pending.exists()


@pytest.mark.unit
def test_apply_raises_on_empty_patched_content(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Old", encoding="utf-8")

    patch = _patch("my-skill", "# Old", "   ")  # whitespace-only
    patcher = SkillPatcher(skills_dir=tmp_path)

    with pytest.raises(ValueError, match="empty patch"):
        patcher.apply(skill_file, patch)

    # Original must remain untouched
    assert skill_file.read_text(encoding="utf-8") == "# Old"


@pytest.mark.unit
def test_apply_original_unchanged_after_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Original", encoding="utf-8")
    patch = _patch("my-skill", "# Original", "# New")

    # Force failure on os.replace
    import os as _os
    monkeypatch.setattr(_os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    patcher = SkillPatcher(skills_dir=tmp_path)
    with pytest.raises(OSError):
        patcher.apply(skill_file, patch)

    assert skill_file.read_text(encoding="utf-8") == "# Original"
    assert not (tmp_path / "SKILL.md.pending").exists()


# ---------------------------------------------------------------------------
# rollback()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rollback_from_bak_file(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Patched", encoding="utf-8")
    bak_file = tmp_path / "SKILL.md.bak"
    bak_file.write_text("# Original", encoding="utf-8")

    patch = _patch("my-skill", "# Original", "# Patched")
    patcher = SkillPatcher(skills_dir=tmp_path)
    patcher.rollback(skill_file, patch)

    assert skill_file.read_text(encoding="utf-8") == "# Original"


@pytest.mark.unit
def test_rollback_from_original_content_when_no_bak(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Patched", encoding="utf-8")
    # No .bak file

    patch = _patch("my-skill", "# Original via DB", "# Patched")
    patcher = SkillPatcher(skills_dir=tmp_path)
    patcher.rollback(skill_file, patch)

    assert skill_file.read_text(encoding="utf-8") == "# Original via DB"


@pytest.mark.unit
def test_rollback_raises_when_no_source(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Patched", encoding="utf-8")

    patch = _patch("my-skill", original="", patched="# Patched")
    patcher = SkillPatcher(skills_dir=tmp_path)

    with pytest.raises(ValueError, match="Cannot roll back"):
        patcher.rollback(skill_file, patch)
