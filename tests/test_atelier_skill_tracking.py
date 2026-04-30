"""Tests for actual-invocation skill tracking in Atelier and stream_loop.

RC#2: skills_used must reflect read_skill() calls made during the turn,
not the role's assigned skill list.

The function _extract_invoked_skill_names does not exist yet in atelier.main —
these tests are the RED phase that drives its implementation.

build_subagent_traces (in atelier.stream_loop) uses subagent_skill_map (the
assigned skill list) as skill_names in each SubagentTrace.  After the fix it
must instead extract skill names from the messages that were actually produced
by the subagent (read_skill tool_calls), ignoring anything in subagent_skill_map
that was never called.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_serialized_ai_with_read_skill(*skill_names: str) -> dict:
    """Return a serialized AIMessage dict with read_skill tool_calls."""
    tool_calls = [
        {"id": f"tc-{sn}", "name": "read_skill", "args": {"skill_name": sn}}
        for sn in skill_names
    ]
    return {"type": "ai", "content": "", "tool_calls": tool_calls}


def _make_human(content: str = "do something") -> dict:
    """Return a serialized HumanMessage dict."""
    return {"type": "human", "content": content}


# ---------------------------------------------------------------------------
# Tests for _extract_invoked_skill_names (not yet implemented in atelier.main)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skills_used_contains_only_invoked_skills() -> None:
    """_extract_invoked_skill_names returns only skills called via read_skill.

    The function must scan messages_raw for AIMessage tool_calls where
    name == "read_skill" and collect the skill_name argument values.
    It must NOT include skills assigned to the agent role that were never
    actually read during the turn.
    """
    from atelier.main import _extract_invoked_skill_names  # noqa: PLC0415 — tested symbol

    messages_raw = [
        _make_human("Search and email."),
        _make_serialized_ai_with_read_skill("search-web", "mail-summary"),
        {"type": "tool", "tool_call_id": "tc-search-web", "content": "search content"},
        {"type": "tool", "tool_call_id": "tc-mail-summary", "content": "mail content"},
        {"type": "ai", "content": "Done."},
    ]

    result = _extract_invoked_skill_names(messages_raw)

    assert "search-web" in result, "search-web was invoked via read_skill, must be in result"
    assert "mail-summary" in result, "mail-summary was invoked via read_skill, must be in result"
    assert len(result) == 2, f"Expected exactly 2 skills, got {len(result)}: {result}"


@pytest.mark.unit
def test_skills_used_empty_when_no_read_skill_calls() -> None:
    """No read_skill calls in the conversation → empty list.

    When the agent answered without consulting any skill, skills_used must be
    empty so Forgeron does not attempt to update unrelated skill files.
    """
    from atelier.main import _extract_invoked_skill_names  # noqa: PLC0415

    messages_raw = [
        _make_human("Just answer."),
        {"type": "ai", "content": "Here is the answer."},
    ]

    result = _extract_invoked_skill_names(messages_raw)

    assert result == [], f"Expected [], got {result!r}"


@pytest.mark.unit
def test_skills_used_deduplicates_repeated_calls() -> None:
    """Same skill read multiple times → appears exactly once in the result.

    The agent might read the same SKILL.md twice (e.g. after a tool error).
    The returned list must contain each skill name only once to avoid duplicate
    Forgeron edit triggers.
    """
    from atelier.main import _extract_invoked_skill_names  # noqa: PLC0415

    messages_raw = [
        _make_human("Step 1"),
        _make_serialized_ai_with_read_skill("mail-summary"),
        {"type": "tool", "tool_call_id": "tc-mail-summary", "content": "content"},
        _make_human("Step 2"),
        _make_serialized_ai_with_read_skill("mail-summary"),
        {"type": "tool", "tool_call_id": "tc-mail-summary-2", "content": "content"},
    ]

    result = _extract_invoked_skill_names(messages_raw)

    assert result.count("mail-summary") == 1, (
        f"mail-summary must appear exactly once, got {result.count('mail-summary')} times"
    )


# ---------------------------------------------------------------------------
# Test for build_subagent_traces — skill_names must come from invocations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_skill_names_from_invocations() -> None:
    """build_subagent_traces uses actually-invoked skills, not assigned skills.

    RC#2: subagent_skill_map carries role-assigned skills (e.g. ["jw-daily-digest",
    "mail-summary"]) but the subagent may have only invoked read_skill("search-web").
    After the fix, SubagentTrace.skill_names must contain only "search-web" —
    the skills that were actually consulted — not the full assigned list.

    The test will FAIL before the fix because build_subagent_traces currently
    sets skill_names=subagent_skill_map.get(subagent_name, []) unconditionally.
    """
    from langchain_core.messages import AIMessage  # noqa: PLC0415

    from atelier.stream_loop import build_subagent_traces  # noqa: PLC0415
    from atelier.subagent_capture import SubagentMetrics  # noqa: PLC0415

    # The subagent was ASSIGNED jw-daily-digest and mail-summary …
    assigned_skills = ["jw-daily-digest", "mail-summary"]

    # … but it ONLY invoked read_skill("search-web") during the turn.
    ai_msg_with_invocation = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "tc-sw",
                "name": "read_skill",
                "args": {"skill_name": "search-web"},
                "type": "tool_call",
            }
        ],
    )

    class _MockCapture:
        def get_subagent_data(self, ns_id: str) -> SubagentMetrics:
            return SubagentMetrics(
                messages=[ai_msg_with_invocation],
                tool_calls=2,
                tool_errors=0,
            )

    traces = build_subagent_traces(
        capture=_MockCapture(),
        ns_to_name={"ns-1": "my-subagent"},
        subagent_skill_map={"my-subagent": assigned_skills},
        serialize_messages_fn=lambda msgs: [{"type": "ai", "content": ""}],
    )

    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"
    trace = traces[0]

    # After fix: only search-web (actually invoked) must appear
    assert "search-web" in trace.skill_names, (
        "Actually-invoked skill 'search-web' must appear in skill_names"
    )
    assert "jw-daily-digest" not in trace.skill_names, (
        "Assigned-but-not-invoked skill 'jw-daily-digest' must NOT appear in skill_names"
    )
    assert "mail-summary" not in trace.skill_names, (
        "Assigned-but-not-invoked skill 'mail-summary' must NOT appear in skill_names"
    )
