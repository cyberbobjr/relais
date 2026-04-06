"""Unit tests for portail.main.Portail._handle_envelope.

Covers the main processing path (enrich → policy → forward) including:
- Known user enrichment and forward to relais:security
- Guest policy stamps and forward
- Pending policy publishes to admin stream and returns without forwarding
- Deny policy drops silently without forwarding
- action and trace stamping
- content_preview truncation in the log xadd
- Redis log emission
- Handler always returns True
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_VALIDATED
from common.streams import STREAM_LOGS, STREAM_SECURITY, STREAM_ADMIN_PENDING_USERS
from common.user_record import UserRecord
from portail.main import Portail


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user_record(user_id: str = "usr_alice") -> UserRecord:
    return UserRecord(
        user_id=user_id,
        display_name="Alice",
        role="user",
        blocked=False,
        actions=["*"],
        skills_dirs=[],
        allowed_mcp_tools=[],
        allowed_subagents=[],
        prompt_path=None,
    )


def _make_envelope(
    content: str = "Hello",
    sender_id: str = "discord:111222333",
    channel: str = "discord",
    context: dict | None = None,
) -> Envelope:
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id="sess-001",
        correlation_id="corr-portail-001",
        context=context or {},
    )


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    redis.xadd = AsyncMock()
    return redis


@pytest.fixture
def portail() -> Portail:
    """Return a Portail instance with all external I/O patched out.

    Construction notes
    ------------------
    ``Portail.__new__`` is used to bypass ``__init__`` so that no Redis
    connection is opened and no YAML file is read during test setup.
    ``BrickBase.__init__`` is then called directly to populate the base-class
    attributes (``_config_lock``, ``_brick_logger``, etc.) that the handler
    code expects.

    The following attributes are set synthetically instead of being loaded by
    ``_load()`` — keep this list in sync with ``Portail._load()`` if new
    config-driven attributes are added:

    * ``stream_in``, ``stream_out``, ``group_name``, ``consumer_name``
    * ``_config_path``, ``_config_loaded_once``
    * ``_user_registry`` (replaced by a MagicMock)
    * ``_guest_role``, ``_unknown_user_policy``
    """
    with patch("portail.main.UserRegistry") as mock_reg_cls:
        mock_registry = MagicMock()
        mock_registry.unknown_user_policy = "deny"
        mock_registry.guest_role = "guest"
        mock_registry.is_permissive = True
        mock_registry._config_path = None
        mock_registry.resolve_user.return_value = None
        mock_reg_cls.return_value = mock_registry
        instance = Portail.__new__(Portail)
        # Initialise BrickBase attributes without calling super().__init__
        from common.brick_base import BrickBase
        import asyncio
        BrickBase.__init__(instance, "portail")
        instance.stream_in = "relais:messages:incoming"
        instance.stream_out = STREAM_SECURITY
        instance.group_name = "portail_group"
        instance.consumer_name = "portail_1"
        instance._config_path = None
        instance._config_loaded_once = False
        instance._user_registry = mock_registry
        instance._guest_role = "guest"
        instance._unknown_user_policy = "deny"
    return instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_user_enriches_and_forwards(portail: Portail) -> None:
    """Known sender: context[CTX_PORTAIL] is stamped and envelope forwarded."""
    record = _make_user_record()
    portail._user_registry.resolve_user.return_value = record
    envelope = _make_envelope()
    redis = _make_redis()

    result = await portail._handle_envelope(envelope, redis)

    assert result is True
    assert CTX_PORTAIL in envelope.context
    ctx = envelope.context[CTX_PORTAIL]
    assert ctx["user_id"] == "usr_alice"
    assert ctx["user_record"]["display_name"] == "Alice"
    assert ctx["llm_profile"] == "default"

    # Must have forwarded to security stream
    xadd_streams = [call.args[0] for call in redis.xadd.call_args_list]
    assert STREAM_SECURITY in xadd_streams


@pytest.mark.asyncio
async def test_known_user_uses_channel_profile(portail: Portail) -> None:
    """llm_profile is taken from context[CTX_AIGUILLEUR][channel_profile]."""
    record = _make_user_record()
    portail._user_registry.resolve_user.return_value = record
    envelope = _make_envelope(context={CTX_AIGUILLEUR: {"channel_profile": "fast"}})
    redis = _make_redis()

    await portail._handle_envelope(envelope, redis)

    assert envelope.context[CTX_PORTAIL]["llm_profile"] == "fast"


@pytest.mark.asyncio
async def test_action_and_trace_stamped(portail: Portail) -> None:
    """Portail stamps ACTION_MESSAGE_VALIDATED and adds a trace entry."""
    record = _make_user_record()
    portail._user_registry.resolve_user.return_value = record
    envelope = _make_envelope()
    redis = _make_redis()

    await portail._handle_envelope(envelope, redis)

    assert envelope.action == ACTION_MESSAGE_VALIDATED
    assert any(t.get("brick") == "portail" for t in envelope.traces)


@pytest.mark.asyncio
async def test_deny_policy_drops_unknown_user(portail: Portail) -> None:
    """deny policy: unknown user is dropped, nothing forwarded to security."""
    portail._unknown_user_policy = "deny"
    portail._user_registry.resolve_user.return_value = None
    envelope = _make_envelope()
    redis = _make_redis()

    result = await portail._handle_envelope(envelope, redis)

    assert result is True
    xadd_streams = [call.args[0] for call in redis.xadd.call_args_list]
    assert STREAM_SECURITY not in xadd_streams


@pytest.mark.asyncio
async def test_guest_policy_stamps_and_forwards(portail: Portail) -> None:
    """guest policy: guest context is stamped and envelope forwarded."""
    portail._unknown_user_policy = "guest"
    portail._user_registry.resolve_user.return_value = None
    guest_record = _make_user_record(user_id="guest")
    portail._user_registry.build_guest_record.return_value = guest_record
    envelope = _make_envelope()
    redis = _make_redis()

    result = await portail._handle_envelope(envelope, redis)

    assert result is True
    assert CTX_PORTAIL in envelope.context
    assert envelope.context[CTX_PORTAIL]["user_id"] == "guest"
    xadd_streams = [call.args[0] for call in redis.xadd.call_args_list]
    assert STREAM_SECURITY in xadd_streams


@pytest.mark.asyncio
async def test_pending_policy_publishes_and_drops(portail: Portail) -> None:
    """pending policy: unknown user is published to admin stream, not forwarded."""
    portail._unknown_user_policy = "pending"
    portail._user_registry.resolve_user.return_value = None
    envelope = _make_envelope()
    redis = _make_redis()

    result = await portail._handle_envelope(envelope, redis)

    assert result is True
    xadd_streams = [call.args[0] for call in redis.xadd.call_args_list]
    assert STREAM_ADMIN_PENDING_USERS in xadd_streams
    assert STREAM_SECURITY not in xadd_streams


@pytest.mark.asyncio
async def test_log_emitted_to_redis(portail: Portail) -> None:
    """After forwarding, a log entry is written to relais:logs."""
    record = _make_user_record()
    portail._user_registry.resolve_user.return_value = record
    envelope = _make_envelope()
    redis = _make_redis()

    await portail._handle_envelope(envelope, redis)

    xadd_streams = [call.args[0] for call in redis.xadd.call_args_list]
    assert STREAM_LOGS in xadd_streams


@pytest.mark.asyncio
async def test_content_preview_truncated_at_60(portail: Portail) -> None:
    """content_preview in the log xadd is truncated to 60 characters."""
    record = _make_user_record()
    portail._user_registry.resolve_user.return_value = record
    long_content = "x" * 120
    envelope = _make_envelope(content=long_content)
    redis = _make_redis()

    await portail._handle_envelope(envelope, redis)

    log_calls = [
        call for call in redis.xadd.call_args_list
        if call.args[0] == STREAM_LOGS
    ]
    assert log_calls, "No log xadd found"
    log_payload: dict = log_calls[0].args[1]
    assert len(log_payload.get("content_preview", "")) <= 60


@pytest.mark.asyncio
async def test_returns_true_always(portail: Portail) -> None:
    """_handle_envelope always returns True regardless of policy."""
    portail._unknown_user_policy = "deny"
    portail._user_registry.resolve_user.return_value = None
    envelope = _make_envelope()
    redis = _make_redis()

    assert await portail._handle_envelope(envelope, redis) is True
