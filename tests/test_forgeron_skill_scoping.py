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
    """HumanMessage and AIMessage are always kept when the target skill is invoked."""
    messages = [
        _human("Do something."),
        _ai_msg(tool_calls=[
            _tool_call("read_skill", "mail-agent", "tc-mail"),
            _tool_call("read_skill", "other-skill", "tc-other"),
        ]),
        _tool_msg("tc-mail", "mail-agent content"),
        _tool_msg("tc-other", "other skill content"),
        _ai_msg(content="Done."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    human_msgs = [m for m in scoped if m.get("type") == "human"]
    ai_msgs = [m for m in scoped if m.get("type") == "ai"]

    assert len(human_msgs) == 1, "Human messages must always be kept"
    assert len(ai_msgs) == 2, "AI messages must always be kept"


# ---------------------------------------------------------------------------
# Test 3 — No relevant read_skill calls → scoping returns empty (not a fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_returns_empty_when_no_relevant_calls() -> None:
    """When the conversation has no read_skill calls for the target skill,
    scope_messages_to_skill must return [] instead of falling back to the full list.

    Bug: the current implementation returns the full message list as a fallback
    when fewer than 3 messages survive filtering, which pollutes the LLM prompt
    with unrelated content.  The correct behavior is to return an empty list so
    the caller can skip the LLM call altogether.
    """
    # Only 2 messages, no tool calls — no read_skill call for mail-agent
    messages = [
        _human("Hi."),
        _ai_msg(content="Hello."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    assert scoped == [], (
        "scope_messages_to_skill must return [] when no read_skill call references "
        "the target skill — fallback to full list pollutes the LLM prompt"
    )


# ---------------------------------------------------------------------------
# Test 4 — Empty input returns empty output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_empty_input() -> None:
    """Empty messages_raw returns an empty list."""
    assert scope_messages_to_skill([], "mail-agent") == []


# ---------------------------------------------------------------------------
# Test 5 — Skill not invoked → scoping returns empty (target not called at all)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_returns_empty_when_target_not_invoked() -> None:
    """When the conversation uses read_skill for OTHER skills but NOT the target,
    scope_messages_to_skill must return [] to prevent cross-contamination.

    Bug: the current implementation returns the full message list because
    relevant_tool_call_ids is empty, and the guard `if relevant_tool_call_ids`
    disables the ToolMessage filter entirely — causing search-web tool results
    to appear in the mail-agent LLM prompt.
    """
    messages = [
        _human("Query 1."),
        _ai_msg(tool_calls=[_tool_call("read_skill", "search-web", "tc-sw")]),
        _tool_msg("tc-sw", "search result"),
        _ai_msg(content="Here is the answer."),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    # mail-agent was never called via read_skill → scope must be empty
    assert scoped == [], (
        "scope_messages_to_skill must return [] when the target skill was never "
        "invoked — returning all messages leaks search-web content into the "
        "mail-agent LLM prompt (cross-contamination bug)"
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


# ---------------------------------------------------------------------------
# Test 8 — ToolMessage content referencing the skill name is a valid signal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scoping_content_based_signal() -> None:
    """A ToolMessage whose content mentions the skill name is treated as a
    relevant signal even if no explicit read_skill tool_call was made for
    that skill, while an unrelated ToolMessage is excluded.

    This verifies two things simultaneously:
    1. Content-based matching picks up the relevant ToolMessage.
    2. An unrelated ToolMessage (no read_skill call, content does not mention
       the target skill) is filtered OUT — not kept by the fallback.

    The current implementation does NOT implement content-based matching; it
    relies on tool_call ID collection from read_skill calls.  When no read_skill
    call exists, relevant_tool_call_ids is empty and the guard
    `if relevant_tool_call_ids` disables filtering, so ALL ToolMessages pass
    through (fallback behavior).  This means the unrelated ToolMessage
    ("tc-unrelated") is kept — the second assertion will FAIL.
    """
    messages = [
        _human("do something"),
        _ai_msg(content="Working on it."),  # no tool_calls — no read_skill call
        _tool_msg("tc-content-ref", "mail-agent skill content here"),  # references target skill
        _tool_msg("tc-unrelated", "completely unrelated tool output"),  # must be excluded
        _ai_msg(content="done"),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    # The ToolMessage whose content references the target skill must be kept.
    tool_msgs_in_scoped = [m for m in scoped if m.get("tool_call_id") == "tc-content-ref"]
    assert len(tool_msgs_in_scoped) == 1, (
        "The ToolMessage whose content contains the skill name must be included in scoped"
    )

    # The unrelated ToolMessage must be excluded — this assertion FAILS with the
    # current buggy fallback that keeps all messages when no read_skill calls match.
    unrelated_in_scoped = [m for m in scoped if m.get("tool_call_id") == "tc-unrelated"]
    assert len(unrelated_in_scoped) == 0, (
        "Unrelated ToolMessage must be filtered out when content-based scoping is used; "
        "the current fallback keeps it erroneously"
    )


# ---------------------------------------------------------------------------
# Test 9 — No read_skill call for target → returns []
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_messages_returns_empty_when_no_read_skill_call() -> None:
    """A conversation that uses read_skill for OTHER skills but never for the
    target returns an empty list — not the full message list.

    This is an explicit statement of the guard: if the target skill was never
    invoked, there is nothing to extract, and returning the full conversation
    would pollute the LLM prompt with completely unrelated context.
    """
    messages = [
        _human("search and email"),
        _ai_msg(tool_calls=[_tool_call("read_skill", "search-web", "tc-search-web")]),
        _tool_msg("tc-search-web", "search content"),
        _ai_msg(content="found"),
    ]

    scoped = scope_messages_to_skill(messages, "mail-agent")

    assert scoped == [], (
        "scope_messages_to_skill must return [] when the target skill (mail-agent) "
        "was never called via read_skill — got non-empty result instead"
    )
