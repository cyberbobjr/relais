"""Tests for the edit_history.jsonl journal — _append_edit_history + SkillEditor integration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forgeron.models import EditHistoryEntry
from forgeron.skill_editor import (
    EDIT_HISTORY_FILENAME,
    MAX_HISTORY_ENTRIES,
    _REASON_LLM_FAILURE,
    _append_edit_history,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_entry(**kwargs) -> EditHistoryEntry:
    defaults = dict(ts=1_000_000.0, trigger="3 errors", reason="added tip", changed=True, corr="corr-1")
    defaults.update(kwargs)
    return EditHistoryEntry(**defaults)


def _read_history(skill_md: Path) -> list[dict]:
    hist = skill_md.parent / EDIT_HISTORY_FILENAME
    if not hist.exists():
        return []
    return [json.loads(line) for line in hist.read_text().splitlines() if line.strip()]


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


def _make_config(**kwargs):
    from forgeron.config import ForgeonConfig

    defaults = dict(
        edit_mode=True,
        edit_min_tool_errors=1,
        edit_cooldown_seconds=300,
        edit_call_threshold=10,
        correction_mode=False,
        creation_mode=False,
        skills_dir=None,
    )
    defaults.update(kwargs)
    return ForgeonConfig(**defaults)


# ---------------------------------------------------------------------------
# Unit tests — _append_edit_history
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_append_creates_file(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")

    _append_edit_history(skill_md, _make_entry())

    rows = _read_history(skill_md)
    assert len(rows) == 1
    assert rows[0]["changed"] is True
    assert rows[0]["corr"] == "corr-1"


@pytest.mark.unit
def test_append_accumulates(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")

    for i in range(3):
        _append_edit_history(skill_md, _make_entry(corr=f"c-{i}"))

    rows = _read_history(skill_md)
    assert len(rows) == 3
    assert [r["corr"] for r in rows] == ["c-0", "c-1", "c-2"]


@pytest.mark.unit
def test_rotation_keeps_last_n(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")

    for i in range(MAX_HISTORY_ENTRIES + 5):
        _append_edit_history(skill_md, _make_entry(corr=f"c-{i}"))

    rows = _read_history(skill_md)
    assert len(rows) == MAX_HISTORY_ENTRIES
    assert rows[0]["corr"] == "c-5"
    assert rows[-1]["corr"] == f"c-{MAX_HISTORY_ENTRIES + 4}"


@pytest.mark.unit
def test_entry_fields_serialized(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")
    entry = EditHistoryEntry(ts=9999.5, trigger="aborted", reason="loop detected", changed=False, corr="xyz")

    _append_edit_history(skill_md, entry)

    row = _read_history(skill_md)[0]
    assert row == {"ts": 9999.5, "trigger": "aborted", "reason": "loop detected", "changed": False, "corr": "xyz"}


@pytest.mark.unit
def test_append_tolerates_missing_parent_gracefully(tmp_path, caplog):
    # skill_md whose parent directory does not exist — should log warning, not raise
    skill_md = tmp_path / "nonexistent_dir" / "SKILL.md"
    import logging

    with caplog.at_level(logging.WARNING, logger="forgeron.skill_editor"):
        _append_edit_history(skill_md, _make_entry())

    assert "Could not write" in caplog.text


@pytest.mark.unit
def test_changed_false_entry(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")

    _append_edit_history(skill_md, _make_entry(changed=False, reason=_REASON_LLM_FAILURE))

    rows = _read_history(skill_md)
    assert rows[0]["changed"] is False
    assert rows[0]["reason"] == _REASON_LLM_FAILURE


@pytest.mark.unit
def test_rotation_uses_atomic_write(tmp_path, monkeypatch):
    """Rotation must go through tmp + os.replace, not direct open('a')."""
    import os

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# skill", encoding="utf-8")

    # Fill up to MAX_HISTORY_ENTRIES via append
    for i in range(MAX_HISTORY_ENTRIES):
        _append_edit_history(skill_md, _make_entry(corr=f"c-{i}"))

    replace_calls: list[tuple] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    # This triggers rotation
    _append_edit_history(skill_md, _make_entry(corr="overflow"))

    assert any(str(skill_md.parent / EDIT_HISTORY_FILENAME) in dst for _, dst in replace_calls), (
        "rotation should use os.replace to write the trimmed file"
    )


# ---------------------------------------------------------------------------
# Integration tests — SkillEditor.edit() journal emission
# ---------------------------------------------------------------------------


def _make_redis_mock(ttl: int = 0) -> AsyncMock:
    redis = AsyncMock()
    redis.ttl.return_value = ttl
    redis.setex.return_value = True
    return redis


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_changed_writes_journal(tmp_path):
    from forgeron.skill_editor import SkillEditor, SkillEditResult

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# original", encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config(edit_cooldown_seconds=300)
    redis = _make_redis_mock()

    messages_with_skill = [
        {"type": "human", "content": "use my-skill"},
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}],
        },
        {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
    ]
    mock_result = SkillEditResult(updated_skill="# improved", changed=True, reason="added tip")
    with patch.object(editor, "_call_llm", new=AsyncMock(return_value=mock_result)):
        result = await editor.edit("my-skill", messages_with_skill, config, redis, trigger_reason="errors", correlation_id="abc123")

    assert result is True
    rows = _read_history(skill_md)
    assert len(rows) == 1
    assert rows[0]["changed"] is True
    assert rows[0]["corr"] == "abc123"
    assert rows[0]["trigger"] == "errors"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_unchanged_writes_journal(tmp_path):
    from forgeron.skill_editor import SkillEditor, SkillEditResult

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# content", encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config()
    redis = _make_redis_mock()

    messages_with_skill = [
        {"type": "human", "content": "use my-skill"},
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}],
        },
        {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
    ]
    mock_result = SkillEditResult(updated_skill="# content", changed=False, reason="nothing new")
    with patch.object(editor, "_call_llm", new=AsyncMock(return_value=mock_result)):
        result = await editor.edit("my-skill", messages_with_skill, config, redis, correlation_id="xyz")

    assert result is False
    rows = _read_history(skill_md)
    assert len(rows) == 1
    assert rows[0]["changed"] is False
    assert rows[0]["corr"] == "xyz"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_llm_failure_writes_journal(tmp_path):
    from forgeron.skill_editor import SkillEditor

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# content", encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config()
    redis = _make_redis_mock()

    messages_with_skill = [
        {"type": "human", "content": "use my-skill"},
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}],
        },
        {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
    ]
    with patch.object(editor, "_call_llm", new=AsyncMock(return_value=None)):
        result = await editor.edit("my-skill", messages_with_skill, config, redis, trigger_reason="aborted", correlation_id="fail-1")

    assert result is False
    rows = _read_history(skill_md)
    assert len(rows) == 1
    assert rows[0]["reason"] == _REASON_LLM_FAILURE
    assert rows[0]["changed"] is False
    assert rows[0]["corr"] == "fail-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_cooldown_skips_journal(tmp_path):
    """When cooldown is active, edit() returns early — no journal entry expected."""
    from forgeron.skill_editor import SkillEditor

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# content", encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config()
    redis = _make_redis_mock(ttl=120)  # cooldown active

    result = await editor.edit("my-skill", [], config, redis, correlation_id="cooldown")

    assert result is False
    rows = _read_history(skill_md)
    assert rows == [], "no journal entry should be written when cooldown blocks the attempt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_default_correlation_id(tmp_path):
    """correlation_id defaults to empty string — journal must still be written."""
    from forgeron.skill_editor import SkillEditor, SkillEditResult

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    skill_md.write_text("# content", encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config()
    redis = _make_redis_mock()

    messages_with_skill = [
        {"type": "human", "content": "use my-skill"},
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}],
        },
        {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
    ]
    mock_result = SkillEditResult(updated_skill="# improved", changed=True, reason="tip added")
    with patch.object(editor, "_call_llm", new=AsyncMock(return_value=mock_result)):
        await editor.edit("my-skill", messages_with_skill, config, redis)

    rows = _read_history(skill_md)
    assert rows[0]["corr"] == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_identical_content_writes_journal(tmp_path):
    """If LLM returns changed=True but content is identical after strip, journal entry is still written."""
    from forgeron.skill_editor import SkillEditor, SkillEditResult

    skill_md = tmp_path / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir()
    original = "# content"
    skill_md.write_text(original, encoding="utf-8")

    editor = SkillEditor(profile=_make_profile(), skills_dir=tmp_path)
    config = _make_config()
    redis = _make_redis_mock()

    messages_with_skill = [
        {"type": "human", "content": "use my-skill"},
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}],
        },
        {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
    ]
    mock_result = SkillEditResult(updated_skill=original, changed=True, reason="unchanged")
    with patch.object(editor, "_call_llm", new=AsyncMock(return_value=mock_result)):
        result = await editor.edit("my-skill", messages_with_skill, config, redis, correlation_id="strip-test")

    assert result is False
    rows = _read_history(skill_md)
    assert len(rows) == 1
    assert rows[0]["changed"] is False
