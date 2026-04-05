"""Unit tests for Portail._update_active_sessions() (Wave 1B).

Tests follow TDD red-green-refactor. All tests are isolated via AsyncMock;
no real Redis connection is required.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from common.contexts import CTX_PORTAIL
from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    sender_id: str = "discord:123456",
    channel: str = "discord",
    session_id: str = "sess-abc",
    context: dict | None = None,
) -> Envelope:
    """Return a minimal Envelope for portail session tests.

    Args:
        sender_id: Simulated sender identifier.
        channel: The originating channel.
        session_id: The session identifier.
        context: Optional context dict (defaults to empty).

    Returns:
        An Envelope instance suitable for unit testing.
    """
    return Envelope(
        content="hello",
        sender_id=sender_id,
        channel=channel,
        session_id=session_id,
        correlation_id="corr-001",
        context=context or {},
    )


def _make_portail() -> object:
    """Instantiate Portail with Redis client patched to avoid I/O.

    Returns:
        A Portail instance whose RedisClient is mocked.
    """
    with patch("portail.main.RedisClient"):
        from portail.main import Portail
        return Portail()


# ---------------------------------------------------------------------------
# T1 — correct Redis key and HSET call
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_sets_correct_key() -> None:
    """_update_active_sessions() calls HSET with key relais:active_sessions:{sender_id}.

    The Redis hash key must follow the pattern
    ``relais:active_sessions:{envelope.sender_id}``.
    """
    portail = _make_portail()
    envelope = _make_envelope(sender_id="discord:999")
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock()

    await portail._update_active_sessions(redis_conn, envelope)

    hset_calls = redis_conn.hset.await_args_list
    assert len(hset_calls) == 1

    call_args = hset_calls[0]
    # First positional arg must be the correct key
    assert call_args.args[0] == "relais:active_sessions:discord:999"


# ---------------------------------------------------------------------------
# T2 — TTL is set to 3600
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_sets_ttl_3600() -> None:
    """_update_active_sessions() calls EXPIRE with 3600 on the session key.

    The 1-hour TTL must be reset on every call so active users are never
    evicted while a session is in progress.
    """
    portail = _make_portail()
    envelope = _make_envelope(sender_id="telegram:42")
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock()

    await portail._update_active_sessions(redis_conn, envelope)

    expire_calls = redis_conn.expire.await_args_list
    assert len(expire_calls) == 1

    key_arg, ttl_arg = expire_calls[0].args[0], expire_calls[0].args[1]
    assert key_arg == "relais:active_sessions:telegram:42"
    assert ttl_arg == 3600


# ---------------------------------------------------------------------------
# T3 — hash fields contain last_seen (float), channel, session_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_stores_required_fields() -> None:
    """HSET payload contains last_seen (float), channel, and session_id.

    The hash at ``relais:active_sessions:{sender_id}`` must expose at minimum:
    - ``last_seen``: a numeric epoch float (mapping key)
    - ``channel``: the originating channel string
    - ``session_id``: the envelope session identifier
    """
    portail = _make_portail()
    envelope = _make_envelope(
        sender_id="discord:555",
        channel="discord",
        session_id="sess-xyz",
    )
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock()

    await portail._update_active_sessions(redis_conn, envelope)

    call_kwargs = redis_conn.hset.await_args_list[0]
    # hset can be called with mapping= kwarg or with positional args
    mapping: dict = call_kwargs.kwargs.get("mapping") or call_kwargs.args[1]

    assert "last_seen" in mapping, "mapping must include 'last_seen'"
    assert float(mapping["last_seen"]) > 0, "'last_seen' must be a positive float epoch"
    assert mapping["channel"] == "discord"
    assert mapping["session_id"] == "sess-xyz"


# ---------------------------------------------------------------------------
# T3b — display_name is stored when present in envelope metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_stores_display_name_from_metadata() -> None:
    """When context[CTX_PORTAIL] contains display_name inside user_record, it is persisted in the hash.

    This allows the Crieur to personalise push notifications without an
    extra lookup.
    """
    portail = _make_portail()
    envelope = _make_envelope(
        sender_id="discord:777",
        context={CTX_PORTAIL: {"user_record": {"display_name": "Alice"}}},
    )
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock()

    await portail._update_active_sessions(redis_conn, envelope)

    call_kwargs = redis_conn.hset.await_args_list[0]
    mapping: dict = call_kwargs.kwargs.get("mapping") or call_kwargs.args[1]

    assert mapping.get("display_name") == "Alice"


# ---------------------------------------------------------------------------
# T3c — display_name is absent from hash when not in metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_omits_display_name_when_absent() -> None:
    """When metadata does not contain display_name, the field is not stored.

    Storing an empty display_name would pollute the hash with meaningless
    data; the field should only be written when the value is non-empty.
    """
    portail = _make_portail()
    envelope = _make_envelope(sender_id="discord:888")
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock()

    await portail._update_active_sessions(redis_conn, envelope)

    call_kwargs = redis_conn.hset.await_args_list[0]
    mapping: dict = call_kwargs.kwargs.get("mapping") or call_kwargs.args[1]

    assert "display_name" not in mapping


# ---------------------------------------------------------------------------
# T4 — Redis failure logs warning but does NOT raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_logs_warning_on_redis_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When Redis.hset() raises, a warning is logged and no exception propagates.

    The method is fire-and-forget: a Redis outage must not interrupt the
    main message pipeline.
    """
    portail = _make_portail()
    envelope = _make_envelope(sender_id="discord:123")
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock(side_effect=ConnectionError("Redis down"))
    redis_conn.expire = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="portail"):
        # Must not raise
        await portail._update_active_sessions(redis_conn, envelope)

    assert any(
        "active_session" in record.message.lower()
        or "session" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "A WARNING log entry about the session update failure must be emitted"


# ---------------------------------------------------------------------------
# T4b — expire failure also logs warning but does NOT raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_active_sessions_logs_warning_on_expire_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When Redis.expire() raises after hset, a warning is logged and no exception propagates.

    Any step of the session update that fails should be swallowed silently
    with a warning rather than crashing the pipeline.
    """
    portail = _make_portail()
    envelope = _make_envelope(sender_id="discord:321")
    redis_conn = AsyncMock()
    redis_conn.hset = AsyncMock()
    redis_conn.expire = AsyncMock(side_effect=ConnectionError("Redis down"))

    with caplog.at_level(logging.WARNING, logger="portail"):
        await portail._update_active_sessions(redis_conn, envelope)

    assert any(
        record.levelno >= logging.WARNING for record in caplog.records
    ), "A WARNING must be emitted when expire() fails"
