"""Tests for REST adapter Phase 3 streaming uniformization.

Phase 3 behaviour:
  - Atelier always streams (Phase 1 removed the if-streaming bifurcation).
  - The REST adapter must call correlator.resolve() unconditionally — even when
    context["atelier"]["streamed"] = True — because:
      • SSE mode: _handle_sse() awaits the future to emit the 'done' event.
      • Classic JSON mode: the HTTP handler awaits the future to return content.
  - The push-stream mirror (stream_outgoing_user) must also fire for streamed
    envelopes so GET /v1/events subscribers receive the final response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.contexts import CTX_ATELIER
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from common.streams import stream_outgoing_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter():
    """Return a RestAiguilleur stub with no real Redis or config."""
    from aiguilleur.channels.rest.adapter import RestAiguilleur

    config = MagicMock()
    config.extras = {}
    config.profile_ref = MagicMock()
    config.profile_ref.profile = "default"
    config.prompt_path = None

    with patch("aiguilleur.channels.rest.adapter.RedisClient"):
        adapter = RestAiguilleur.__new__(RestAiguilleur)
        adapter._bind = "127.0.0.1"
        adapter._port = 8080
        adapter._request_timeout = 30.0
        adapter._cors_origins = ["*"]
        adapter._include_traces = False
        adapter._stop_event = MagicMock()
        adapter.config = config
    return adapter


def _streamed_envelope(
    content: str = "Bonjour le monde",
    user: str = "usr_alice",
    corr_id: str = "corr-phase3-1",
) -> Envelope:
    """Build a final-reply envelope with context['atelier']['streamed'] = True."""
    return Envelope(
        content=content,
        sender_id=f"rest:{user}",
        channel="rest",
        session_id="sess-1",
        correlation_id=corr_id,
        action=ACTION_MESSAGE_OUTGOING,
        context={CTX_ATELIER: {"streamed": True}},
    )


# ---------------------------------------------------------------------------
# Phase 3.1 — correlator.resolve() is called even when streamed=True
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_resolves_correlator_when_streamed() -> None:
    """_handle_outgoing_message resolves the correlator even when streamed=True.

    The _handle_sse() handler awaits the correlator future to emit the 'done'
    SSE event with the full content. Skipping correlator.resolve() for streamed
    envelopes would leave SSE clients hanging until request_timeout fires.
    """
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    adapter = _make_adapter()
    envelope = _streamed_envelope()
    redis_conn = AsyncMock()
    correlator = ResponseCorrelator()
    future = await correlator.register(envelope.correlation_id)

    await adapter._handle_outgoing_message(
        {"payload": envelope.to_json().encode()},
        "1-0",
        redis_conn,
        correlator,
        "relais:messages:outgoing:rest",
    )

    assert future.done()
    assert future.result().correlation_id == envelope.correlation_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_resolved_content_is_full_text_when_streamed() -> None:
    """Resolved future carries the full LLM reply even when streamed=True.

    The classic JSON handler returns reply_envelope.content from the future.
    Atelier writes the complete response to the outgoing envelope regardless
    of streaming, so content must be preserved end-to-end.
    """
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    adapter = _make_adapter()
    envelope = _streamed_envelope(content="Résultat complet de l'agent")
    redis_conn = AsyncMock()
    correlator = ResponseCorrelator()
    future = await correlator.register(envelope.correlation_id)

    await adapter._handle_outgoing_message(
        {"payload": envelope.to_json().encode()},
        "1-0",
        redis_conn,
        correlator,
        "relais:messages:outgoing:rest",
    )

    assert future.result().content == "Résultat complet de l'agent"


# ---------------------------------------------------------------------------
# Phase 3.2 — push stream mirror fires for streamed envelopes
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_mirrors_push_stream_when_streamed() -> None:
    """_handle_outgoing_message mirrors the envelope to the push stream even when streamed=True.

    Users subscribed to GET /v1/events receive the full response via the push
    stream. This must work regardless of whether the LLM reply was streamed.
    """
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    adapter = _make_adapter()
    envelope = _streamed_envelope()
    redis_conn = AsyncMock()
    correlator = ResponseCorrelator()
    await correlator.register(envelope.correlation_id)

    await adapter._handle_outgoing_message(
        {"payload": envelope.to_json().encode()},
        "1-0",
        redis_conn,
        correlator,
        "relais:messages:outgoing:rest",
    )

    expected_push_stream = stream_outgoing_user("rest", "usr_alice")
    push_calls = [
        c for c in redis_conn.xadd.call_args_list if c[0][0] == expected_push_stream
    ]
    assert len(push_calls) >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_xack_called_when_streamed() -> None:
    """_handle_outgoing_message XACKs the message even when streamed=True."""
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    adapter = _make_adapter()
    envelope = _streamed_envelope()
    redis_conn = AsyncMock()
    correlator = ResponseCorrelator()
    await correlator.register(envelope.correlation_id)

    stream = "relais:messages:outgoing:rest"
    message_id = "5-0"

    await adapter._handle_outgoing_message(
        {"payload": envelope.to_json().encode()},
        message_id,
        redis_conn,
        correlator,
        stream,
    )

    redis_conn.xack.assert_called_once()
    call_args = redis_conn.xack.call_args[0]
    assert call_args[0] == stream
    assert call_args[2] == message_id


# ---------------------------------------------------------------------------
# Phase 3.3 — non-streamed envelopes (errors, command rejections) still work
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rest_resolves_correlator_for_non_streamed_envelope() -> None:
    """_handle_outgoing_message resolves the correlator for non-LLM envelopes.

    Error replies from Sentinelle and command rejections have streamed=False
    (or no atelier context at all). The correlator must still be resolved so
    the HTTP caller receives the error response.
    """
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    adapter = _make_adapter()
    envelope = Envelope(
        content="Accès refusé.",
        sender_id="rest:usr_bob",
        channel="rest",
        session_id="sess-2",
        correlation_id="corr-phase3-2",
        action=ACTION_MESSAGE_OUTGOING,
    )
    redis_conn = AsyncMock()
    correlator = ResponseCorrelator()
    future = await correlator.register(envelope.correlation_id)

    await adapter._handle_outgoing_message(
        {"payload": envelope.to_json().encode()},
        "2-0",
        redis_conn,
        correlator,
        "relais:messages:outgoing:rest",
    )

    assert future.done()
    assert future.result().content == "Accès refusé."
