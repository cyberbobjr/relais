"""Tests for ChangelogWriter — Phase 1 of S3 skill improvement mechanism.

Follows strict TDD: all tests written before implementation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forgeron.config import ForgeonConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile():
    """Build a minimal ProfileConfig-like object for testing."""
    from common.profile_loader import ProfileConfig, ResilienceConfig

    return ProfileConfig(
        model="anthropic:claude-haiku-4-5",
        temperature=0.0,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _make_config(**kwargs) -> ForgeonConfig:
    """Build a ForgeonConfig with sensible test defaults."""
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
    cfg = ForgeonConfig.__new__(ForgeonConfig)
    for k, v in defaults.items():
        object.__setattr__(cfg, k, v)
    cfg.skills_dir = kwargs.get("skills_dir", None)
    return cfg


def _make_redis(ttl: int = 0) -> AsyncMock:
    """Build a mock Redis connection.

    Args:
        ttl: Value returned by ``redis.ttl()``. 0 = no cooldown, >0 = active.

    Returns:
        AsyncMock mimicking the relevant Redis methods.
    """
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=ttl)
    redis.set = AsyncMock(return_value=True)
    return redis


def _make_llm_mock(content: str) -> MagicMock:
    """Build a mock LLM that returns ``content`` on ``ainvoke``."""
    response = MagicMock()
    response.content = content
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


MESSAGES_RAW = [
    {"type": "human", "content": "Run the deploy script."},
    {"type": "ai", "content": "Deploying now..."},
]

OBSERVATIONS = "- Always run tests before deploying.\n- Check env variables first."


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_creates_changelog_with_entry(tmp_path: Path) -> None:
    """After observe(), CHANGELOG.md must exist with a dated entry."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill\nSome content.", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)
    llm_mock = _make_llm_mock(OBSERVATIONS)

    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock):
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        result = await writer.observe(
            skill_name="my-skill",
            tool_error_count=2,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert result is True
    changelog = skill_dir / "CHANGELOG.md"
    assert changelog.exists(), "CHANGELOG.md should be created"
    content = changelog.read_text(encoding="utf-8")
    assert "##" in content, "Should contain a section header"
    assert "errors=2" in content, "Should record error count"
    assert "Always run tests" in content, "Should contain LLM observations"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_skips_on_cooldown(tmp_path: Path) -> None:
    """When annotation cooldown is active (TTL > 0), observe() returns False without writing."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=120)  # active cooldown

    with patch("forgeron.changelog_writer.build_chat_model") as mock_build:
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        result = await writer.observe(
            skill_name="my-skill",
            tool_error_count=2,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert result is False
    mock_build.assert_not_called()
    assert not (skill_dir / "CHANGELOG.md").exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_skips_when_skill_missing(tmp_path: Path) -> None:
    """When the skill directory does not exist, observe() returns False."""
    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    with patch("forgeron.changelog_writer.build_chat_model") as mock_build:
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        result = await writer.observe(
            skill_name="nonexistent-skill",
            tool_error_count=2,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert result is False
    mock_build.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_skips_on_no_new_observations(tmp_path: Path) -> None:
    """When LLM returns 'No new observations.', no CHANGELOG.md is written."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)
    llm_mock = _make_llm_mock("No new observations.")

    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock):
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        result = await writer.observe(
            skill_name="my-skill",
            tool_error_count=1,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert result is False
    assert not (skill_dir / "CHANGELOG.md").exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_sets_cooldown_key(tmp_path: Path) -> None:
    """After a successful observe(), Redis SET must be called with the correct TTL."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, annotation_cooldown_seconds=600)
    redis = _make_redis(ttl=0)
    llm_mock = _make_llm_mock(OBSERVATIONS)

    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock):
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        await writer.observe(
            skill_name="my-skill",
            tool_error_count=2,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    redis.set.assert_called_once()
    call_args = redis.set.call_args
    key = call_args[0][0] if call_args[0] else call_args.kwargs.get("name", call_args[0][0])
    assert "my-skill" in key
    assert "annotation_cooldown" in key
    # TTL passed as ex= keyword or positional
    kwargs = call_args[1] if call_args[1] else {}
    args = call_args[0] if call_args[0] else ()
    ttl_passed = kwargs.get("ex") or (args[2] if len(args) > 2 else None)
    assert ttl_passed == 600


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_appends_not_overwrites(tmp_path: Path) -> None:
    """Calling observe() twice should produce two separate entries in CHANGELOG.md."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)
    llm_mock = _make_llm_mock("- Observation A.")

    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock):
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        await writer.observe(
            skill_name="my-skill",
            tool_error_count=1,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )
        # Reset cooldown mock to allow second call
        redis.ttl = AsyncMock(return_value=0)
        llm_mock2 = _make_llm_mock("- Observation B.")
    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock2):
        writer2 = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        await writer2.observe(
            skill_name="my-skill",
            tool_error_count=1,
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    content = (skill_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Observation A" in content
    assert "Observation B" in content
    # Two section headers → two entries
    assert content.count("##") >= 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_consolidate_true_when_threshold_exceeded(tmp_path: Path) -> None:
    """should_consolidate() returns True when line count >= threshold and no cooldown."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    changelog = skill_dir / "CHANGELOG.md"
    # Write 90 lines (> default threshold of 80)
    changelog.write_text("\n".join(f"line {i}" for i in range(90)), encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, consolidation_line_threshold=80)
    redis = _make_redis(ttl=0)

    from forgeron.changelog_writer import ChangelogWriter

    writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
    result = await writer.should_consolidate(changelog, cfg, redis, "my-skill")

    assert result is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_consolidate_false_when_under_threshold(tmp_path: Path) -> None:
    """should_consolidate() returns False when CHANGELOG.md has fewer lines than threshold."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    changelog = skill_dir / "CHANGELOG.md"
    changelog.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, consolidation_line_threshold=80)
    redis = _make_redis(ttl=0)

    from forgeron.changelog_writer import ChangelogWriter

    writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
    result = await writer.should_consolidate(changelog, cfg, redis, "my-skill")

    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_consolidate_false_when_consolidation_cooldown_active(
    tmp_path: Path,
) -> None:
    """should_consolidate() returns False when consolidation cooldown is active."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    changelog = skill_dir / "CHANGELOG.md"
    changelog.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, consolidation_line_threshold=80)
    redis = _make_redis(ttl=0)
    # TTL for consolidation cooldown key
    redis.ttl = AsyncMock(return_value=3600)

    from forgeron.changelog_writer import ChangelogWriter

    writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
    result = await writer.should_consolidate(changelog, cfg, redis, "my-skill")

    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observe_force_bypasses_error_guard(tmp_path: Path) -> None:
    """force=True should call the LLM even when tool_error_count=0."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, annotation_min_tool_errors=1)
    redis = _make_redis(ttl=0)
    llm_mock = _make_llm_mock(OBSERVATIONS)

    with patch("forgeron.changelog_writer.build_chat_model", return_value=llm_mock):
        from forgeron.changelog_writer import ChangelogWriter

        writer = ChangelogWriter(profile=_make_profile(), skills_dir=tmp_path)
        result = await writer.observe(
            skill_name="my-skill",
            tool_error_count=0,  # no errors
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
            force=True,  # but force=True
        )

    assert result is True
    llm_mock.ainvoke.assert_called_once()
