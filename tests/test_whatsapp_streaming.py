"""Tests for WhatsApp adapter — Phase 4 streaming uniformization.

Phase 4 behaviour (via StreamingMixin):
  - Atelier always publishes token-by-token to relais:messages:streaming:whatsapp:{corr_id}
  - The adapter subscribes to relais:streaming:start:whatsapp pub/sub via StreamingMixin
  - On each start signal: spawn _consume_stream() task that reads the streaming stream,
    buffers chunks, calls _deliver() once is_final=1 is received
  - When outgoing:whatsapp receives an envelope with context["atelier"]["streamed"]=True,
    _process_outgoing returns True (ACK) without calling _send_message
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MESSAGE_INCOMING,
    ACTION_MESSAGE_OUTGOING,
)
from common.contexts import CTX_AIGUILLEUR, CTX_ATELIER
from common.streams import stream_streaming

_LOG = logging.getLogger("test.whatsapp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client() -> "_RelaisWhatsAppClient":
    """Return a _RelaisWhatsAppClient with mocked redis and adapter.

    Constructs the instance via __new__ to bypass __init__ env-var reads,
    then manually injects required attributes.

    Returns:
        A _RelaisWhatsAppClient instance ready for unit testing.
    """
    from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient

    adapter = MagicMock()
    adapter.config.profile = "default"
    adapter.config.prompt_path = None

    stop = MagicMock()
    stop.is_set = MagicMock(return_value=False)

    client = _RelaisWhatsAppClient.__new__(_RelaisWhatsAppClient)
    client._adapter = adapter
    client._stop = stop
    client._log = MagicMock()
    client._gateway_url = "http://localhost:3025"
    client._phone_number = "+33600000000"
    client._api_key = "test-key"
    client._webhook_secret = "test"
    client._webhook_port = 9000
    client._webhook_host = "127.0.0.1"
    client._self_jid = "33600000000@s.whatsapp.net"
    client.consumer_name = "whatsapp_test"
    client._http = None
    client._site = None
    client._streaming_tasks: set = set()

    from collections import OrderedDict
    client.seen_message_ids = OrderedDict()
    client.sent_message_ids = OrderedDict()

    redis_conn = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=[])
    redis_conn.xadd = AsyncMock(return_value="0-0")
    client._redis = redis_conn

    return client


def _outgoing_envelope(streamed: bool = False, content: str = "Hello!") -> Envelope:
    """Build a final-reply envelope destined for WhatsApp.

    Args:
        streamed: Whether to set context["atelier"]["streamed"]=True.
        content: Message content.

    Returns:
        Envelope configured for WhatsApp outgoing.
    """
    atelier_ctx: dict = {"streamed": streamed} if streamed else {}
    return Envelope(
        content=content,
        sender_id="whatsapp:+33611111111",
        channel="whatsapp",
        session_id="whatsapp:+33611111111",
        action=ACTION_MESSAGE_OUTGOING,
        context={
            CTX_AIGUILLEUR: {"reply_to": "33611111111@s.whatsapp.net"},
            CTX_ATELIER: atelier_ctx,
        },
    )


# ---------------------------------------------------------------------------
# Phase 4.1 — _process_outgoing skips delivery when streamed flag is set
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_process_outgoing_skips_when_streamed() -> None:
    """_process_outgoing must return True without calling _send_message when streamed=True.

    The LLM response was already delivered via the streaming stream.
    Calling _send_message again would produce a duplicate WhatsApp message.
    """
    client = _make_client()

    with patch.object(client, "_send_message", new_callable=AsyncMock) as mock_send:
        result = await client._process_outgoing(
            stream="relais:messages:outgoing:whatsapp",
            group="whatsapp_relay_group",
            msg_id="1-0",
            msg_data={"payload": _outgoing_envelope(streamed=True).to_json()},
        )

    assert result is True
    mock_send.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_process_outgoing_delivers_when_not_streamed() -> None:
    """_process_outgoing delivers normally when streamed=False.

    Error replies, command rejections, and other non-streamed messages must
    still be sent via _send_message.
    """
    client = _make_client()

    with patch.object(
        client, "_send_message", new_callable=AsyncMock, return_value="msg-id-123"
    ) as mock_send:
        result = await client._process_outgoing(
            stream="relais:messages:outgoing:whatsapp",
            group="whatsapp_relay_group",
            msg_id="1-0",
            msg_data={"payload": _outgoing_envelope(streamed=False, content="Accès refusé.").to_json()},
        )

    assert result is True
    mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 4.2 — _consume_stream (mixin) buffers tokens and calls _deliver
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_consume_streaming_reply_buffers_and_sends() -> None:
    """_consume_stream reads token entries and delivers assembled message via _deliver.

    Given a streaming stream with three chunks followed by is_final=1, the
    mixin must concatenate them and call _deliver with the assembled text.
    """
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="whatsapp:+33611111111",
        channel="whatsapp",
        session_id="whatsapp:+33611111111",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "33611111111@s.whatsapp.net"}},
    )
    corr_id = envelope.correlation_id
    stream_key = stream_streaming("whatsapp", corr_id)

    entries = [
        (stream_key, [
            ("1-0", {"type": "token", "chunk": "Bonjour", "seq": "0", "is_final": "0"}),
            ("1-1", {"type": "token", "chunk": " le", "seq": "1", "is_final": "0"}),
            ("1-2", {"type": "token", "chunk": " monde", "seq": "2", "is_final": "0"}),
        ]),
    ]
    final_entry = [
        (stream_key, [
            ("1-3", {"type": "token", "chunk": "", "seq": "3", "is_final": "1"}),
        ]),
    ]

    call_count = 0

    async def mock_xread(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return entries
        if call_count == 2:
            return final_entry
        return []

    client._redis.xread = mock_xread

    sent_calls: list[tuple[str, str]] = []

    async def mock_send(to_jid: str, text: str):
        sent_calls.append((to_jid, text))
        return "msg-id-ok"

    with patch.object(client, "_send_message", side_effect=mock_send):
        await client._consume_stream(client._redis, "whatsapp", envelope, _LOG)

    assert len(sent_calls) == 1
    assert sent_calls[0][0] == "33611111111@s.whatsapp.net"
    assert sent_calls[0][1] == "Bonjour le monde"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_consume_streaming_reply_ignores_empty_final_chunk() -> None:
    """is_final=1 sentinel with empty chunk is not included in the message.

    The StreamPublisher always sends an empty final sentinel to close the stream.
    That empty string must not be appended to the buffered text.
    """
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="whatsapp:+33611111111",
        channel="whatsapp",
        session_id="whatsapp:+33611111111",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "33611111111@s.whatsapp.net"}},
    )
    stream_key = stream_streaming("whatsapp", envelope.correlation_id)

    client._redis.xread = AsyncMock(return_value=[
        (stream_key, [
            ("1-0", {"type": "token", "chunk": "Final", "seq": "0", "is_final": "0"}),
            ("1-1", {"type": "token", "chunk": "", "seq": "1", "is_final": "1"}),
        ]),
    ])

    sent_calls: list[tuple[str, str]] = []

    async def mock_send(to_jid: str, text: str):
        sent_calls.append((to_jid, text))
        return "msg-id-ok"

    with patch.object(client, "_send_message", side_effect=mock_send):
        await client._consume_stream(client._redis, "whatsapp", envelope, _LOG)

    assert len(sent_calls) == 1
    assert sent_calls[0][1] == "Final"


# ---------------------------------------------------------------------------
# Phase 4.3 — subscribe_streaming_start (mixin) pub/sub listener
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_subscribe_streaming_start_spawns_task() -> None:
    """subscribe_streaming_start spawns _consume_stream for each start message."""
    client = _make_client()

    envelope = Envelope(
        content="hello",
        sender_id="whatsapp:+33611111111",
        channel="whatsapp",
        session_id="whatsapp:+33611111111",
        action=ACTION_MESSAGE_INCOMING,
        context={CTX_AIGUILLEUR: {"reply_to": "33611111111@s.whatsapp.net"}},
    )

    messages = [{"type": "message", "data": envelope.to_json().encode()}]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    client._redis.pubsub = MagicMock(return_value=mock_pubsub)

    spawned: list[Envelope] = []

    async def fake_consume(redis_conn, channel_name, env: Envelope, log) -> None:
        spawned.append(env)

    with patch.object(client, "_consume_stream", side_effect=fake_consume):
        await client.subscribe_streaming_start(client._redis, "whatsapp", client._streaming_tasks, _LOG)
        # Yield to the event loop so the spawned task can run
        await asyncio.sleep(0)

    assert len(spawned) == 1
    assert spawned[0].correlation_id == envelope.correlation_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_subscribe_streaming_start_subscribes_correct_channel() -> None:
    """subscribe_streaming_start subscribes to relais:streaming:start:whatsapp."""
    client = _make_client()

    subscribed: list[str] = []

    async def fake_listen():
        return
        yield  # noqa: unreachable — makes this an async generator

    mock_pubsub = AsyncMock()

    async def capture_subscribe(channel: str) -> None:
        subscribed.append(channel)

    mock_pubsub.subscribe = capture_subscribe
    mock_pubsub.listen = fake_listen

    client._redis.pubsub = MagicMock(return_value=mock_pubsub)

    await client.subscribe_streaming_start(client._redis, "whatsapp", client._streaming_tasks, _LOG)

    assert "relais:streaming:start:whatsapp" in subscribed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_subscribe_streaming_start_ignores_non_message_events() -> None:
    """subscribe_streaming_start ignores pub/sub subscription confirmation events."""
    client = _make_client()

    messages = [
        {"type": "subscribe", "data": 1},
        {"type": "psubscribe", "data": 1},
    ]

    async def fake_listen():
        for msg in messages:
            yield msg

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen

    client._redis.pubsub = MagicMock(return_value=mock_pubsub)

    spawned: list = []

    with patch.object(client, "_consume_stream", side_effect=lambda *a: spawned.append(a)):
        await client.subscribe_streaming_start(client._redis, "whatsapp", client._streaming_tasks, _LOG)

    assert spawned == []


# ---------------------------------------------------------------------------
# Phase 4.4 — start() launches streaming subscriber
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whatsapp_start_includes_streaming_subscriber() -> None:
    """start() must pass subscribe_streaming_start to asyncio.gather.

    The streaming subscriber must run alongside _consume_outgoing so that
    token streams are consumed before the final outgoing envelope arrives.

    We mock aiohttp at the module level so that start() can proceed past
    the HTTP client / webhook runner setup and actually reach asyncio.gather.
    """
    client = _make_client()

    mock_session = AsyncMock()
    mock_session.closed = False

    mock_app = MagicMock()
    mock_app.router = MagicMock()

    mock_runner = AsyncMock()
    mock_runner.setup = AsyncMock()
    mock_runner.cleanup = AsyncMock()

    mock_site = AsyncMock()
    mock_site.start = AsyncMock()

    mock_web = MagicMock()
    mock_web.Application.return_value = mock_app
    mock_web.AppRunner.return_value = mock_runner
    mock_web.TCPSite.return_value = mock_site

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession.return_value = mock_session
    mock_aiohttp.web = mock_web

    import sys

    # Patch aiohttp inside the adapter module so local imports in start() see the mock
    with (
        patch.dict(sys.modules, {"aiohttp": mock_aiohttp, "aiohttp.web": mock_web}),
        patch("asyncio.gather") as mock_gather,
    ):
        mock_gather.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await client.start()

    assert mock_gather.called
    # Verify that the coroutines passed to gather include subscribe_streaming_start
    args = mock_gather.call_args[0]
    coro_names = [getattr(a, "__name__", repr(a)) for a in args]
    assert "subscribe_streaming_start" in coro_names, (
        f"subscribe_streaming_start not found in gather args: {coro_names}"
    )
