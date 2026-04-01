"""Unit tests for the SDK-based atelier.main."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from atelier.agent_executor import AgentExecutionError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "Hello world",
    channel: str = "discord",
    metadata: dict | None = None,
) -> Envelope:
    """Create a minimal Envelope for testing.

    Args:
        content: The message text.
        channel: The originating channel.
        metadata: Optional metadata dict (defaults to empty).

    Returns:
        A test Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id="discord:123456789",
        channel=channel,
        session_id="sess-abc",
        correlation_id="corr-test-001",
        metadata=metadata or {},
    )


def _make_redis_mock() -> AsyncMock:
    """Create a fully mocked Redis connection.

    Returns:
        AsyncMock configured to behave as a Redis async client.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=None)
    redis_conn.publish = AsyncMock()
    return redis_conn


def _make_xreadgroup_result(envelope: Envelope) -> list:
    """Wrap an envelope in the structure returned by xreadgroup.

    Uses string keys to match aioredis decode_responses=True behaviour,
    which is what _process_stream expects when calling data.get("payload").

    Args:
        envelope: The Envelope to embed as the stream message payload.

    Returns:
        List mimicking Redis xreadgroup output format.
    """
    message_id = "1234567890-0"
    data = {"payload": envelope.to_json()}
    return [
        ("relais:tasks", [(message_id, data)])
    ]


def _default_profile_mock() -> MagicMock:
    """Return a MagicMock that behaves like a ProfileConfig.

    Returns:
        MagicMock with model and max_turns set.
    """
    m = MagicMock()
    m.model = "claude-opus-4-5"
    m.max_turns = 10
    return m


def _make_atelier_with_patches(extra_patches: dict | None = None):
    """Instantiate Atelier with __init__-time loaders patched out.

    Patches load_profiles, load_for_sdk, make_skills_tools, and
    resolve_profile before the Atelier() call so that __init__ does not
    hit the filesystem.

    Args:
        extra_patches: Optional dict of additional patch targets → return values
            to apply inside the returned context (applied before Atelier()).

    Returns:
        Tuple of (atelier_instance, dict_of_patch_objects).
    """
    from atelier.main import Atelier

    profile_mock = _default_profile_mock()
    profiles_map = {"default": profile_mock}

    patches = {
        "atelier.main.load_profiles": profiles_map,
        "atelier.main.load_for_sdk": {},
        "atelier.main.make_skills_tools": [],
        "atelier.main.resolve_profile": profile_mock,
    }
    if extra_patches:
        patches.update(extra_patches)

    active: dict = {}
    for target, retval in patches.items():
        p = patch(target, return_value=retval)
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
        mock_instance.execute = AsyncMock(return_value="Response from SDK")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
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
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
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
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_resolves_profile_from_envelope_metadata() -> None:
    """_handle_message() resolves the LLM profile from envelope.metadata['llm_profile']."""
    fast_profile = MagicMock(model="claude-haiku-4-5", max_turns=5)
    default_profile = _default_profile_mock()
    profiles = {"fast": fast_profile, "default": default_profile}

    atelier = _make_atelier_with_patches({
        "atelier.main.load_profiles": profiles,
        "atelier.main.resolve_profile": default_profile,
    })

    # Override the stored _profiles to match what the test expects
    atelier._profiles = profiles

    envelope = _make_envelope(metadata={"llm_profile": "fast"})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=fast_profile) as mock_resolve:
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    mock_resolve.assert_called_once_with(profiles, "fast")


@pytest.mark.asyncio
async def test_handle_message_requests_memory_from_souvenir() -> None:
    """_handle_message() XADDs a 'get' request to relais:memory:request."""
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    memory_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:memory:request"
    ]
    assert len(memory_calls) >= 1

    # Verify at least one call has action='get'
    get_calls = []
    for c in memory_calls:
        fields = c.args[1]
        payload = json.loads(fields.get("payload", "{}"))
        if payload.get("action") == "get":
            get_calls.append(payload)
    assert len(get_calls) == 1
    assert get_calls[0]["session_id"] == envelope.session_id
    assert "correlation_id" in get_calls[0]


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
        mock_instance.execute = AsyncMock(return_value="Sunny and warm.")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
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
    assert response_data["metadata"]["user_message"] == "What is the weather?"


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
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    redis_conn.xack.assert_awaited_once()


# ---------------------------------------------------------------------------
# Phase 4.3 — _fetch_context race-condition fix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 4.4 — Additional gap-filling tests (B1–B5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_context_returns_empty_on_timeout() -> None:
    """_fetch_context returns [] gracefully when xread times out (returns []).

    When Redis xread returns an empty list (no messages within the block
    window), _fetch_context must not raise and must return an empty list.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    # xadd succeeds; xread always returns [] to simulate a persistent timeout
    redis_conn.xadd = AsyncMock(return_value=b"1000000-0")
    redis_conn.xread = AsyncMock(return_value=[])

    # Force the deadline to expire immediately after the first xread call so
    # the while-loop terminates without looping forever.
    call_count = 0

    def mock_loop_time() -> float:
        nonlocal call_count
        call_count += 1
        return 0.0 if call_count <= 3 else 999.0

    loop = asyncio.get_event_loop()
    with patch.object(loop, "time", mock_loop_time):
        result = await atelier._fetch_context(redis_conn, envelope)

    assert result == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_context_returns_parsed_messages_on_success() -> None:
    """_fetch_context returns the messages list when a matching response arrives.

    When xread returns a message whose correlation_id matches the request and
    whose payload contains a 'messages' list, _fetch_context must return that
    list unchanged.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    # Capture the correlation_id sent in the XADD payload so we can echo it back
    captured_correlation_id: list[str] = []

    async def mock_xadd(stream: str, fields: dict) -> bytes:
        if stream == "relais:memory:request":
            req = json.loads(fields.get("payload", "{}"))
            captured_correlation_id.append(req.get("correlation_id", ""))
        return b"1000000-0"

    expected_history = [
        {"role": "user", "content": "previous message"},
        {"role": "assistant", "content": "previous reply"},
    ]

    xread_call_count = 0

    async def mock_xread(streams: dict, count: int, block: int) -> list:
        nonlocal xread_call_count
        xread_call_count += 1
        if xread_call_count == 1 and captured_correlation_id:
            response_payload = json.dumps({
                "correlation_id": captured_correlation_id[0],
                "messages": expected_history,
            })
            return [
                ("relais:memory:response", [("1000001-0", {"payload": response_payload})])
            ]
        return []

    redis_conn.xadd = AsyncMock(side_effect=mock_xadd)
    redis_conn.xread = AsyncMock(side_effect=mock_xread)

    result = await atelier._fetch_context(redis_conn, envelope)

    assert result == expected_history


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_signal_published_for_telegram_channel() -> None:
    """handle_message publishes relais:streaming:start:telegram for telegram channel.

    When the envelope channel is 'telegram' (a STREAMING_CAPABLE_CHANNELS
    member), _handle_message must call redis.publish with the streaming-start
    signal before invoking SDK execute.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
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
                        with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._process_stream(redis_conn)
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
async def test_streaming_signal_not_published_for_non_streaming_channel() -> None:
    """handle_message does NOT publish a streaming signal for whatsapp channel.

    When the envelope channel is 'whatsapp' (not in STREAMING_CAPABLE_CHANNELS),
    _handle_message must not call redis.publish with any streaming-start signal.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="whatsapp")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    # redis.publish must not have been called with any streaming-start pattern
    streaming_publish_calls = [
        c for c in redis_conn.publish.await_args_list
        if "relais:streaming:start:" in str(c)
    ]
    assert len(streaming_publish_calls) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_publisher_finalize_called_after_sdk_execution() -> None:
    """StreamPublisher.finalize() is called after SDK execute() for streaming channels.

    For telegram (a STREAMING_CAPABLE_CHANNELS member), _handle_message must
    call stream_pub.finalize() once after sdk_executor.execute() completes.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="final reply")
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
                        with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._process_stream(redis_conn)
                                except asyncio.CancelledError:
                                    pass

    mock_pub.finalize.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_context_uses_xadd_id_not_dollar() -> None:
    """_fetch_context uses the XADD return ID as starting point, not '$'.

    When XADD returns '1000000-0', the first XREAD call must use
    '999999-0' as the last_id so any response produced between the XADD
    and the XREAD call is not missed.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope()

    captured_last_ids: list[str] = []

    async def mock_xadd(stream: str, fields: dict) -> bytes:
        return b"1000000-0"

    async def mock_xread(streams: dict, count: int, block: int) -> list:
        # Capture the last_id used for relais:memory:response
        captured_last_ids.append(streams.get("relais:memory:response", ""))
        # Return empty so we reach the timeout path quickly
        return []

    redis_conn = _make_redis_mock()
    redis_conn.xadd = AsyncMock(side_effect=mock_xadd)
    redis_conn.xread = AsyncMock(side_effect=mock_xread)

    # Mock loop time so the while loop body executes exactly once:
    # calls 1-3 return 0.0 (deadline setup + first iteration check + remaining),
    # call 4+ returns 999.0 (exits the loop after the first XREAD).
    call_count = 0

    def mock_loop_time() -> float:
        nonlocal call_count
        call_count += 1
        return 0.0 if call_count <= 3 else 999.0

    loop = asyncio.get_event_loop()
    with patch.object(loop, "time", mock_loop_time):
        await atelier._fetch_context(redis_conn, envelope)

    assert len(captured_last_ids) >= 1, "xread should have been called at least once"
    assert captured_last_ids[0] == "999999-0", (
        f"Expected '999999-0' but got '{captured_last_ids[0]}'. "
        "The fix must use XADD return ID minus 1 ms, not '$'."
    )


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
    envelope = _make_envelope(channel="telegram")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
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
                        with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._process_stream(redis_conn)
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
    """_handle_message() sets metadata["streamed"]=True for streaming-capable channels.

    When the envelope channel is "telegram" (a STREAMING_CAPABLE_CHANNEL), the
    response envelope published to relais:messages:outgoing:telegram must carry
    metadata["streamed"] == True so the Aiguilleur can edit instead of re-send.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="telegram")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="Streamed reply")
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
                        with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                            with patch("atelier.main.load_for_sdk", return_value={}):
                                try:
                                    await atelier._process_stream(redis_conn)
                                except asyncio.CancelledError:
                                    pass

    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1
    payload_json = outgoing_calls[0].args[1]["payload"]
    response_data = json.loads(payload_json)
    assert response_data["metadata"].get("streamed") is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_streamed_flag_for_non_streaming_channel() -> None:
    """_handle_message() must NOT set metadata["streamed"] for non-streaming channels.

    When the envelope channel is "whatsapp" (not in STREAMING_CAPABLE_CHANNELS),
    the response envelope must not carry a streamed flag.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(channel="whatsapp")
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="Non-streamed reply")
        MockExecutor.return_value = mock_instance

        with patch("atelier.main.McpSessionManager") as MockMcpMgr:
            mock_mgr = AsyncMock()
            mock_mgr.start_all = AsyncMock()
            MockMcpMgr.return_value = mock_mgr

            with patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
                with patch("atelier.main.resolve_profile", return_value=_default_profile_mock()):
                    with patch("atelier.main.assemble_system_prompt", return_value="soul"):
                        with patch("atelier.main.load_for_sdk", return_value={}):
                            try:
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    outgoing_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_calls) == 1
    payload_json = outgoing_calls[0].args[1]["payload"]
    response_data = json.loads(payload_json)
    assert "streamed" not in response_data["metadata"]


# ---------------------------------------------------------------------------
# Phase 4 — user_role forwarded to assemble_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_stream_passes_user_role_to_assemble_system_prompt() -> None:
    """_process_stream passes envelope.metadata['user_role'] to assemble_system_prompt.

    When Portail has stamped a user_role into the envelope metadata, Atelier
    must forward it as the user_role keyword argument so the role overlay is
    included in the assembled system prompt.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(metadata={"user_role": "admin", "llm_profile": "default"})
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value="soul")

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
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
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("user_role") == "admin"


@pytest.mark.asyncio
async def test_process_stream_user_role_none_when_absent_in_metadata() -> None:
    """_process_stream passes user_role=None when metadata has no user_role key.

    When the envelope carries no user_role (e.g. unknown user), the call must
    still succeed with user_role=None rather than raising KeyError.
    """
    atelier = _make_atelier_with_patches()
    envelope = _make_envelope(metadata={"llm_profile": "default"})  # no user_role
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value="soul")

    with patch("atelier.main.AgentExecutor") as MockExecutor:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
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
                                await atelier._process_stream(redis_conn)
                            except asyncio.CancelledError:
                                pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("user_role") is None
