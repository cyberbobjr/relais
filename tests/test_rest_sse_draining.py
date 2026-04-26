"""Tests for SSE token draining after ResponseCorrelator future resolves.

Regression guard for Bug 3: the SSE loop used to break as soon as
`future.done()` was True, before draining all streaming tokens from Redis.
The fix uses an `is_final="1"` sentinel to decide when the stream is done.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from common.user_record import UserRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user_record() -> UserRecord:
    return UserRecord(
        user_id="usr_test",
        display_name="Test",
        role="user",
        blocked=False,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        allowed_subagents=["*"],
        prompt_path=None,
    )


def _make_registry() -> MagicMock:
    registry = MagicMock()
    registry.resolve_rest_api_key.return_value = _make_user_record()
    return registry


@pytest.fixture
def valid_token() -> str:
    return "sse-drain-test-token"


@pytest.fixture
def fake_redis():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def correlator():
    from aiguilleur.channels.rest.correlator import ResponseCorrelator

    return ResponseCorrelator()


@pytest.fixture
def adapter_mock():
    from aiguilleur.channel_config import ChannelConfig

    config = ChannelConfig(
        name="rest",
        enabled=True,
        streaming=True,
        extras={
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 5,
            "cors_origins": ["*"],
            "include_traces": False,
        },
    )
    adapter = MagicMock()
    adapter.config = config
    return adapter


@pytest_asyncio.fixture
async def test_client(adapter_mock, fake_redis, correlator):
    from aiohttp.test_utils import TestClient, TestServer
    from aiguilleur.channels.rest.server import create_app

    app = create_app(
        adapter=adapter_mock,
        redis_conn=fake_redis,
        correlator=correlator,
        registry=_make_registry(),
        config={
            "bind": "127.0.0.1",
            "port": 8080,
            "request_timeout": 5,
            "cors_origins": ["*"],
            "include_traces": False,
        },
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _publish_streaming_tokens(
    redis_conn,
    streaming_stream: str,
    tokens: list[str],
    *,
    delay_before_final: float = 0.05,
) -> None:
    """Publish token entries followed by the is_final sentinel to a Redis stream."""
    for tok in tokens:
        await redis_conn.xadd(
            streaming_stream,
            {"type": "token", "chunk": tok, "is_final": "0"},
        )
    await asyncio.sleep(delay_before_final)
    await redis_conn.xadd(
        streaming_stream,
        {"type": "", "chunk": "", "is_final": "1"},
    )


def _parse_sse_frames(raw: bytes) -> list[dict]:
    """Parse raw SSE bytes into a list of {event, data} dicts."""
    frames: list[dict] = []
    current: dict = {}
    for line in raw.decode().splitlines():
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw_data = line[len("data:"):].strip()
            try:
                current["data"] = json.loads(raw_data)
            except json.JSONDecodeError:
                current["data"] = raw_data
        elif line == "" and current:
            frames.append(current)
            current = {}
    if current:
        frames.append(current)
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSseDraining:
    @pytest.mark.asyncio
    async def test_tokens_received_before_final_message(
        self, test_client, valid_token, fake_redis, correlator
    ) -> None:
        """All streaming tokens must be delivered even if the final message
        arrives at the correlator before all tokens are written to the stream.

        Regression: the old loop broke on `future.done()` before draining.
        """
        captured_corr_id: list[str] = []
        original_xadd = fake_redis.xadd

        async def intercepting_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )
                    captured_corr_id.append(env.correlation_id)

                    async def _resolve_and_stream():
                        streaming_stream = (
                            f"relais:messages:streaming:{env.channel}:{env.correlation_id}"
                        )
                        # Publish tokens first
                        tokens = ["Hello", " ", "world", "!"]
                        await _publish_streaming_tokens(fake_redis, streaming_stream, tokens)
                        # Then resolve the correlator future (simulates outgoing reply arriving)
                        reply = Envelope.from_parent(env, "reply")
                        reply.action = ACTION_MESSAGE_OUTGOING
                        await correlator.resolve(env.correlation_id, reply)

                    asyncio.get_event_loop().create_task(_resolve_and_stream())
            return result

        fake_redis.xadd = intercepting_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "hello stream"},
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Accept": "text/event-stream",
            },
        )

        raw = await resp.read()
        frames = _parse_sse_frames(raw)

        token_frames = [f for f in frames if f.get("event") == "token"]
        token_text = "".join(f["data"].get("t", "") for f in token_frames)

        assert token_text == "Hello world!", (
            f"Expected all tokens but got: {token_text!r}. Frames: {frames}"
        )

    @pytest.mark.asyncio
    async def test_tokens_after_future_resolved(
        self, test_client, valid_token, fake_redis, correlator
    ) -> None:
        """Tokens published AFTER the correlator future resolves must still be delivered.

        This is the core regression: future.done()=True must not terminate the
        loop until the is_final sentinel is seen.
        """
        original_xadd = fake_redis.xadd

        async def intercepting_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )

                    async def _resolve_then_stream():
                        # Resolve the future FIRST — simulates reply arriving before tokens
                        reply = Envelope.from_parent(env, "reply")
                        reply.action = ACTION_MESSAGE_OUTGOING
                        await correlator.resolve(env.correlation_id, reply)
                        # Then publish tokens after a tiny delay
                        await asyncio.sleep(0.05)
                        streaming_stream = (
                            f"relais:messages:streaming:{env.channel}:{env.correlation_id}"
                        )
                        for tok in ["late", " token"]:
                            await fake_redis.xadd(
                                streaming_stream,
                                {"type": "token", "chunk": tok, "is_final": "0"},
                            )
                        await fake_redis.xadd(
                            streaming_stream,
                            {"type": "", "chunk": "", "is_final": "1"},
                        )

                    asyncio.get_event_loop().create_task(_resolve_then_stream())
            return result

        fake_redis.xadd = intercepting_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "late tokens test"},
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Accept": "text/event-stream",
            },
        )

        raw = await resp.read()
        frames = _parse_sse_frames(raw)
        token_frames = [f for f in frames if f.get("event") == "token"]
        token_text = "".join(f["data"].get("t", "") for f in token_frames)

        assert token_text == "late token", (
            f"Expected late tokens but got: {token_text!r}. Frames: {frames}"
        )

    @pytest.mark.asyncio
    async def test_no_tokens_stream_ends_on_final_sentinel(
        self, test_client, valid_token, fake_redis, correlator
    ) -> None:
        """SSE stream ends gracefully when only the is_final sentinel is published
        (no tokens). The done event must be present in the response."""
        original_xadd = fake_redis.xadd

        async def intercepting_xadd(stream, fields, *args, **kwargs):
            result = await original_xadd(stream, fields, *args, **kwargs)
            stream_str = stream if isinstance(stream, str) else stream.decode()
            if "relais:messages:incoming" in stream_str:
                payload = fields.get(b"payload") or fields.get("payload")
                if payload:
                    env = Envelope.from_json(
                        payload if isinstance(payload, str) else payload.decode()
                    )

                    async def _resolve():
                        streaming_stream = (
                            f"relais:messages:streaming:{env.channel}:{env.correlation_id}"
                        )
                        # Only sentinel, no tokens
                        await fake_redis.xadd(
                            streaming_stream,
                            {"type": "", "chunk": "", "is_final": "1"},
                        )
                        reply = Envelope.from_parent(env, "reply")
                        reply.action = ACTION_MESSAGE_OUTGOING
                        await correlator.resolve(env.correlation_id, reply)

                    asyncio.get_event_loop().create_task(_resolve())
            return result

        fake_redis.xadd = intercepting_xadd

        resp = await test_client.post(
            "/v1/messages",
            json={"content": "no tokens"},
            headers={
                "Authorization": f"Bearer {valid_token}",
                "Accept": "text/event-stream",
            },
        )

        raw = await resp.read()
        frames = _parse_sse_frames(raw)
        event_names = {f.get("event") for f in frames}
        assert "done" in event_names, f"Expected 'done' SSE event. Frames: {frames}"
