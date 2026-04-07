"""Tests for SkillAnalyzer — with_structured_output refactor.

TDD RED phase: these tests import SkillPatchLLMResponse (not yet defined)
and assert that analyze() uses with_structured_output instead of manual
JSON parsing.
"""

from __future__ import annotations

import difflib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from forgeron.analyzer import SkillAnalyzer, SkillPatchLLMResponse, SkillPatchProposal
from forgeron.models import SkillTrace
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


def _make_trace(**kwargs) -> SkillTrace:
    defaults = dict(
        skill_name="my-skill",
        correlation_id="corr-1",
        tool_call_count=2,
        tool_error_count=1,
        messages_raw='[{"type":"human","content":"do the thing"},{"type":"ai","content":"done"}]',
    )
    defaults.update(kwargs)
    return SkillTrace(**defaults)


def _mock_structured_llm(response: SkillPatchLLMResponse) -> tuple[MagicMock, AsyncMock]:
    """Return (mock_llm, mock_structured_llm) pre-wired with the given response."""
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(return_value=response)
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_structured)
    return mock_llm, mock_structured


# ---------------------------------------------------------------------------
# Unit tests — analyze()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_analyze_returns_skill_patch_proposal():
    """Happy path: structured LLM returns valid response → SkillPatchProposal."""
    analyzer = SkillAnalyzer(_make_profile())
    llm_response = SkillPatchLLMResponse(
        patched_content="# my-skill\nImproved content",
        rationale="Fixed command flags based on trace errors.",
    )
    mock_llm, _ = _mock_structured_llm(llm_response)

    with patch("forgeron.analyzer.build_chat_model", return_value=mock_llm):
        result = await analyzer.analyze(
            skill_name="my-skill",
            skill_content="# my-skill\nOriginal content",
            traces=[_make_trace()],
        )

    assert isinstance(result, SkillPatchProposal)
    assert result.patched_content == "# my-skill\nImproved content"
    assert result.rationale == "Fixed command flags based on trace errors."
    assert "my-skill/SKILL.md" in result.diff


@pytest.mark.asyncio
@pytest.mark.unit
async def test_analyze_calls_with_structured_output():
    """analyze() must call llm.with_structured_output(SkillPatchLLMResponse)."""
    analyzer = SkillAnalyzer(_make_profile())
    llm_response = SkillPatchLLMResponse(patched_content="improved", rationale="reason")
    mock_llm, _ = _mock_structured_llm(llm_response)

    with patch("forgeron.analyzer.build_chat_model", return_value=mock_llm):
        await analyzer.analyze("skill", "content", [_make_trace()])

    mock_llm.with_structured_output.assert_called_once_with(SkillPatchLLMResponse)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_analyze_raises_on_empty_patched_content():
    """Empty patched_content raises ValueError."""
    analyzer = SkillAnalyzer(_make_profile())
    mock_llm, _ = _mock_structured_llm(
        SkillPatchLLMResponse(patched_content="", rationale="ok")
    )

    with patch("forgeron.analyzer.build_chat_model", return_value=mock_llm):
        with pytest.raises(ValueError, match="patched_content"):
            await analyzer.analyze("skill", "content", [_make_trace()])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_analyze_raises_on_empty_rationale():
    """Empty rationale raises ValueError."""
    analyzer = SkillAnalyzer(_make_profile())
    mock_llm, _ = _mock_structured_llm(
        SkillPatchLLMResponse(patched_content="improved", rationale="")
    )

    with patch("forgeron.analyzer.build_chat_model", return_value=mock_llm):
        with pytest.raises(ValueError, match="rationale"):
            await analyzer.analyze("skill", "content", [_make_trace()])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_analyze_diff_reflects_changes():
    """Diff field reflects actual line changes between original and patched."""
    original = "line1\nline2\nline3"
    patched = "line1\nLINE2\nline3"
    expected_diff = "\n".join(difflib.unified_diff(
        original.splitlines(), patched.splitlines(),
        fromfile="my-skill/SKILL.md (original)",
        tofile="my-skill/SKILL.md (patched)",
        lineterm="",
    ))

    analyzer = SkillAnalyzer(_make_profile())
    mock_llm, _ = _mock_structured_llm(
        SkillPatchLLMResponse(patched_content=patched, rationale="changed line2")
    )

    with patch("forgeron.analyzer.build_chat_model", return_value=mock_llm):
        result = await analyzer.analyze("my-skill", original, [_make_trace()])

    assert result.diff == expected_diff


# ---------------------------------------------------------------------------
# Unit tests — _build_user_message()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_user_message_includes_skill_name_and_content():
    """_build_user_message includes skill name and content."""
    analyzer = SkillAnalyzer(_make_profile())
    msg = analyzer._build_user_message("my-skill", "# My Skill\nDo the thing.", [])
    assert "my-skill" in msg
    assert "# My Skill" in msg
    assert "Do the thing." in msg


@pytest.mark.unit
def test_build_user_message_handles_malformed_trace_json():
    """_build_user_message doesn't crash on malformed messages_raw."""
    analyzer = SkillAnalyzer(_make_profile())
    bad_trace = _make_trace(messages_raw="not valid json {{")
    msg = analyzer._build_user_message("skill", "content", [bad_trace])
    assert "trace content unavailable" in msg


@pytest.mark.unit
def test_build_user_message_shows_trace_count():
    """_build_user_message shows the correct trace count."""
    analyzer = SkillAnalyzer(_make_profile())
    traces = [_make_trace(), _make_trace()]
    msg = analyzer._build_user_message("skill", "content", traces)
    assert "2" in msg


@pytest.mark.unit
def test_build_user_message_handles_list_content_blocks():
    """_build_user_message handles LangChain structured content blocks (list of dicts)."""
    analyzer = SkillAnalyzer(_make_profile())
    import json
    messages = [{"type": "ai", "content": [{"text": "hello"}, {"text": "world"}]}]
    trace = _make_trace(messages_raw=json.dumps(messages))
    msg = analyzer._build_user_message("skill", "content", [trace])
    assert "hello" in msg
    assert "world" in msg


# ---------------------------------------------------------------------------
# Unit tests — system prompt contains skill writing norms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_system_prompt_contains_frontmatter_spec():
    """_SYSTEM_PROMPT must reference the mandatory frontmatter fields."""
    from forgeron.analyzer import _SYSTEM_PROMPT
    assert "name" in _SYSTEM_PROMPT
    assert "description" in _SYSTEM_PROMPT
    # Must not still have the old manual JSON format instruction
    assert "raw JSON only" not in _SYSTEM_PROMPT
