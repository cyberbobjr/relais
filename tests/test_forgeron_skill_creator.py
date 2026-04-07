"""Tests for SkillCreator — with_structured_output refactor.

TDD RED phase: these tests import SkillContentLLMResponse (not yet defined)
and assert that create() uses with_structured_output and that the description
comes from the model output rather than _extract_description().
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from forgeron.skill_creator import SkillCreator, SkillContentLLMResponse, SkillCreationResult
from common.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile() -> ProfileConfig:
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _mock_structured_llm(response: SkillContentLLMResponse) -> MagicMock:
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(return_value=response)
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_structured)
    return mock_llm


_VALID_SKILL_CONTENT = """\
---
name: send-email
description: Sends an email via SMTP. Use when user asks to send or compose an email.
---
# send-email

## Step-by-step instructions
1. Collect recipient, subject, and body.
2. Connect to SMTP server.
3. Send the message.
"""


# ---------------------------------------------------------------------------
# Unit tests — create()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_returns_skill_creation_result(tmp_path: Path):
    """Happy path: structured LLM returns valid response → SkillCreationResult."""
    creator = SkillCreator(_make_profile(), tmp_path)
    llm_response = SkillContentLLMResponse(
        skill_content=_VALID_SKILL_CONTENT,
        description="Sends an email via SMTP. Use when user asks to send or compose an email.",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.skill_creator.build_chat_model", return_value=mock_llm):
        result = await creator.create(
            intent_label="send_email",
            session_examples=[{"user_content_preview": "please send an email to alice"}],
        )

    assert result is not None
    assert isinstance(result, SkillCreationResult)
    assert result.skill_name == "send-email"
    assert result.description == "Sends an email via SMTP. Use when user asks to send or compose an email."
    assert result.skill_path.exists()
    assert result.skill_content == _VALID_SKILL_CONTENT.strip()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_calls_with_structured_output(tmp_path: Path):
    """create() must call llm.with_structured_output(SkillContentLLMResponse)."""
    creator = SkillCreator(_make_profile(), tmp_path)
    llm_response = SkillContentLLMResponse(
        skill_content=_VALID_SKILL_CONTENT,
        description="A description.",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.skill_creator.build_chat_model", return_value=mock_llm):
        await creator.create("send_email", [{"user_content_preview": "send email"}])

    mock_llm.with_structured_output.assert_called_once_with(SkillContentLLMResponse)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_idempotent_returns_none_if_skill_exists(tmp_path: Path):
    """create() returns None if skill already exists (idempotency check)."""
    creator = SkillCreator(_make_profile(), tmp_path)
    skill_dir = tmp_path / "send-email"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("existing content")

    result = await creator.create("send_email", [])

    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_returns_none_if_content_too_short(tmp_path: Path):
    """create() returns None if generated content is less than 100 chars."""
    creator = SkillCreator(_make_profile(), tmp_path)
    llm_response = SkillContentLLMResponse(
        skill_content="too short",
        description="short",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.skill_creator.build_chat_model", return_value=mock_llm):
        result = await creator.create("send_email", [{"user_content_preview": "x"}])

    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_uses_llm_description_not_extracted(tmp_path: Path):
    """description comes from LLM response, not parsed from skill_content."""
    creator = SkillCreator(_make_profile(), tmp_path)
    # skill_content has NO ## Description section — but description field is set
    content_without_description_section = "---\nname: search-web\n---\n# search-web\n\n" + "x" * 200
    llm_response = SkillContentLLMResponse(
        skill_content=content_without_description_section,
        description="Searches the web for information.",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.skill_creator.build_chat_model", return_value=mock_llm):
        result = await creator.create("search_web", [{"user_content_preview": "search x"}])

    assert result is not None
    assert result.description == "Searches the web for information."


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_writes_file_to_correct_path(tmp_path: Path):
    """SKILL.md is written to skills_dir/{skill_name}/SKILL.md."""
    creator = SkillCreator(_make_profile(), tmp_path)
    llm_response = SkillContentLLMResponse(
        skill_content=_VALID_SKILL_CONTENT,
        description="desc",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.skill_creator.build_chat_model", return_value=mock_llm):
        result = await creator.create("send_email", [{"user_content_preview": "email"}])

    assert result is not None
    expected_path = tmp_path / "send-email" / "SKILL.md"
    assert result.skill_path == expected_path
    assert expected_path.read_text() == _VALID_SKILL_CONTENT.strip()


# ---------------------------------------------------------------------------
# Unit tests — system prompt contains skill writing norms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_system_prompt_contains_frontmatter_spec():
    """_SYSTEM_PROMPT must reference the SKILL.md frontmatter specification."""
    from forgeron.skill_creator import SkillCreator
    assert "name" in SkillCreator._SYSTEM_PROMPT
    assert "description" in SkillCreator._SYSTEM_PROMPT
    assert "frontmatter" in SkillCreator._SYSTEM_PROMPT.lower()
