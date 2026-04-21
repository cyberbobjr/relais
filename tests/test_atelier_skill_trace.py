"""Unit tests for subagent skill trace publication in atelier/main.py (Phase 4)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from common.envelope import Envelope
from common.streams import STREAM_SKILL_TRACE
from atelier.agent_executor import AgentResult, SubagentTrace
from atelier.soul_assembler import AssemblyResult
from tests.conftest import (
    _make_atelier,
    _make_envelope,
    _make_redis_mock,
    _make_xreadgroup_result,
    _default_profile_mock,
)


# ---------------------------------------------------------------------------
# Phase 4 RED tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_skill_trace_published_when_tool_calls_present() -> None:
    """A SubagentTrace with tool_call_count > 0 and non-empty skill_names triggers
    a separate xadd to STREAM_SKILL_TRACE beyond any parent trace."""
    atelier = _make_atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    sa_trace = SubagentTrace(
        subagent_name="mail-agent",
        skill_names=["mail-ops"],
        tool_call_count=3,
        tool_error_count=0,
        messages_raw=[{"type": "human", "content": "send mail"}],
    )
    agent_result = AgentResult(
        reply_text="done",
        messages_raw=[],
        tool_call_count=1,
        tool_error_count=0,
        subagent_traces=(sa_trace,),
    )

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=agent_result)
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            # Make skills_used empty so parent trace is skipped —
                            # we isolate the subagent trace publication
                            with patch.object(atelier._tool_policy, "resolve_skills", return_value=[]):
                                try:
                                    await atelier._run_stream_loop(
                                        atelier.stream_specs()[0], redis_conn, asyncio.Event()
                                    )
                                except asyncio.CancelledError:
                                    pass

    skill_trace_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == STREAM_SKILL_TRACE
    ]
    assert len(skill_trace_calls) == 1, (
        f"Expected 1 STREAM_SKILL_TRACE xadd for subagent, got {len(skill_trace_calls)}"
    )

    payload_json = skill_trace_calls[0].args[1]["payload"]
    published_env = Envelope.from_json(payload_json)
    from common.contexts import CTX_SKILL_TRACE
    skill_ctx = published_env.context[CTX_SKILL_TRACE]
    assert skill_ctx["skill_names"] == ["mail-ops"]
    assert skill_ctx["tool_call_count"] == 3
    assert skill_ctx["tool_error_count"] == 0
    assert skill_ctx["messages_raw"] == [{"type": "human", "content": "send mail"}]


@pytest.mark.asyncio
async def test_subagent_skill_trace_not_published_when_no_tool_calls() -> None:
    """A SubagentTrace with tool_call_count == 0 must NOT emit a STREAM_SKILL_TRACE xadd."""
    atelier = _make_atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    sa_trace_no_calls = SubagentTrace(
        subagent_name="mail-agent",
        skill_names=["mail-ops"],
        tool_call_count=0,
        tool_error_count=0,
        messages_raw=[],
    )
    agent_result = AgentResult(
        reply_text="nothing done",
        messages_raw=[],
        tool_call_count=0,
        tool_error_count=0,
        subagent_traces=(sa_trace_no_calls,),
    )

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=agent_result)
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            with patch.object(atelier._tool_policy, "resolve_skills", return_value=[]):
                                try:
                                    await atelier._run_stream_loop(
                                        atelier.stream_specs()[0], redis_conn, asyncio.Event()
                                    )
                                except asyncio.CancelledError:
                                    pass

    skill_trace_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == STREAM_SKILL_TRACE
    ]
    assert len(skill_trace_calls) == 0, (
        f"Expected 0 STREAM_SKILL_TRACE xadd when tool_call_count==0, got {len(skill_trace_calls)}"
    )


@pytest.mark.asyncio
async def test_subagent_skill_trace_not_published_when_skill_names_empty() -> None:
    """A SubagentTrace with empty skill_names must NOT emit a STREAM_SKILL_TRACE xadd."""
    atelier = _make_atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    sa_trace_no_skills = SubagentTrace(
        subagent_name="unknown-agent",
        skill_names=[],
        tool_call_count=2,
        tool_error_count=0,
        messages_raw=[],
    )
    agent_result = AgentResult(
        reply_text="did stuff",
        messages_raw=[],
        tool_call_count=2,
        tool_error_count=0,
        subagent_traces=(sa_trace_no_skills,),
    )

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=agent_result)
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            with patch.object(atelier._tool_policy, "resolve_skills", return_value=[]):
                                try:
                                    await atelier._run_stream_loop(
                                        atelier.stream_specs()[0], redis_conn, asyncio.Event()
                                    )
                                except asyncio.CancelledError:
                                    pass

    skill_trace_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == STREAM_SKILL_TRACE
    ]
    assert len(skill_trace_calls) == 0, (
        f"Expected 0 STREAM_SKILL_TRACE xadd when skill_names==[], got {len(skill_trace_calls)}"
    )
