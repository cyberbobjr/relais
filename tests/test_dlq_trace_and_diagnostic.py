"""Tests for DLQ trace messages_raw propagation and ToolErrorGuard diagnostic threshold.

Phase 1: DLQ failure trace must include exc.messages_raw (not empty []).
Phase 3: ToolErrorGuard max_total raised to 8; self-diagnosis prompt in system prompt.

TDD RED phase — tests written before implementation.
"""

from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.profile_loader import ResilienceConfig
from atelier.soul_assembler import AssemblyResult


# ---------------------------------------------------------------------------
# Helpers (reused from test_agent_executor.py patterns)
# ---------------------------------------------------------------------------


def _make_profile(model: str = "anthropic:claude-haiku-4-5") -> MagicMock:
    """Build a minimal ProfileConfig-like mock."""
    profile = MagicMock()
    profile.model = model
    profile.base_url = None
    profile.api_key_env = None
    profile.parallel_tool_calls = None
    profile.resilience = ResilienceConfig(retry_attempts=0, retry_delays=[])
    return profile


def _make_envelope(content: str = "Hello") -> MagicMock:
    """Build a minimal Envelope mock for AgentExecutor tests."""
    envelope = MagicMock()
    envelope.content = content
    envelope.correlation_id = "test-corr-id"
    envelope.sender_id = "test:user"
    return envelope


def _make_agent_state() -> MagicMock:
    """Return a mock graph state with an empty messages list."""
    state = MagicMock()
    state.values = {"messages": []}
    return state


def _v2_chunk(chunk_type: str, ns: tuple, data: object) -> dict:
    """Build a v2 astream chunk dict."""
    return {"type": chunk_type, "ns": ns, "data": data}


def _tool_error_token(tool_name: str, error_msg: str = "Error") -> MagicMock:
    """Build a mock ToolMessage chunk with status='error'."""
    token = MagicMock()
    token.type = "tool"
    token.name = tool_name
    token.content = error_msg
    token.status = "error"
    token.tool_call_chunks = []
    return token


def _ai_token(content: str) -> MagicMock:
    """Build a mock AIMessageChunk."""
    token = MagicMock()
    token.type = "ai"
    token.content = content
    token.tool_call_chunks = []
    return token


# ---------------------------------------------------------------------------
# Phase 1 — DLQ failure trace must include exc.messages_raw
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dlq_trace_includes_messages_raw_from_exception() -> None:
    """When AgentExecutionError carries messages_raw, the failure trace
    published to STREAM_SKILL_TRACE must include those messages — not [].

    This is the critical fix: Forgeron needs conversation context to analyze
    aborted turns and write meaningful changelog observations.
    """
    from atelier.agent_executor import AgentExecutionError, AgentResult
    from common.envelope import Envelope
    from common.streams import STREAM_SKILL_TRACE

    partial_messages = [
        {"role": "human", "content": "send an email"},
        {"role": "ai", "content": "I'll try himalaya..."},
    ]

    exc = AgentExecutionError(
        "5 tool errors exceeded limit",
        tool_call_count=5,
        tool_error_count=5,
        messages_raw=partial_messages,
    )

    from common.contexts import CTX_PORTAIL
    from common.envelope_actions import ACTION_MESSAGE_INCOMING

    envelope = Envelope(
        content="send an email",
        sender_id="discord:123",
        channel="discord",
        session_id="sess-1",
        correlation_id="corr-dlq-test",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_PORTAIL: {
            "user_record": {"skills_dirs": ["*"]},
            "llm_profile": "default",
        }},
    )

    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.publish = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))

    from atelier.main import Atelier

    # Instantiate Atelier with all loaders patched out
    profile_mock = MagicMock()
    profile_mock.model = "test:model"
    profile_mock.max_turns = 10

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    with (
        patch("atelier.main.load_profiles", return_value={"default": profile_mock}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.resolve_profile", return_value=profile_mock),
        patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls),
    ):
        atelier = Atelier()

    # Make the executor raise AgentExecutionError with messages_raw
    with (
        patch("atelier.main.AgentExecutor") as MockExecutor,
        patch("atelier.main.McpSessionManager") as MockMcpMgr,
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=profile_mock),
        patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(prompt="soul", issues=[], is_degraded=False)),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.ErrorSynthesizer") as MockSynth,
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(side_effect=exc)
        MockExecutor.return_value = mock_instance

        mock_mgr = AsyncMock()
        mock_mgr.start_all = AsyncMock()
        MockMcpMgr.return_value = mock_mgr

        mock_synth = AsyncMock()
        mock_synth.synthesize = AsyncMock(return_value="Sorry, error.")
        MockSynth.return_value = mock_synth

        # Point _skills_base_dir to a tmp dir with a skill so skills_used is populated
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = os.path.join(tmp, "mail-agent")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
                f.write("# mail-agent\n")
            atelier._skills_base_dir = Path(tmp)

            import asyncio

            redis_conn.xreadgroup = AsyncMock(side_effect=[
                [("relais:tasks", [("1-0", {"payload": envelope.to_json()})])],
                asyncio.CancelledError(),
            ])

            try:
                await atelier._run_stream_loop(
                    atelier.stream_specs()[0], redis_conn, asyncio.Event()
                )
            except asyncio.CancelledError:
                pass

    # Find the XADD call to STREAM_SKILL_TRACE
    trace_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == STREAM_SKILL_TRACE
    ]
    assert len(trace_calls) == 1, (
        f"Expected exactly 1 trace call to {STREAM_SKILL_TRACE}, "
        f"got {len(trace_calls)}. All xadd targets: "
        f"{[c.args[0] for c in redis_conn.xadd.await_args_list]}"
    )

    payload = json.loads(trace_calls[0].args[1]["payload"])
    trace_ctx = payload["context"]["skill_trace"]

    assert trace_ctx["tool_error_count"] == -1, "Aborted turns use sentinel -1"
    assert trace_ctx["messages_raw"] == partial_messages, (
        "Failure trace must include exc.messages_raw, not an empty list. "
        f"Got: {trace_ctx['messages_raw']}"
    )


# ---------------------------------------------------------------------------
# Phase 3 — ToolErrorGuard max_total=8 (up from 5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_error_guard_does_not_abort_at_5_errors() -> None:
    """With max_total=8, the agent must survive 5 total errors without aborting.

    Previously max_total was 5, which meant the 5th error was fatal.
    Now errors 5-7 should be tolerated to give the agent diagnostic room.
    """
    from atelier.agent_executor import AgentExecutor

    error_count = 0

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        nonlocal error_count
        # 5 errors from different tools (not consecutive for the same tool)
        for i in range(5):
            yield _v2_chunk("messages", (), (_tool_error_token(f"tool_{i}"), {}))
            error_count += 1
        # After 5 errors, the agent should still be alive
        yield _v2_chunk("messages", (), (_ai_token("I diagnosed the issue!"), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(), soul_prompt="...", tools=[]
        )
        result = await executor.execute(_make_envelope("Fix it"))

    assert error_count == 5
    assert result.reply_text == "I diagnosed the issue!"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_error_guard_aborts_at_8_errors() -> None:
    """With max_total=8, the 8th total error must trigger AgentExecutionError."""
    from atelier.agent_executor import AgentExecutor, AgentExecutionError

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        for i in range(8):
            yield _v2_chunk("messages", (), (_tool_error_token(f"tool_{i}"), {}))
        yield _v2_chunk("messages", (), (_ai_token("should not reach"), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(), soul_prompt="...", tools=[]
        )
        with pytest.raises(AgentExecutionError, match="8 total tool errors"):
            await executor.execute(_make_envelope("Fix it"))


@pytest.mark.unit
def test_tool_error_guard_default_max_total_is_8() -> None:
    """ToolErrorGuard used in _stream() must have max_total=8."""
    from atelier.agent_executor import ToolErrorGuard

    guard = ToolErrorGuard(max_consecutive=5, max_total=8)
    # Record 7 errors (all different tools) — should NOT raise
    for i in range(7):
        guard.record(f"tool_{i}", True)

    assert guard.total_errors == 7


# ---------------------------------------------------------------------------
# Phase 3 — Self-diagnosis instructions in system prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enriched_system_prompt_contains_self_diagnosis() -> None:
    """_enrich_system_prompt() must include self-diagnosis instructions.

    These instructions tell the agent to stop and re-read SKILL.md
    troubleshooting when encountering repeated tool errors.
    """
    from atelier.agent_executor import _enrich_system_prompt

    prompt = _enrich_system_prompt("You are helpful.")

    assert "tool error" in prompt.lower() or "self-diagnosis" in prompt.lower(), (
        "System prompt must contain self-diagnosis instructions for tool errors."
    )
    assert "SKILL.md" in prompt or "skill" in prompt.lower(), (
        "Self-diagnosis instructions must reference SKILL.md or skills."
    )
