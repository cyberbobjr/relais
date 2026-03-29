"""Unit tests for ContextStore context window compaction (Wave 2B).

Tests cover the `maybe_compact()` method and the auto-compaction behaviour
wired into `append()` when an LLM client is configured.
"""

import json
import time
from unittest.mock import AsyncMock, call, patch

import pytest

from souvenir.context_store import ContextStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(count: int) -> list[bytes]:
    """Return ``count`` serialised message bytes suitable for lrange return values.

    Args:
        count: Number of messages to generate.

    Returns:
        List of JSON-encoded message bytes alternating user/assistant roles.
    """
    roles = ["user", "assistant"]
    return [
        json.dumps({"role": roles[i % 2], "content": f"message {i}"}).encode()
        for i in range(count)
    ]


def _make_llm_client(summary: str = "Résumé de test.") -> AsyncMock:
    """Return an async mock that acts as an LLM summarisation callable.

    Args:
        summary: The summary string the mock should return.

    Returns:
        AsyncMock returning ``summary`` when awaited.
    """
    client = AsyncMock(return_value=summary)
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return an AsyncMock simulating a redis.asyncio.Redis client."""
    redis = AsyncMock()
    redis.rpush = AsyncMock(return_value=1)
    redis.ltrim = AsyncMock()
    redis.expire = AsyncMock()
    redis.lrange = AsyncMock(return_value=[])
    redis.llen = AsyncMock(return_value=0)
    redis.delete = AsyncMock()
    redis.scan = AsyncMock(return_value=(0, []))
    return redis


@pytest.fixture
def context_store(mock_redis: AsyncMock) -> ContextStore:
    """Return a ContextStore with max_messages=20 wired to mock Redis."""
    return ContextStore(redis=mock_redis, max_messages=20, ttl_seconds=86400)


# ---------------------------------------------------------------------------
# T1 — maybe_compact returns False when below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_returns_false_below_threshold(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """maybe_compact() returns False when message count is below 80% threshold.

    With max_messages=20 the threshold is 16. A list of 10 messages must not
    trigger compaction.
    """
    mock_redis.llen.return_value = 10
    llm_client = _make_llm_client()

    result = await context_store.maybe_compact("user-1", llm_client=llm_client)

    assert result is False
    llm_client.assert_not_awaited()


# ---------------------------------------------------------------------------
# T2 — maybe_compact returns True at threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_returns_true_at_threshold(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """maybe_compact() returns True when message count reaches the 80% threshold.

    With max_messages=20 the threshold is 16. A list of exactly 16 messages
    must trigger compaction.
    """
    messages = _make_messages(16)
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages
    llm_client = _make_llm_client()

    result = await context_store.maybe_compact("user-2", llm_client=llm_client)

    assert result is True


# ---------------------------------------------------------------------------
# T3 — after compaction, list length is reduced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_reduces_list_length(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """After compaction the Redis list contains only to_keep + 1 summary message.

    With 16 messages: to_compact = messages[:8], to_keep = messages[8:].
    The rebuilt list must contain 9 entries (1 summary + 8 kept).
    """
    messages = _make_messages(16)
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages
    llm_client = _make_llm_client("Mon super résumé.")

    await context_store.maybe_compact("user-3", llm_client=llm_client)

    # DEL must be called to wipe the old list
    mock_redis.delete.assert_awaited_once_with("relais:context:user-3")

    # RPUSH must be called once with key + (1 summary + 8 kept) = 9 items
    assert mock_redis.rpush.await_count == 1
    rpush_args = mock_redis.rpush.call_args[0]
    # rpush_args = (key, item0, item1, ..., item8) → length 10 (key + 9 items)
    assert len(rpush_args) == 10  # 1 key + 1 summary + 8 to_keep


# ---------------------------------------------------------------------------
# T4 — summary message has role="system" and starts with "[RÉSUMÉ]"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_summary_message_format(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """The summary message pushed to Redis must have role='system' and prefix '[RÉSUMÉ]'."""
    messages = _make_messages(16)
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages
    llm_client = _make_llm_client("Ceci est le résumé.")

    await context_store.maybe_compact("user-4", llm_client=llm_client)

    rpush_args = mock_redis.rpush.call_args[0]
    # First element after key is the summary message
    summary_msg = json.loads(rpush_args[1])
    assert summary_msg["role"] == "system"
    assert summary_msg["content"].startswith("[RÉSUMÉ]")
    assert "Ceci est le résumé." in summary_msg["content"]


# ---------------------------------------------------------------------------
# T5 — LLM client is called with the oldest messages (to_compact slice)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_llm_called_with_oldest_messages(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """The LLM client must receive the first half of messages (to_compact slice)."""
    messages = _make_messages(16)
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages
    llm_client = _make_llm_client()

    await context_store.maybe_compact("user-5", llm_client=llm_client)

    llm_client.assert_awaited_once()
    called_with = llm_client.call_args[0][0]
    # to_compact = messages[:8] → 8 decoded dicts
    assert len(called_with) == 8
    assert called_with[0] == json.loads(messages[0])
    assert called_with[7] == json.loads(messages[7])


# ---------------------------------------------------------------------------
# T6 — append() auto-triggers maybe_compact when llm_client is set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_append_auto_triggers_compaction_when_llm_client_set(
    mock_redis: AsyncMock,
) -> None:
    """append() must call maybe_compact() automatically when llm_client is set.

    Simulates a full list (16 msgs) so compaction is triggered.
    """
    messages = _make_messages(16)
    # After append the list grows to 16, so llen returns 16
    mock_redis.rpush.return_value = 16
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages

    llm_client = _make_llm_client()
    store = ContextStore(
        redis=mock_redis,
        max_messages=20,
        ttl_seconds=86400,
        llm_client=llm_client,
    )

    await store.append("user-6", "user", "nouveau message")

    llm_client.assert_awaited_once()


# ---------------------------------------------------------------------------
# T7 — append() does NOT call maybe_compact when llm_client is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_append_does_not_compact_when_no_llm_client(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """append() must not trigger compaction when llm_client is None (default).

    Ensures backward compatibility: existing ContextStore instances that do
    not provide an LLM client continue to work without modification.
    """
    mock_redis.rpush.return_value = 20

    # context_store fixture has no llm_client set
    await context_store.append("user-7", "user", "message")

    # llen should never be called — compaction gate is the llm_client check
    mock_redis.llen.assert_not_awaited()


# ---------------------------------------------------------------------------
# T8 — compaction failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_maybe_compact_failure_is_non_fatal(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """When the LLM call raises, maybe_compact() logs a warning and returns False.

    The Redis list must be left unchanged (no DELETE, no RPUSH after failure).
    """
    messages = _make_messages(16)
    mock_redis.llen.return_value = 16
    mock_redis.lrange.return_value = messages

    failing_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    with patch("souvenir.context_store.logger") as mock_logger:
        result = await context_store.maybe_compact("user-8", llm_client=failing_llm)

    assert result is False
    mock_logger.warning.assert_called_once()
    # The key must NOT have been deleted (context left unchanged)
    mock_redis.delete.assert_not_awaited()
