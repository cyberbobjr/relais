"""Unit tests for atelier.executor and XACK conditional logic in atelier.main."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest
import pytest_asyncio

from common.envelope import Envelope
from atelier.executor import execute_with_resilience, ExhaustedRetriesError, RETRIABLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope() -> Envelope:
    """Create a minimal Envelope for testing.

    Returns:
        A test Envelope instance.
    """
    return Envelope(
        content="Hello world",
        sender_id="discord:123456789",
        channel="discord",
        session_id="sess-abc",
        correlation_id="corr-test-001",
    )


def _make_ok_response(text: str = "Hello from LLM") -> MagicMock:
    """Build a mock httpx.Response that represents a successful LiteLLM reply.

    Args:
        text: The assistant reply text to embed in the JSON payload.

    Returns:
        A MagicMock that behaves like a successful httpx.Response.
    """
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()  # no-op
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}]
    }
    return resp


def _make_http_error_response(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError for a given HTTP status code.

    Args:
        status_code: The HTTP status code to simulate.

    Returns:
        An httpx.HTTPStatusError instance with a stubbed response.
    """
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = status_code
    return httpx.HTTPStatusError(
        message=f"HTTP {status_code}",
        request=MagicMock(),
        response=mock_response,
    )


# ---------------------------------------------------------------------------
# executor.py — success case
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_with_resilience_returns_llm_reply_on_success() -> None:
    """execute_with_resilience() returns the LLM reply text when the call succeeds."""
    envelope = _make_envelope()
    ok_response = _make_ok_response("Bonjour!")

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.return_value = ok_response

    result = await execute_with_resilience(
        http_client=async_client,
        envelope=envelope,
        context=[{"role": "user", "content": "Hello"}],
        litellm_url="http://localhost:4000/v1",
        model="test-model",
    )

    assert result == "Bonjour!"
    async_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# executor.py — retry on ConnectError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_connect_error_with_sleep() -> None:
    """ConnectError triggers retries with asyncio.sleep delays between attempts."""
    envelope = _make_envelope()
    ok_response = _make_ok_response("Retry success")

    async_client = AsyncMock(spec=httpx.AsyncClient)
    # First two calls raise ConnectError, third succeeds
    async_client.post.side_effect = [
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("connection refused"),
        ok_response,
    ]

    with patch("atelier.executor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await execute_with_resilience(
            http_client=async_client,
            envelope=envelope,
            context=[],
            litellm_url="http://localhost:4000/v1",
            model="test-model",
        )

    assert result == "Retry success"
    assert async_client.post.call_count == 3
    # Sleep called after attempt 1 (delay=2) and attempt 2 (delay=5)
    assert mock_sleep.await_count == 2
    mock_sleep.assert_any_await(2)
    mock_sleep.assert_any_await(5)


# ---------------------------------------------------------------------------
# executor.py — retry on TimeoutException
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_on_timeout_exception() -> None:
    """TimeoutException triggers retries; succeeds on the next attempt."""
    envelope = _make_envelope()
    ok_response = _make_ok_response("Timeout recovered")

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = [
        httpx.TimeoutException("timed out"),
        ok_response,
    ]

    with patch("atelier.executor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await execute_with_resilience(
            http_client=async_client,
            envelope=envelope,
            context=[],
            litellm_url="http://localhost:4000/v1",
            model="test-model",
        )

    assert result == "Timeout recovered"
    assert async_client.post.call_count == 2
    # Sleep called once after the first failed attempt
    mock_sleep.assert_awaited_once_with(2)


# ---------------------------------------------------------------------------
# executor.py — retry on HTTP 502 / 503 / 504
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [502, 503, 504])
async def test_retry_on_http_5xx_gateway_errors(status_code: int) -> None:
    """HTTP 502/503/504 triggers retry; succeeds on second attempt."""
    envelope = _make_envelope()
    ok_response = _make_ok_response("Recovered")

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = [
        _make_http_error_response(status_code),
        ok_response,
    ]

    with patch("atelier.executor.asyncio.sleep", new_callable=AsyncMock):
        result = await execute_with_resilience(
            http_client=async_client,
            envelope=envelope,
            context=[],
            litellm_url="http://localhost:4000/v1",
            model="test-model",
        )

    assert result == "Recovered"
    assert async_client.post.call_count == 2


# ---------------------------------------------------------------------------
# executor.py — ExhaustedRetriesError after 3 ConnectError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exhausted_retries_raises_after_three_connect_errors() -> None:
    """ExhaustedRetriesError is raised after all 3 retry attempts fail with ConnectError."""
    envelope = _make_envelope()

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = httpx.ConnectError("unreachable")

    with patch("atelier.executor.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ExhaustedRetriesError):
            await execute_with_resilience(
                http_client=async_client,
                envelope=envelope,
                context=[],
                litellm_url="http://localhost:4000/v1",
                model="test-model",
            )

    assert async_client.post.call_count == 3


# ---------------------------------------------------------------------------
# executor.py — no retry on non-retriable 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_retry_on_http_400() -> None:
    """HTTP 400 (bad request) raises HTTPStatusError immediately without any retry."""
    envelope = _make_envelope()

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = _make_http_error_response(400)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await execute_with_resilience(
            http_client=async_client,
            envelope=envelope,
            context=[],
            litellm_url="http://localhost:4000/v1",
            model="test-model",
        )

    assert exc_info.value.response.status_code == 400
    # Must not retry — only one call made
    assert async_client.post.call_count == 1


# ---------------------------------------------------------------------------
# executor.py — no retry on non-retriable 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_retry_on_http_401() -> None:
    """HTTP 401 (unauthorized) raises HTTPStatusError immediately without any retry."""
    envelope = _make_envelope()

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = _make_http_error_response(401)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await execute_with_resilience(
            http_client=async_client,
            envelope=envelope,
            context=[],
            litellm_url="http://localhost:4000/v1",
            model="test-model",
        )

    assert exc_info.value.response.status_code == 401
    assert async_client.post.call_count == 1


# ---------------------------------------------------------------------------
# executor.py — retry delay sequence on 3 failed attempts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_delays_respected_on_exhausted_connect_errors() -> None:
    """asyncio.sleep is called with delays [2, 5] for the first two failed attempts."""
    envelope = _make_envelope()

    async_client = AsyncMock(spec=httpx.AsyncClient)
    async_client.post.side_effect = httpx.ConnectError("down")

    sleep_calls: list[float] = []

    async def _capture_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("atelier.executor.asyncio.sleep", side_effect=_capture_sleep):
        with pytest.raises(ExhaustedRetriesError):
            await execute_with_resilience(
                http_client=async_client,
                envelope=envelope,
                context=[],
                litellm_url="http://localhost:4000/v1",
                model="test-model",
            )

    # RETRY_DELAYS = [2, 5, 15]; sleep is called after attempt 1 and 2 (not after 3)
    assert sleep_calls == [2, 5]


# ---------------------------------------------------------------------------
# main.py — XACK conditional behaviour
# ---------------------------------------------------------------------------

def _make_redis_mock() -> AsyncMock:
    """Create a fully mocked Redis connection.

    Returns:
        AsyncMock configured to behave as a Redis async client.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock()
    redis_conn.xack = AsyncMock()
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


@pytest.mark.asyncio
async def test_xack_sent_after_successful_llm_call() -> None:
    """XACK is sent after a successful LLM response and stream publication."""
    from atelier.main import Atelier

    atelier = Atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    # xreadgroup returns one message, then blocks forever (empty)
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    context = [{"role": "user", "content": "Hello"}]
    reply_text = "Response from LLM"

    with patch.object(atelier, "_get_memory_context", new=AsyncMock(return_value=context)):
        with patch.object(atelier, "_append_assistant_memory", new=AsyncMock()):
            with patch(
                "atelier.main.executor.execute_with_resilience",
                new=AsyncMock(return_value=reply_text),
            ):
                try:
                    await atelier._process_stream(redis_conn)
                except asyncio.CancelledError:
                    pass

    redis_conn.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_xack_sent_after_exhausted_retries_routed_to_dlq() -> None:
    """XACK is sent when ExhaustedRetriesError routes the message to the DLQ."""
    from atelier.main import Atelier

    atelier = Atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch.object(atelier, "_get_memory_context", new=AsyncMock(return_value=[])):
        with patch(
            "atelier.main.executor.execute_with_resilience",
            new=AsyncMock(side_effect=ExhaustedRetriesError("all retries failed")),
        ):
            try:
                await atelier._process_stream(redis_conn)
            except asyncio.CancelledError:
                pass

    # XACK must have been called (message safely in DLQ)
    redis_conn.xack.assert_awaited_once()

    # DLQ stream must have received the failed message
    dlq_calls = [
        c for c in redis_conn.xadd.await_args_list
        if c.args[0] == "relais:tasks:failed"
    ]
    assert len(dlq_calls) == 1


@pytest.mark.asyncio
async def test_xack_sent_on_non_retriable_generic_exception() -> None:
    """XACK is sent when a non-retriable generic Exception occurs (avoid PEL poison)."""
    from atelier.main import Atelier

    atelier = Atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch.object(atelier, "_get_memory_context", new=AsyncMock(return_value=[])):
        with patch(
            "atelier.main.executor.execute_with_resilience",
            new=AsyncMock(side_effect=ValueError("Corrupt JSON in envelope")),
        ):
            try:
                await atelier._process_stream(redis_conn)
            except asyncio.CancelledError:
                pass

    # XACK must be called — non-retriable errors should not poison the PEL
    redis_conn.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_xack_not_sent_on_connect_error_message_stays_in_pel() -> None:
    """XACK is NOT sent when a raw ConnectError occurs — message stays in PEL."""
    from atelier.main import Atelier

    atelier = Atelier()
    envelope = _make_envelope()
    redis_conn = _make_redis_mock()

    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    with patch.object(atelier, "_get_memory_context", new=AsyncMock(return_value=[])):
        with patch(
            "atelier.main.executor.execute_with_resilience",
            new=AsyncMock(side_effect=httpx.ConnectError("unreachable")),
        ):
            try:
                await atelier._process_stream(redis_conn)
            except asyncio.CancelledError:
                pass

    # XACK must NOT have been called — message remains in PEL for re-delivery
    redis_conn.xack.assert_not_awaited()
