"""Tests for SkillConsolidator — Phase 2 of S3 skill improvement mechanism.

Follows strict TDD: all tests written before implementation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forgeron.config import ForgeonConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile():
    """Build a minimal ProfileConfig for testing."""
    from common.profile_loader import ProfileConfig, ResilienceConfig

    return ProfileConfig(
        model="anthropic:claude-sonnet-4-6",
        temperature=0.0,
        max_tokens=4096,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _make_config(**kwargs) -> ForgeonConfig:
    """Build a ForgeonConfig with test defaults."""
    cfg = ForgeonConfig.__new__(ForgeonConfig)
    defaults = dict(
        annotation_mode=True,
        annotation_min_tool_errors=1,
        annotation_cooldown_seconds=300,
        annotation_call_threshold=10,
        consolidation_line_threshold=80,
        consolidation_cooldown_seconds=604800,
        consolidation_profile="precise",
        notify_user_on_consolidation=True,
    )
    defaults.update(kwargs)
    for k, v in defaults.items():
        object.__setattr__(cfg, k, v)
    cfg.skills_dir = kwargs.get("skills_dir", None)
    return cfg


def _make_redis(ttl: int = 0) -> AsyncMock:
    """Build a mock Redis connection.

    Args:
        ttl: Value returned by ``redis.ttl()``.

    Returns:
        AsyncMock for Redis.
    """
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=ttl)
    redis.set = AsyncMock(return_value=True)
    return redis


def _make_structured_llm_mock(result) -> MagicMock:
    """Build a mock LLM with with_structured_output returning ``result``."""
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(return_value=result)
    llm = MagicMock()
    llm.with_structured_output = MagicMock(return_value=mock_structured)
    return llm


SKILL_CONTENT = "# My Skill\n\nDo this. Do that.\n"
CHANGELOG_CONTENT = (
    "## 2026-04-07T10:00Z (errors=1, trigger=error)\n"
    "- Always check env vars.\n"
    "- Run tests first.\n"
)

CONSOLIDATED_SKILL = (
    "# My Skill\n\n"
    "Do this. Do that.\n\n"
    "## Best Practices\n"
    "- Always check env vars.\n"
    "- Run tests first."
)

CONSOLIDATED_DIGEST = (
    "Absorbed: 2\n"
    "- env var check added\n"
    "- test-first pattern noted\n"
    "Discarded: 0"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_rewrites_skill_file(tmp_path: Path) -> None:
    """consolidate() must rewrite SKILL.md with the LLM-generated content."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, consolidation_cooldown_seconds=604800)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill=CONSOLIDATED_SKILL, digest=CONSOLIDATED_DIGEST))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        result = await consolidator.consolidate("my-skill", redis, cfg)

    assert result is True
    new_skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Best Practices" in new_skill
    assert "Always check env vars" in new_skill


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_produces_digest_file(tmp_path: Path) -> None:
    """consolidate() must create/append to CHANGELOG_DIGEST.md."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill=CONSOLIDATED_SKILL, digest=CONSOLIDATED_DIGEST))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        await consolidator.consolidate("my-skill", redis, cfg)

    digest = skill_dir / "CHANGELOG_DIGEST.md"
    assert digest.exists(), "CHANGELOG_DIGEST.md should be created"
    content = digest.read_text(encoding="utf-8")
    assert "Consolidation" in content
    assert "Absorbed" in content


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_clears_changelog(tmp_path: Path) -> None:
    """consolidate() must clear CHANGELOG.md after successful consolidation."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill=CONSOLIDATED_SKILL, digest=CONSOLIDATED_DIGEST))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        await consolidator.consolidate("my-skill", redis, cfg)

    changelog_content = (skill_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog_content.strip() == "", "CHANGELOG.md should be cleared"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_sets_cooldown(tmp_path: Path) -> None:
    """consolidate() must set Redis consolidation cooldown after success."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, consolidation_cooldown_seconds=604800)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill=CONSOLIDATED_SKILL, digest=CONSOLIDATED_DIGEST))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        await consolidator.consolidate("my-skill", redis, cfg)

    redis.set.assert_called_once()
    call_args = redis.set.call_args
    args = call_args[0] if call_args[0] else ()
    kwargs = call_args[1] if call_args[1] else {}
    key = args[0] if args else kwargs.get("name", "")
    assert "consolidation_cooldown" in key
    assert "my-skill" in key
    ttl = kwargs.get("ex") or (args[2] if len(args) > 2 else None)
    assert ttl == 604800


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_noop_on_llm_failure(tmp_path: Path) -> None:
    """consolidate() must leave all files unchanged when the LLM raises an exception."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    original_skill = SKILL_CONTENT
    original_changelog = CHANGELOG_CONTENT
    (skill_dir / "SKILL.md").write_text(original_skill, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(original_changelog, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
    llm = MagicMock()
    llm.with_structured_output = MagicMock(return_value=mock_structured)

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        result = await consolidator.consolidate("my-skill", redis, cfg)

    assert result is False
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == original_skill
    assert (skill_dir / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog
    assert not (skill_dir / "CHANGELOG_DIGEST.md").exists()
    redis.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_noop_on_empty_skill_content(tmp_path: Path) -> None:
    """consolidate() must leave all files unchanged when LLM returns empty updated_skill."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    original_skill = SKILL_CONTENT
    (skill_dir / "SKILL.md").write_text(original_skill, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill="", digest="nothing"))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        result = await consolidator.consolidate("my-skill", redis, cfg)

    assert result is False
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == original_skill
    redis.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_noop_when_skill_missing(tmp_path: Path) -> None:
    """consolidate() must return False when SKILL.md does not exist."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")
    # Note: SKILL.md is intentionally NOT created

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()

    with patch("forgeron.skill_consolidator.build_chat_model") as mock_build:
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        result = await consolidator.consolidate("my-skill", redis, cfg)

    assert result is False
    mock_build.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_noop_when_changelog_empty(tmp_path: Path) -> None:
    """consolidate() must return False when CHANGELOG.md is empty or missing."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    # CHANGELOG.md is empty
    (skill_dir / "CHANGELOG.md").write_text("", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()

    with patch("forgeron.skill_consolidator.build_chat_model") as mock_build:
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        result = await consolidator.consolidate("my-skill", redis, cfg)

    assert result is False
    mock_build.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_consolidate_calls_with_structured_output(tmp_path: Path) -> None:
    """consolidate() must call llm.with_structured_output(ConsolidationResult)."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_CONTENT, encoding="utf-8")
    (skill_dir / "CHANGELOG.md").write_text(CHANGELOG_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis()
    from forgeron.skill_consolidator import ConsolidationResult as CR
    llm_mock = _make_structured_llm_mock(CR(updated_skill=CONSOLIDATED_SKILL, digest=CONSOLIDATED_DIGEST))

    with patch("forgeron.skill_consolidator.build_chat_model", return_value=llm_mock):
        from forgeron.skill_consolidator import SkillConsolidator

        consolidator = SkillConsolidator(profile=_make_profile(), skills_dir=tmp_path)
        await consolidator.consolidate("my-skill", redis, cfg)

    llm_mock.with_structured_output.assert_called_once_with(CR)
