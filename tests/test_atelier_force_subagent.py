"""Tests for Atelier force_subagent routing (Forgeron correction pipeline).

Covers:
- Happy path: force_subagent present and found in registry
  → AgentExecutor receives only the forced subagent + deterministic delegation prompt
- Fallback path: force_subagent present but not found in registry
  → AgentExecutor receives the original full subagent list (normal agent flow)
- Normal path: no force_subagent in context
  → AgentExecutor receives the full subagent list unmodified
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from common.contexts import CTX_FORGERON, CTX_PORTAIL
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_TASK
from atelier.agent_executor import AgentResult


# ---------------------------------------------------------------------------
# Helpers — reuse the helpers already defined in test_atelier.py
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "Please create a skill for this",
    channel: str = "discord",
    context: dict | None = None,
) -> Envelope:
    return Envelope(
        content=content,
        sender_id="discord:123456789",
        channel=channel,
        session_id="sess-force-subagent",
        correlation_id="corr-force-001",
        context=context or {},
        action=ACTION_MESSAGE_TASK,
    )


def _make_redis_mock() -> AsyncMock:
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=None)
    redis_conn.publish = AsyncMock()
    return redis_conn


def _make_xreadgroup_result(envelope: Envelope) -> list:
    message_id = "1234567890-0"
    data = {"payload": envelope.to_json()}
    return [("relais:tasks", [(message_id, data)])]


def _default_profile_mock() -> MagicMock:
    m = MagicMock()
    m.model = "claude-opus-4-5"
    m.max_turns = 10
    return m


def _make_subagent_spec(name: str) -> dict:
    """Return a minimal deepagents-compatible subagent spec dict."""
    return {
        "name": name,
        "description": f"Test subagent {name}",
        "system_prompt": f"You are {name}.",
    }


def _make_atelier():
    from atelier.main import Atelier

    profile_mock = _default_profile_mock()
    profiles_map = {"default": profile_mock}
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = MagicMock()

    rv_patches = {
        "atelier.main.load_profiles": profiles_map,
        "atelier.main.load_for_sdk": {},
        "atelier.main.resolve_profile": profile_mock,
    }
    new_patches = {
        "atelier.main.AsyncSqliteSaver": mock_saver_cls,
    }
    active: dict = {}
    for target, retval in rv_patches.items():
        p = patch(target, return_value=retval)
        active[target] = p.start()
    for target, new_val in new_patches.items():
        p = patch(target, new=new_val)
        active[target] = p.start()
    try:
        atelier = Atelier()
    finally:
        for p_obj in active.values():
            try:
                p_obj.stop()
            except RuntimeError:
                pass
    return atelier


def _common_patches(atelier, redis_conn, envelope, mock_executor_instance):
    """Return a context-manager stack that wires all the standard mocks."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _stack():
        with patch("atelier.main.AgentExecutor") as MockExecutor:
            MockExecutor.return_value = mock_executor_instance

            with patch("atelier.main.McpSessionManager") as MockMcpMgr:
                mock_mgr = AsyncMock()
                mock_mgr.start_all = AsyncMock()
                MockMcpMgr.return_value = mock_mgr

                with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                yield MockExecutor

    return _stack()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_force_subagent_found_routes_to_named_subagent() -> None:
    """When force_subagent is set and found in registry, AgentExecutor receives
    only that subagent and a deterministic delegation prompt."""
    atelier = _make_atelier()

    # Build envelope with CTX_FORGERON force_subagent stamp
    envelope = _make_envelope(context={
        CTX_PORTAIL: {"user_record": {}, "llm_profile": "default"},
        CTX_FORGERON: {
            "force_subagent": "skill-designer",
            "corrected_behavior": "Use plain text",
        },
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    # Registry returns two subagents including skill-designer
    skill_designer_spec = _make_subagent_spec("skill-designer")
    other_spec = _make_subagent_spec("other-agent")
    mock_registry = MagicMock()
    mock_registry.specs_for_user.return_value = [skill_designer_spec, other_spec]
    mock_registry.delegation_prompt_for_user.return_value = "normal delegation"
    atelier._subagent_registry = mock_registry

    mock_executor_instance = AsyncMock()
    mock_executor_instance.execute = AsyncMock(
        return_value=AgentResult(reply_text="skill created", messages_raw=[])
    )

    async with _common_patches(atelier, redis_conn, envelope, mock_executor_instance) as MockExecutor:
        try:
            await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
        except asyncio.CancelledError:
            pass

    # AgentExecutor must have been instantiated with only the forced subagent
    _, kwargs = MockExecutor.call_args
    assert kwargs["subagents"] == [skill_designer_spec]
    # Delegation prompt must mention "skill-designer" explicitly
    assert "skill-designer" in kwargs["delegation_prompt"]
    assert "MUST" in kwargs["delegation_prompt"] or "must" in kwargs["delegation_prompt"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_force_subagent_not_found_falls_back_to_normal_agent() -> None:
    """When force_subagent names a subagent NOT in the filtered list,
    AgentExecutor receives the original full subagent list (graceful fallback)."""
    atelier = _make_atelier()

    envelope = _make_envelope(context={
        CTX_PORTAIL: {"user_record": {}, "llm_profile": "default"},
        CTX_FORGERON: {"force_subagent": "nonexistent-agent"},
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    # Registry does NOT contain "nonexistent-agent"
    other_spec = _make_subagent_spec("some-other-agent")
    mock_registry = MagicMock()
    mock_registry.specs_for_user.return_value = [other_spec]
    mock_registry.delegation_prompt_for_user.return_value = "normal delegation"
    atelier._subagent_registry = mock_registry

    mock_executor_instance = AsyncMock()
    mock_executor_instance.execute = AsyncMock(
        return_value=AgentResult(reply_text="fallback response", messages_raw=[])
    )

    async with _common_patches(atelier, redis_conn, envelope, mock_executor_instance) as MockExecutor:
        try:
            await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
        except asyncio.CancelledError:
            pass

    # Must fall back to the full list (not empty, not just forced)
    _, kwargs = MockExecutor.call_args
    assert kwargs["subagents"] == [other_spec]
    # Delegation prompt must be the original, not the force override
    assert kwargs["delegation_prompt"] == "normal delegation"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_force_subagent_leaves_subagents_unchanged() -> None:
    """Without CTX_FORGERON force_subagent, subagent list and delegation prompt
    are passed to AgentExecutor unchanged."""
    atelier = _make_atelier()

    envelope = _make_envelope(context={
        CTX_PORTAIL: {"user_record": {}, "llm_profile": "default"},
        # No CTX_FORGERON at all
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    spec_a = _make_subagent_spec("agent-a")
    spec_b = _make_subagent_spec("agent-b")
    mock_registry = MagicMock()
    mock_registry.specs_for_user.return_value = [spec_a, spec_b]
    mock_registry.delegation_prompt_for_user.return_value = "delegate to agents"
    atelier._subagent_registry = mock_registry

    mock_executor_instance = AsyncMock()
    mock_executor_instance.execute = AsyncMock(
        return_value=AgentResult(reply_text="normal reply", messages_raw=[])
    )

    async with _common_patches(atelier, redis_conn, envelope, mock_executor_instance) as MockExecutor:
        try:
            await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
        except asyncio.CancelledError:
            pass

    _, kwargs = MockExecutor.call_args
    assert kwargs["subagents"] == [spec_a, spec_b]
    assert kwargs["delegation_prompt"] == "delegate to agents"
