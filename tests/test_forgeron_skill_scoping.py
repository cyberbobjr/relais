"""Regression tests for scope_messages_to_skill — cross-contamination guard.

When a conversation uses multiple skills, ToolMessage entries from
unrelated skills must be filtered out so the LLM only sees observations
relevant to the target skill.
"""

from __future__ import annotations

import pytest

from forgeron.skill_editor import scope_messages_to_skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ai_msg(tool_calls: list[dict] | None = None, content: str = "") -> dict:
    msg: dict = {"type": "ai", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_call(name: str, skill: str, call_id: str) -> dict:
    return {"id": call_id, "name": name, "args": {"skill_name": skill}}


def _tool_msg(call_id: str, content: str = "result") -> dict:
    return {"type": "tool", "tool_call_id": call_id, "content": content}


def _human(content: str) -> dict:
    return {"type": "human", "content": content}


# ---------------------------------------------------------------------------
# Test 1 — Only relevant ToolMessage entries are kept
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_keeps_only_relevant_tool_results() -> None:
    """ToolMessage entries for a different skill must be stripped out."""
    messages = [
        _human("Send an email and search the web."),
        _ai_msg(tool_calls=[
            _tool_call("read_skill", "mail-agent", "tc-mail"),
            _tool_call("read_skill", "search-web", "tc-search"),
        ]),
        _tool_msg("tc-mail", "mail-agent skill content"),
        _tool_msg("tc-search", "search-web skill content"),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    # The mail-agent tool result must be present
    mail_results = [m for m in scoped if m.get("tool_call_id") == "tc-mail"]
    assert len(mail_results) == 1, "mail-agent ToolMessage must be kept"

    # The search-web tool result must be absent
    search_results = [m for m in scoped if m.get("tool_call_id") == "tc-search"]
    assert len(search_results) == 0, "search-web ToolMessage must be filtered out"


# ---------------------------------------------------------------------------
# Test 2 — Human and AI messages are always kept
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_always_keeps_human_and_ai_messages() -> None:
    """HumanMessage and AIMessage are never filtered, regardless of skill."""
    messages = [
        _human("Do something."),
        _ai_msg(tool_calls=[_tool_call("read_skill", "other-skill", "tc-other")]),
        _tool_msg("tc-other", "other skill content"),
        _ai_msg(content="Done."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    human_msgs = [m for m in scoped if m.get("type") == "human"]
    ai_msgs = [m for m in scoped if m.get("type") == "ai"]

    assert len(human_msgs) == 1, "Human messages must always be kept"
    assert len(ai_msgs) == 2, "AI messages must always be kept"


# ---------------------------------------------------------------------------
# Test 3 — Falls back to full list when filtered result < 3 messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_falls_back_when_too_few_messages() -> None:
    """When scoping would leave < 3 messages, the full list is returned as fallback."""
    # Only 2 messages, no tool calls — scoping yields < 3
    messages = [
        _human("Hi."),
        _ai_msg(content="Hello."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    assert scoped == messages, "Full list returned as fallback when result < 3 messages"


# ---------------------------------------------------------------------------
# Test 4 — Empty input returns empty output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_empty_input() -> None:
    """Empty messages_raw returns an empty list."""
    assert scope_messages_to_skill([], "mail-agent") == []


# ---------------------------------------------------------------------------
# Test 5 — No relevant tool calls → no ToolMessage filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_no_read_skill_calls_keeps_all() -> None:
    """When there are no read_skill calls for the target skill, no ToolMessages
    match the relevant set, so the filter keeps all messages (fallback path)."""
    messages = [
        _human("Query 1."),
        _ai_msg(tool_calls=[_tool_call("read_skill", "search-web", "tc-sw")]),
        _tool_msg("tc-sw", "search result"),
        _ai_msg(content="Here is the answer."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    # No read_skill call for mail-agent → relevant_tool_call_ids is empty
    # → ToolMessages are not filtered (condition: `tc_id not in relevant_tool_call_ids`
    #   only applies when the relevant set is non-empty)
    assert len(scoped) == len(messages), (
        "All messages kept when no read_skill calls reference the target skill"
    )


# ---------------------------------------------------------------------------
# Test 6 — String args in tool_calls are handled
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_handles_string_args() -> None:
    """Tool calls with args as a JSON string (not dict) are handled correctly."""
    messages = [
        _human("Send mail."),
        {
            "type": "ai",
            "content": "",
            "tool_calls": [
                {"id": "tc-mail", "name": "read_skill", "args": '{"skill_name": "mail-agent"}'},
                {"id": "tc-other", "name": "read_skill", "args": '{"skill_name": "other-skill"}'},
            ],
        },
        _tool_msg("tc-mail", "mail-agent content"),
        _tool_msg("tc-other", "other content"),
        _ai_msg(content="Done."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    mail_results = [m for m in scoped if m.get("tool_call_id") == "tc-mail"]
    other_results = [m for m in scoped if m.get("tool_call_id") == "tc-other"]

    assert len(mail_results) == 1, "mail-agent ToolMessage must be kept"
    assert len(other_results) == 0, "other-skill ToolMessage must be filtered out"


# ---------------------------------------------------------------------------
# Test 7 — Multiple turns, skill appears only in second turn
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_multi_turn_skill_in_second_turn() -> None:
    """Scoping correctly handles multi-turn conversations where the target
    skill is only referenced in a later turn."""
    messages = [
        _human("First task."),
        _ai_msg(tool_calls=[_tool_call("read_skill", "search-web", "tc-sw-1")]),
        _tool_msg("tc-sw-1", "search result 1"),
        _human("Now send an email."),
        _ai_msg(tool_calls=[_tool_call("read_skill", "mail-agent", "tc-mail-1")]),
        _tool_msg("tc-mail-1", "mail-agent skill content"),
        _ai_msg(content="Email sent."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    sw_results = [m for m in scoped if m.get("tool_call_id") == "tc-sw-1"]
    mail_results = [m for m in scoped if m.get("tool_call_id") == "tc-mail-1"]

    assert len(sw_results) == 0, "search-web ToolMessage must be filtered"
    assert len(mail_results) == 1, "mail-agent ToolMessage must be kept"
    # Human and AI messages are still present
    human_msgs = [m for m in scoped if m.get("type") == "human"]
    assert len(human_msgs) == 2, "Both human messages must be kept"
