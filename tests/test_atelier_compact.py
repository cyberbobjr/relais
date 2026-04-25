"""Tests for the /compact command — Phase 3: Atelier handler (TDD).

RED phase: all tests written before implementation.  Run with:
    pytest tests/test_atelier_compact.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from common.contexts import CTX_PORTAIL, CTX_ATELIER_CONTROL
from common.streams import STREAM_ATELIER_CONTROL, STREAM_TASKS
from common.envelope_actions import ACTION_ATELIER_COMPACT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_control_envelope(
    op: str = "compact",
    user_id: str = "usr_admin",
    session_id: str = "sess_123",
) -> Envelope:
    """Build a minimal control envelope as Commandant would publish it."""
    return Envelope(
        content="",
        sender_id="discord:42",
        channel="discord",
        session_id=session_id,
        correlation_id="corr_abc",
        action=ACTION_ATELIER_COMPACT,
        context={
            CTX_ATELIER_CONTROL: {
                "op": op,
                "user_id": user_id,
                "envelope_json": "{}",
            }
        },
    )


def _make_atelier_minimal():
    """Build a minimal Atelier instance with all heavy deps mocked."""
    from atelier.main import Atelier

    fake_profiles = {"default": MagicMock(name="default_profile", compact_keep=6)}
    fake_mcp_servers = {}
    fake_display = MagicMock(name="display_config")

    with (
        patch("atelier.main.load_profiles", return_value=fake_profiles),
        patch("atelier.main.load_for_sdk", return_value=fake_mcp_servers),
        patch("atelier.main.load_display_config", return_value=fake_display),
        patch("atelier.main.resolve_skills_dir", return_value=MagicMock()),
        patch("atelier.main.SubagentRegistry") as mock_registry_cls,
        patch("atelier.main.AsyncSqliteSaver"),
        patch("atelier.main.resolve_storage_dir", return_value=MagicMock()),
        patch("atelier.main.RedisClient"),
        patch("atelier.main.resolve_bundles_dir", return_value=MagicMock()),
    ):
        mock_registry_cls.discover.return_value = MagicMock()
        mock_registry_cls.load.return_value = MagicMock()
        atelier = Atelier()

    if not hasattr(atelier, "_config_lock"):
        atelier._config_lock = asyncio.Lock()

    return atelier


# ---------------------------------------------------------------------------
# CompactResult dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compact_result_dataclass_exists() -> None:
    """CompactResult must be importable from agent_executor."""
    from atelier.agent_executor import CompactResult  # noqa: F401


@pytest.mark.unit
def test_compact_result_fields() -> None:
    """CompactResult must have messages_before, messages_after, cutoff_index."""
    from atelier.agent_executor import CompactResult

    result = CompactResult(messages_before=10, messages_after=7, cutoff_index=3)
    assert result.messages_before == 10
    assert result.messages_after == 7
    assert result.cutoff_index == 3


@pytest.mark.unit
def test_compact_result_is_frozen() -> None:
    """CompactResult must be a frozen dataclass."""
    from atelier.agent_executor import CompactResult

    result = CompactResult(messages_before=5, messages_after=3, cutoff_index=2)
    with pytest.raises((AttributeError, TypeError)):
        result.messages_before = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AgentExecutor.compact_session() — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_returns_none_when_state_empty() -> None:
    """compact_session() returns None when checkpointer state has no messages."""
    from atelier.agent_executor import AgentExecutor

    profile = MagicMock(compact_keep=6, model="anthropic:claude-haiku-4-5-20251001")
    executor = MagicMock(spec=AgentExecutor)
    executor._agent = AsyncMock()
    executor._agent.aget_state = AsyncMock(return_value=MagicMock(values={}))
    executor._profile = profile

    # Call the real method bound to our mock
    result = await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 6)
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_returns_none_when_messages_lte_keep() -> None:
    """compact_session() returns None when message count <= compact_keep."""
    from atelier.agent_executor import AgentExecutor
    from langchain_core.messages import HumanMessage

    profile = MagicMock(compact_keep=6, model="anthropic:claude-haiku-4-5-20251001")
    executor = MagicMock(spec=AgentExecutor)
    executor._agent = AsyncMock()

    short_messages = [HumanMessage(content="hello")]
    state = MagicMock(values={"messages": short_messages})
    executor._agent.aget_state = AsyncMock(return_value=state)
    executor._profile = profile

    result = await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 6)
    assert result is None


def _make_profile_mock(**kwargs) -> MagicMock:
    """Build a MagicMock profile that passes _resolve_profile_model cleanly."""
    defaults = dict(
        compact_keep=6,
        model="anthropic:claude-haiku-4-5-20251001",
        base_url=None,
        api_key_env=None,
        parallel_tool_calls=None,
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_returns_compact_result_on_success() -> None:
    """compact_session() returns CompactResult with correct counts when messages > keep."""
    from atelier.agent_executor import AgentExecutor, CompactResult
    from langchain_core.messages import HumanMessage, AIMessage

    profile = _make_profile_mock(compact_keep=4)
    executor = MagicMock(spec=AgentExecutor)
    executor._profile = profile
    executor._agent = AsyncMock()

    messages = [HumanMessage(content=f"msg{i}") for i in range(10)]
    state = MagicMock(values={"messages": messages})
    executor._agent.aget_state = AsyncMock(return_value=state)
    executor._agent.aupdate_state = AsyncMock()

    fake_summary_msg = AIMessage(content="Summary of earlier conversation.")

    with (
        patch("atelier.agent_executor._DeepAgentsSummarizationMiddleware") as mock_mw_cls,
    ):
        mock_mw = MagicMock()
        mock_mw._acreate_summary = AsyncMock(return_value="Summary text")
        mock_mw._build_new_messages_with_path = MagicMock(return_value=[fake_summary_msg])
        mock_mw_cls.return_value = mock_mw

        result = await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 4)

    assert isinstance(result, CompactResult)
    assert result.messages_before == 10
    assert result.cutoff_index == 6  # 10 - 4
    assert result.messages_after == 5  # 4 kept + 1 summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_calls_aupdate_state_with_event() -> None:
    """compact_session() must call aupdate_state with a _summarization_event dict."""
    from atelier.agent_executor import AgentExecutor
    from langchain_core.messages import HumanMessage, AIMessage

    profile = _make_profile_mock(compact_keep=3)
    executor = MagicMock(spec=AgentExecutor)
    executor._profile = profile
    executor._agent = AsyncMock()

    messages = [HumanMessage(content=f"msg{i}") for i in range(8)]
    state = MagicMock(values={"messages": messages})
    executor._agent.aget_state = AsyncMock(return_value=state)
    executor._agent.aupdate_state = AsyncMock()

    fake_summary_msg = AIMessage(content="Summary.")

    with patch("atelier.agent_executor._DeepAgentsSummarizationMiddleware") as mock_mw_cls:
        mock_mw = MagicMock()
        mock_mw._acreate_summary = AsyncMock(return_value="Summary text")
        mock_mw._build_new_messages_with_path = MagicMock(return_value=[fake_summary_msg])
        mock_mw_cls.return_value = mock_mw

        await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 3)

    executor._agent.aupdate_state.assert_called_once()
    _call_args = executor._agent.aupdate_state.call_args
    state_update = _call_args[0][1]  # second positional arg
    assert "_summarization_event" in state_update
    event = state_update["_summarization_event"]
    assert "cutoff_index" in event
    assert "summary_message" in event
    assert event["file_path"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_returns_none_on_exception() -> None:
    """compact_session() returns None (non-fatal) when an exception occurs."""
    from atelier.agent_executor import AgentExecutor

    profile = MagicMock(compact_keep=6, model="anthropic:claude-haiku-4-5-20251001")
    executor = MagicMock(spec=AgentExecutor)
    executor._profile = profile
    executor._agent = AsyncMock()
    executor._agent.aget_state = AsyncMock(side_effect=RuntimeError("db error"))

    result = await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 6)
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_session_reraises_cancelled_error() -> None:
    """compact_session() must re-raise asyncio.CancelledError — never swallow it."""
    from atelier.agent_executor import AgentExecutor

    profile = MagicMock(compact_keep=6, model="anthropic:claude-haiku-4-5-20251001")
    executor = MagicMock(spec=AgentExecutor)
    executor._profile = profile
    executor._agent = AsyncMock()
    executor._agent.aget_state = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await AgentExecutor.compact_session(executor, "sess_1", "usr_1", 6)


# ---------------------------------------------------------------------------
# Atelier.stream_specs() — must include STREAM_ATELIER_CONTROL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_specs_includes_control_stream() -> None:
    """stream_specs() must return a spec for STREAM_ATELIER_CONTROL."""
    atelier = _make_atelier_minimal()
    specs = atelier.stream_specs()
    stream_names = [s.stream for s in specs]
    assert STREAM_ATELIER_CONTROL in stream_names, (
        f"Expected {STREAM_ATELIER_CONTROL!r} in stream_specs, got {stream_names}"
    )


@pytest.mark.unit
def test_stream_specs_still_includes_tasks_stream() -> None:
    """stream_specs() must still include the original STREAM_TASKS spec."""
    atelier = _make_atelier_minimal()
    specs = atelier.stream_specs()
    stream_names = [s.stream for s in specs]
    assert STREAM_TASKS in stream_names


@pytest.mark.unit
def test_stream_specs_has_two_entries() -> None:
    """stream_specs() must return exactly two StreamSpec entries."""
    atelier = _make_atelier_minimal()
    assert len(atelier.stream_specs()) == 2


# ---------------------------------------------------------------------------
# Atelier._handle_control() — routing and reply
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_control_calls_compact_session() -> None:
    """_handle_control() must call compact_session() for op='compact'."""
    atelier = _make_atelier_minimal()

    envelope = _make_control_envelope(op="compact", user_id="usr_admin", session_id="sess_xyz")
    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock()

    fake_profile = MagicMock(compact_keep=6)
    atelier._profiles = {"default": fake_profile}
    atelier._checkpointer = MagicMock()

    mock_executor_instance = AsyncMock()
    mock_executor_instance.compact_session = AsyncMock(return_value=None)

    with patch("atelier.main.AgentExecutor", return_value=mock_executor_instance):
        await atelier._handle_control(envelope, redis_conn)

    mock_executor_instance.compact_session.assert_called_once()
    call_kwargs = mock_executor_instance.compact_session.call_args
    all_args = str(call_kwargs)
    assert "sess_xyz" in all_args
    assert "usr_admin" in all_args


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_control_publishes_reply_on_success() -> None:
    """_handle_control() must publish a reply envelope to outgoing_pending on success."""
    from atelier.agent_executor import CompactResult

    atelier = _make_atelier_minimal()

    compact_result = CompactResult(messages_before=10, messages_after=7, cutoff_index=3)
    mock_executor_instance = AsyncMock()
    mock_executor_instance.compact_session = AsyncMock(return_value=compact_result)

    envelope = _make_control_envelope()
    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock()

    fake_profile = MagicMock(compact_keep=6)
    atelier._profiles = {"default": fake_profile}
    atelier._checkpointer = MagicMock()

    with patch("atelier.main.AgentExecutor", return_value=mock_executor_instance):
        await atelier._handle_control(envelope, redis_conn)

    redis_conn.xadd.assert_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_control_returns_true() -> None:
    """_handle_control() must return True to ACK the control message."""
    atelier = _make_atelier_minimal()

    mock_executor_instance = AsyncMock()
    mock_executor_instance.compact_session = AsyncMock(return_value=None)

    envelope = _make_control_envelope()
    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock()

    fake_profile = MagicMock(compact_keep=6)
    atelier._profiles = {"default": fake_profile}
    atelier._checkpointer = MagicMock()

    with patch("atelier.main.AgentExecutor", return_value=mock_executor_instance):
        result = await atelier._handle_control(envelope, redis_conn)

    assert result is True
