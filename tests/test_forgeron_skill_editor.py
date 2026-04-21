"""Tests for SkillEditor — single-step direct SKILL.md improvement.

Covers: rewrite on changed=True, noop on changed=False, cooldown guard,
missing SKILL.md, cooldown TTL set, and atomic write behaviour.
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
    defaults = dict(
        edit_mode=True,
        edit_min_tool_errors=1,
        edit_cooldown_seconds=300,
        edit_call_threshold=10,
    )
    defaults.update(kwargs)
    cfg = ForgeonConfig.__new__(ForgeonConfig)
    for k, v in defaults.items():
        object.__setattr__(cfg, k, v)
    cfg.skills_dir = kwargs.get("skills_dir", None)
    return cfg


def _make_redis(ttl: int = 0) -> AsyncMock:
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=ttl)
    redis.setex = AsyncMock(return_value=True)
    return redis


def _make_edit_result(*, changed: bool, updated_skill: str = "# Updated\nContent.", reason: str = "improved"):
    from forgeron.skill_editor import SkillEditResult

    return SkillEditResult(updated_skill=updated_skill, changed=changed, reason=reason)


MESSAGES_RAW = [
    {"type": "human", "content": "Run the deploy script."},
    {"type": "ai", "content": "Deploying now..."},
]

ORIGINAL_CONTENT = "# My Skill\n\nOriginal content here."
UPDATED_CONTENT = "# My Skill\n\nImproved content with new lessons."


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_rewrites_skill_md_when_changed(tmp_path: Path) -> None:
    """When LLM returns changed=True, SKILL.md is rewritten with the new content."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    result = _make_edit_result(changed=True, updated_skill=UPDATED_CONTENT)
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
            trigger_reason="2 errors",
        )

    assert edited is True
    assert skill_md.read_text(encoding="utf-8") == UPDATED_CONTENT


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_noop_when_changed_false(tmp_path: Path) -> None:
    """When LLM returns changed=False, SKILL.md is not rewritten."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    result = _make_edit_result(changed=False, updated_skill=ORIGINAL_CONTENT, reason="nothing new")
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert edited is False
    assert skill_md.read_text(encoding="utf-8") == ORIGINAL_CONTENT


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_skips_on_cooldown(tmp_path: Path) -> None:
    """When cooldown TTL > 0 and force=False, edit() returns False without calling LLM."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=250)  # active cooldown

    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert edited is False
    mock_build.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_force_bypasses_cooldown(tmp_path: Path) -> None:
    """force=True skips the cooldown check and calls the LLM anyway."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, edit_cooldown_seconds=300)
    redis = _make_redis(ttl=200)  # active cooldown

    result = _make_edit_result(changed=True, updated_skill=UPDATED_CONTENT)
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
            force=True,
        )

    assert edited is True
    mock_build.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_skips_when_skill_md_missing(tmp_path: Path) -> None:
    """When SKILL.md does not exist, edit() returns False without calling LLM."""
    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="nonexistent-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert edited is False
    mock_build.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_sets_cooldown_key_after_write(tmp_path: Path) -> None:
    """After a successful rewrite, setex is called with the correct key and TTL."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path, edit_cooldown_seconds=600)
    redis = _make_redis(ttl=0)

    result = _make_edit_result(changed=True, updated_skill=UPDATED_CONTENT)
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    redis.setex.assert_called_once()
    call_args = redis.setex.call_args
    args = call_args[0] if call_args[0] else ()
    kwargs = call_args[1] if call_args[1] else {}
    key = args[0] if args else kwargs.get("name", "")
    ttl = args[1] if len(args) > 1 else kwargs.get("time", None)
    assert "my-skill" in key
    assert "edit_cooldown" in key
    assert ttl == 600


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_atomic_write_uses_tmp_file(tmp_path: Path) -> None:
    """The atomic write must use a .tmp file then os.replace — no partial writes."""
    import os

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    result = _make_edit_result(changed=True, updated_skill=UPDATED_CONTENT)
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        replaced.append((src, dst))
        real_replace(src, dst)

    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        with patch("forgeron.skill_editor.os.replace", side_effect=spy_replace):
            from forgeron.skill_editor import SkillEditor

            editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
            await editor.edit(
                skill_name="my-skill",
                messages_raw=MESSAGES_RAW,
                config=cfg,
                redis_conn=redis,
            )

    assert len(replaced) == 1, "os.replace must be called exactly once"
    src_path, dst_path = replaced[0]
    assert src_path.endswith(".tmp"), f"Source of replace must be .tmp, got: {src_path}"
    assert dst_path == str(skill_md), f"Destination must be SKILL.md path, got: {dst_path}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_noop_when_content_identical(tmp_path: Path) -> None:
    """When updated_skill content matches existing SKILL.md after strip, no write occurs."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(ORIGINAL_CONTENT, encoding="utf-8")

    cfg = _make_config(skills_dir=tmp_path)
    redis = _make_redis(ttl=0)

    # LLM says changed=True but returns the same content
    result = _make_edit_result(changed=True, updated_skill=ORIGINAL_CONTENT)
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
        )

    assert edited is False
    redis.setex.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_edit_uses_skill_path_override(tmp_path: Path) -> None:
    """When skill_path is given, SKILL.md is resolved from that directory."""
    override_dir = tmp_path / "custom-location" / "my-skill"
    override_dir.mkdir(parents=True)
    skill_md = override_dir / "SKILL.md"
    skill_md.write_text(ORIGINAL_CONTENT, encoding="utf-8")

    # skills_dir points nowhere relevant
    cfg = _make_config(skills_dir=tmp_path / "skills")
    redis = _make_redis(ttl=0)

    result = _make_edit_result(changed=True, updated_skill=UPDATED_CONTENT)
    with patch("forgeron.skill_editor.build_chat_model") as mock_build:
        llm = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=result)
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        mock_build.return_value = llm

        from forgeron.skill_editor import SkillEditor

        editor = SkillEditor(profile=_make_profile(), skills_dir=None)
        edited = await editor.edit(
            skill_name="my-skill",
            messages_raw=MESSAGES_RAW,
            config=cfg,
            redis_conn=redis,
            skill_path=override_dir,
        )

    assert edited is True
    assert skill_md.read_text(encoding="utf-8") == UPDATED_CONTENT
