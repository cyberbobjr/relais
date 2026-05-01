"""Unit tests for the SDK-based atelier.main."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL, CTX_ATELIER
from common.envelope_actions import ACTION_MESSAGE_INCOMING
from atelier.agent_executor import AgentExecutionError, AgentResult
from atelier.soul_assembler import AssemblyResult
from tests.conftest import (
    _make_envelope,
    _make_redis_mock,
    _make_xreadgroup_result,
    _default_profile_mock,
)


def _make_atelier_with_patches(extra_patches: dict | None = None):
    """Instantiate Atelier with __init__-time loaders patched out.

    Patches load_profiles, load_for_sdk, and resolve_profile before the
    Atelier() call so that __init__ does not hit the filesystem.

    Args:
        extra_patches: Optional dict of additional patch targets → return values
            to apply inside the returned context (applied before Atelier()).

    Returns:
        Tuple of (atelier_instance, dict_of_patch_objects).
    """
    from atelier.main import Atelier

    profile_mock = _default_profile_mock()
    profiles_map = {"default": profile_mock}

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    # Patches applied with return_value= (for functions called at __init__ time)
    rv_patches = {
        "atelier.main.load_profiles": profiles_map,
        "atelier.main.load_for_sdk": {},
        "atelier.main.resolve_profile": profile_mock,
    }
    # Patches applied with new= (for class-level objects where identity matters)
    new_patches: dict = {
        "atelier.main.AsyncSqliteSaver": mock_saver_cls,
    }
    if extra_patches:
        for k, v in extra_patches.items():
            if k in new_patches:
                new_patches[k] = v
            else:
                rv_patches[k] = v

    active: dict = {}
    for target, retval in rv_patches.items():
        p = patch(target, return_value=retval)
        active[target] = p.start()
    for target, new_val in new_patches.items():
        p = patch(target, new=new_val)
        active[target] = p.start()

    try:
        atelier = Atelier()
    except Exception:
        for p in active.values():
            p.stop()
        raise

    # Stop the startup patches; callers can apply their own patches for
    # _handle_message / _process_stream execution.
    for p_obj in active.values():
        try:
            p_obj.stop()
        except RuntimeError:
            pass  # already stopped

    return atelier


# ---------------------------------------------------------------------------
# main.py — SDK-based XACK conditional behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xack_sent_after_successful_sdk_call() -> None:
    """XACK is sent after SDKExecutor.execute() succeeds."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="Response from SDK", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_xack_sent_and_dlq_on_sdk_execution_error() -> None:
    """XACK is sent and DLQ receives the message when SDKExecutionError is raised."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(
            side_effect=AgentExecutionError("Agent failed")
        )
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_awaited_once()

    dlq_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:tasks:failed"
    ]
    assert len(dlq_calls) == 1


@pytest.mark.asyncio
async def test_xack_not_sent_on_generic_exception() -> None:
    """XACK is NOT sent when a generic RuntimeError occurs — message stays in PEL."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(
            side_effect=RuntimeError("unexpected failure")
        )
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_resolves_profile_from_envelope_metadata() -> None:
    """_handle_message() resolves the LLM profile from user_record.llm_profile in envelope metadata."""
    fast_profile = MagicMock(model="claude-haiku-4-5", max_turns=5)
    default_profile = _default_profile_mock()
    profiles = {"fast": fast_profile, "default": default_profile}

    atelier = _make_atelier_with_patches({
        "atelier.main.load_profiles": profiles,
        "atelier.main.resolve_profile": default_profile,
    })

    # Override the stored _profiles to match what the test expects
    atelier._profiles = profiles

    envelope = _make_envelope(context={CTX_PORTAIL: {"llm_profile": "fast", "user_record": {}}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=fast_profile) as mock_resolve:
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    mock_resolve.assert_called_once_with(profiles, "fast")



@pytest.mark.asyncio
async def test_handle_message_injects_user_message_in_response_metadata() -> None:
    """Response envelope metadata contains 'user_message' = original envelope.content."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(content="What is the weather?")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="Sunny and warm.", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    # Find the outgoing stream XADD
    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1

    payload_json = outgoing_calls[0].args[1]["payload"]
    response_data = json.loads(payload_json)
    assert response_data["context"]["atelier"]["user_message"] == "What is the weather?"


@pytest.mark.asyncio
async def test_handle_message_acks_on_success() -> None:
    """_handle_message() returns True (ACK) on successful SDK execution."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_signal_published_for_telegram_channel() -> None:
    """handle_message publishes relais:streaming:start:telegram for telegram channel.

    When the envelope channel is 'telegram' (a STREAMING_CAPABLE_CHANNELS
    member), _handle_message must call redis.publish with the streaming-start
    signal before invoking SDK execute.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram", context={CTX_AIGUILLEUR: {}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    # Verify redis.publish was called with the streaming-start signal
    publish_calls = [
        c for c in redis_conn.publish.await_args_list
        if "relais:streaming:start:telegram" in str(c)
    ]
    assert len(publish_calls) >= 1



@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_publisher_finalize_called_after_sdk_execution() -> None:
    """StreamPublisher.finalize() is called after SDK execute() for streaming channels.

    For telegram (a STREAMING_CAPABLE_CHANNELS member), _handle_message must
    call stream_pub.finalize() once after sdk_executor.execute() completes.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram", context={CTX_AIGUILLEUR: {}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="final reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    mock_pub.finalize.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_publish_payload_is_full_envelope_json() -> None:
    """_handle_message must publish the full envelope JSON to relais:streaming:start:telegram.

    The Pub/Sub payload must be a valid JSON string deserializable as an
    Envelope (containing at least correlation_id), NOT a bare UUID string.
    Publishing a bare correlation_id causes json.loads() to raise
    JSONDecodeError in the Aiguilleur subscriber.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram", context={CTX_AIGUILLEUR: {}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    publish_calls = [
        c for c in redis_conn.publish.await_args_list
        if c.args[0] == "relais:streaming:start:telegram"
    ]
    assert len(publish_calls) == 1, "Expected exactly one publish to relais:streaming:start:telegram"

    payload = publish_calls[0].args[1]
    # Must be valid JSON (not a bare UUID string)
    parsed = json.loads(payload)
    assert parsed.get("correlation_id") == envelope.correlation_id, (
        f"Expected correlation_id '{envelope.correlation_id}' in JSON payload, "
        f"got: {parsed}"
    )


# ---------------------------------------------------------------------------
# Streaming deduplication — streamed metadata flag (Option C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_streamed_flag_set_in_metadata_for_streaming_channel() -> None:
    """_handle_message() sets context["atelier"]["streamed"]=True for streaming-capable channels.

    When the envelope channel is "telegram" (a STREAMING_CAPABLE_CHANNEL), the
    response envelope published to relais:messages:outgoing:telegram must carry
    context["atelier"]["streamed"] == True so the Aiguilleur can edit instead of re-send.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram", context={CTX_AIGUILLEUR: {}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="Streamed reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1
    payload_json = outgoing_calls[0].args[1]["payload"]
    response_data = json.loads(payload_json)
    assert response_data["context"]["atelier"].get("streamed") is True



# ---------------------------------------------------------------------------
# Unified streaming — Atelier always streams regardless of CTX_AIGUILLEUR flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_signal_always_published_without_flag() -> None:
    """relais:streaming:start:{channel} is published even when CTX_AIGUILLEUR has no streaming flag.

    After the streaming uniformization, Atelier must always publish the start
    signal regardless of the 'streaming' field in context["aiguilleur"].
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="discord")  # no streaming flag
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(
            reply_text="reply", messages_raw=[], tool_call_count=0,
            tool_error_count=0, subagent_traces=(),
        ))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    publish_calls = [
        c for c in redis_conn.publish.await_args_list
        if "relais:streaming:start:discord" in str(c)
    ]
    assert len(publish_calls) >= 1, "Expected streaming:start signal even without streaming flag"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_always_called_without_flag() -> None:
    """StreamPublisher.finalize() is always called even when CTX_AIGUILLEUR has no streaming flag."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="discord")  # no streaming flag
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(
            reply_text="reply", messages_raw=[], tool_call_count=0,
            tool_error_count=0, subagent_traces=(),
        ))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    mock_pub.finalize.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streamed_flag_always_set_without_flag() -> None:
    """atelier_ctx['streamed'] is always True even when CTX_AIGUILLEUR has no streaming flag."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="discord")  # no streaming flag
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(
            reply_text="reply", messages_raw=[], tool_call_count=0,
            tool_error_count=0, subagent_traces=(),
        ))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1
    payload_json = outgoing_calls[0].args[1]["payload"]
    response_data = json.loads(payload_json)
    assert response_data["context"]["atelier"].get("streamed") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_callback_always_non_none() -> None:
    """AgentExecutor.execute() always receives a non-None stream_callback.

    After the streaming uniformization, Atelier must always pass push_chunk
    as stream_callback regardless of the 'streaming' field in the envelope.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="discord")  # no streaming flag
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    execute_kwargs: list[dict] = []

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()

        async def capture_execute(**kwargs: object) -> AgentResult:
            execute_kwargs.append(kwargs)
            return AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=())

        mock_instance.execute = capture_execute
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.StreamPublisher") as MockStreamPublisher:
                    mock_pub = AsyncMock()
                    mock_pub.push_chunk = AsyncMock()
                    mock_pub.finalize = AsyncMock()
                    MockStreamPublisher.return_value = mock_pub

                    with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                        with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    assert execute_kwargs, "AgentExecutor.execute should have been called"
    assert execute_kwargs[0].get("stream_callback") is not None, (
        "stream_callback must always be non-None after streaming uniformization"
    )


# ---------------------------------------------------------------------------
# Phase 4 — user_role forwarded to assemble_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_stream_passes_role_prompt_path_to_assemble_system_prompt() -> None:
    """_process_stream forwards user_record['role_prompt_path'] to assemble_system_prompt.

    When Portail has stamped a role_prompt_path into user_record, Atelier
    must forward it as the role_prompt_path keyword argument so the role
    overlay is included in the assembled system prompt.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(context={CTX_PORTAIL: {
        "llm_profile": "default",
        "user_record": {"role_prompt_path": "roles/admin.md"},
    }})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False))

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", mock_sp):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("role_prompt_path") == "roles/admin.md"


@pytest.mark.asyncio
async def test_process_stream_role_prompt_path_none_when_absent_in_user_record() -> None:
    """_process_stream passes role_prompt_path=None when user_record has no role_prompt_path.

    When the envelope carries no role_prompt_path (e.g. unknown user), the call
    must still succeed with role_prompt_path=None rather than raising KeyError.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(context={CTX_PORTAIL: {"llm_profile": "default"}})  # no user_record
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False))

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", mock_sp):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("role_prompt_path") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_message_passes_skills_to_agent_executor(tmp_path) -> None:
    """_handle_message reads skills_dirs from envelope metadata and passes them to AgentExecutor."""
    (tmp_path / "coding").mkdir()

    atelier = _make_atelier_with_patches()
    # Override the skills base dir set at init to point to tmp_path.
    atelier._skills_base_dir = tmp_path

    envelope = _make_envelope(context={CTX_PORTAIL: {"user_record": {
        "skills_dirs": ["coding"],
        "allowed_mcp_tools": [],
    }}})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    executor_calls: list = []

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))

        def capture_call(*args, **kwargs):
            executor_calls.append(kwargs)
            return mock_instance

        MockExecutor.side_effect = capture_call

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    assert len(executor_calls) == 1
    assert "skills" in executor_calls[0]
    assert str(tmp_path / "coding") in executor_calls[0]["skills"]


# ---------------------------------------------------------------------------
# Phase 5 — AsyncSqliteSaver checkpointer (Phase 1 migration)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atelier_creates_async_sqlite_saver_at_startup() -> None:
    """Atelier.__init__ must create AsyncSqliteSaver with checkpoints.db path."""
    from atelier.main import Atelier

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    atelier = _make_atelier_with_patches(
        extra_patches={"atelier.main.AsyncSqliteSaver": mock_saver_cls}
    )

    mock_saver_cls.from_conn_string.assert_called_once()
    call_arg = mock_saver_cls.from_conn_string.call_args[0][0]
    assert call_arg.endswith("checkpoints.db"), (
        f"Expected path ending with 'checkpoints.db', got: {call_arg}"
    )
    # _checkpointer_cm holds the context manager; _checkpointer is None until start()
    assert atelier._checkpointer_cm is mock_saver
    assert atelier._checkpointer is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_message_passes_checkpointer_to_agent_executor() -> None:
    """_handle_message must pass atelier._checkpointer to AgentExecutor."""
    atelier = _make_atelier_with_patches()
    # Simulate what start() does: enter the context manager and store the saver.
    atelier._checkpointer = MagicMock()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    executor_calls: list = []

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))

        def capture_call(*args, **kwargs):
            executor_calls.append(kwargs)
            return mock_instance

        MockExecutor.side_effect = capture_call

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    assert len(executor_calls) == 1
    assert executor_calls[0].get("checkpointer") is atelier._checkpointer


# ---------------------------------------------------------------------------
# Phase 2 — Error reply published to STREAM_OUTGOING_PENDING on agent failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_execution_error_publishes_error_reply_to_outgoing_pending() -> None:
    """When AgentExecutionError is raised, a synthesized error reply is published to outgoing_pending.

    The user must receive a response even when the agent fails.  The error
    synthesizer calls the LLM with the partial message history and publishes
    the result to relais:messages:outgoing_pending so the user sees it.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    error_with_history = AgentExecutionError(
        "Tool loop detected",
        messages_raw=[{"role": "user", "content": "send an email"}],
    )

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(side_effect=error_with_history)
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            with patch("atelier.main.ErrorSynthesizer") as MockSynth:
                                mock_synth_inst = AsyncMock()
                                mock_synth_inst.synthesize = AsyncMock(
                                    return_value="Sorry, I ran into a problem sending the email."
                                )
                                MockSynth.return_value = mock_synth_inst

                                try:
                                    await atelier._run_stream_loop(
                                        atelier.stream_specs()[0], redis_conn, asyncio.Event()
                                    )
                                except asyncio.CancelledError:
                                    pass

    # The error reply must be published to outgoing_pending
    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) >= 1, "Expected at least one publish to outgoing_pending for error reply"

    # Parse the payload and verify it contains the synthesized message
    payloads = [json.loads(c.args[1]["payload"]) for c in outgoing_calls]
    error_replies = [p for p in payloads if "problem" in p.get("content", "").lower()]
    assert len(error_replies) >= 1, f"Expected error reply with 'problem' in content, got: {payloads}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_execution_error_synthesizer_receives_messages_raw() -> None:
    """ErrorSynthesizer.synthesize() is called with the messages_raw from the exception."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    partial_history = [
        {"role": "user", "content": "send email to test@example.com"},
        {"role": "assistant", "content": "Calling himalaya..."},
    ]
    error_with_history = AgentExecutionError(
        "Tool loop",
        messages_raw=partial_history,
    )

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    synth_calls: list[dict] = []

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(side_effect=error_with_history)
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            with patch("atelier.main.ErrorSynthesizer") as MockSynth:
                                mock_synth_inst = AsyncMock()

                                async def capture_synthesize(messages_raw, error, profile):
                                    synth_calls.append({
                                        "messages_raw": messages_raw,
                                        "error": error,
                                        "profile": profile,
                                    })
                                    return "Désolé, je n'ai pas pu envoyer l'email."

                                mock_synth_inst.synthesize = capture_synthesize
                                MockSynth.return_value = mock_synth_inst

                                try:
                                    await atelier._run_stream_loop(
                                        atelier.stream_specs()[0], redis_conn, asyncio.Event()
                                    )
                                except asyncio.CancelledError:
                                    pass

    assert len(synth_calls) == 1
    assert synth_calls[0]["messages_raw"] == partial_history


# ---------------------------------------------------------------------------
# Phase 2 — turn-level timeout (max_turn_seconds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xack_sent_and_dlq_on_turn_timeout() -> None:
    """XACK is sent and DLQ receives the message when the turn exceeds max_turn_seconds.

    asyncio.wait_for() must wrap agent_executor.execute(); a TimeoutError must
    be treated like AgentExecutionError: ACK the message (avoid PEL poison pill),
    route to DLQ, and publish an error reply to the user.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(side_effect=asyncio.TimeoutError())
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_awaited_once()

    dlq_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:tasks:failed"
    ]
    assert len(dlq_calls) == 1

    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1


@pytest.mark.asyncio
async def test_timeout_publishes_skill_trace_with_sentinel() -> None:
    """A timed-out turn publishes a skill trace with tool_error_count=-1.

    When skills were used and the turn is aborted by asyncio.TimeoutError, a
    failure trace envelope must be published to STREAM_SKILL_TRACE with the
    sentinel tool_error_count=-1, so Forgeron can detect the timeout pattern.
    """
    from common.streams import STREAM_SKILL_TRACE

    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(side_effect=asyncio.TimeoutError())
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            # Inject a non-empty skill list so the trace is not skipped
                            with patch("atelier.main.ToolPolicy") as MockToolPolicy:
                                mock_policy = MagicMock()
                                mock_policy.resolve_skills.return_value = ["skills/test-skill"]
                                mock_policy.parse_mcp_patterns.return_value = []
                                mock_policy.filter_mcp_tools.return_value = []
                                MockToolPolicy.return_value = mock_policy

                                try:
                                    await atelier._run_stream_loop(atelier.stream_specs()[0], redis_conn, asyncio.Event())
                                except asyncio.CancelledError:
                                    pass

    skill_trace_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == STREAM_SKILL_TRACE
    ]
    assert len(skill_trace_calls) == 1

    payload = json.loads(skill_trace_calls[0].args[1]["payload"])
    skill_ctx = payload["context"]["skill_trace"]
    assert skill_ctx["tool_error_count"] == -1
    assert skill_ctx["skill_names"] == ["test-skill"]
