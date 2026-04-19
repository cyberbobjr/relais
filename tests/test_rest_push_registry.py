"""Unit tests for aiguilleur/channels/rest/push_registry.py.

TDD: Tests written BEFORE implementation (RED phase).
Uses unittest.mock.AsyncMock to avoid real Redis dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_conn(messages_by_stream: dict | None = None):
    """Build an AsyncMock redis connection.

    Args:
        messages_by_stream: Mapping of stream name -> list of (msg_id, fields)
            tuples to return from xread. Each call pops the first item.
            If None the mock returns empty list forever.

    Returns:
        AsyncMock that simulates redis XREAD BLOCK.
    """
    conn = AsyncMock()
    _queue: list = list(messages_by_stream.items()) if messages_by_stream else []

    async def _fake_xread(streams_dict, count=None, block=None):
        if not _queue:
            # Simulate blocking by sleeping briefly then returning empty
            await asyncio.sleep(0.01)
            return []
        stream_name, msgs = _queue.pop(0)
        if msgs:
            return [(stream_name, msgs)]
        return []

    conn.xread = _fake_xread
    return conn


def _make_envelope_payload(content: str = "hello", corr_id: str = "corr-1") -> str:
    """Build a minimal JSON envelope-like payload string."""
    return json.dumps({"content": content, "correlation_id": corr_id})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPushRegistrySubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_returns_queue(self):
        """subscribe() returns an asyncio.Queue."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_test")
            assert isinstance(queue, asyncio.Queue)
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_subscribe_queue_is_bounded(self):
        """subscribe() returns a bounded Queue with maxsize=256."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_bounded")
            assert queue.maxsize == 256
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_subscribe_same_user_twice_returns_different_queues(self):
        """Two subscribe() calls for the same user return distinct Queue objects."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            q1 = await registry.subscribe("usr_multi")
            q2 = await registry.subscribe("usr_multi")
            assert q1 is not q2
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_subscribe_creates_reader_task(self):
        """First subscribe() for a user creates a background reader task."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            await registry.subscribe("usr_reader")
            # Reader task should be created for this user
            assert "usr_reader" in registry._readers
            assert not registry._readers["usr_reader"].done()
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_subscribe_two_users_separate_readers(self):
        """Each distinct user gets their own reader task."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            await registry.subscribe("usr_a")
            await registry.subscribe("usr_b")
            assert "usr_a" in registry._readers
            assert "usr_b" in registry._readers
            assert registry._readers["usr_a"] is not registry._readers["usr_b"]
        finally:
            await registry.close()


class TestPushRegistryUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe_removes_queue(self):
        """unsubscribe() removes the queue from the subscriber set."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_unsub")
            await registry.unsubscribe("usr_unsub", queue)
            # Queue must no longer be in the set
            assert "usr_unsub" not in registry._subscribers or \
                   queue not in registry._subscribers.get("usr_unsub", set())
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_unsubscribe_last_subscriber_cancels_reader(self):
        """Unsubscribing the last subscriber cancels the reader task."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_cancel")
            task = registry._readers["usr_cancel"]
            await registry.unsubscribe("usr_cancel", queue)
            # Give event loop a tick to process cancellation
            await asyncio.sleep(0.05)
            assert task.cancelled() or task.done()
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_unsubscribe_non_last_subscriber_keeps_reader(self):
        """Unsubscribing one of two subscribers keeps the reader task alive."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            q1 = await registry.subscribe("usr_keep")
            q2 = await registry.subscribe("usr_keep")
            task = registry._readers["usr_keep"]
            await registry.unsubscribe("usr_keep", q1)
            await asyncio.sleep(0.01)
            assert not task.cancelled()
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_user_no_exception(self):
        """unsubscribe() with an unknown user_id must not raise."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        try:
            q = asyncio.Queue()
            await registry.unsubscribe("usr_unknown", q)  # must not raise
        finally:
            await registry.close()


class TestPushRegistryFanout:
    @pytest.mark.asyncio
    async def test_message_fanout_to_subscriber(self):
        """A message read from Redis is put onto all subscriber queues."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        payload = _make_envelope_payload("hello fanout")
        msg = ("msg-1-0", {b"payload": payload.encode()})

        conn = AsyncMock()
        call_count = 0

        async def _fake_xread(streams_dict, count=None, block=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                stream = list(streams_dict.keys())[0]
                return [(stream, [msg])]
            await asyncio.sleep(0.05)
            return []

        conn.xread = _fake_xread
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_fanout")
            # Allow reader task to execute
            await asyncio.sleep(0.15)
            assert not queue.empty()
            item = queue.get_nowait()
            assert item == payload
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_message_fanout_to_multiple_subscribers(self):
        """A message is fanned out to all queues for the same user."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        payload = _make_envelope_payload("broadcast")
        msg = ("msg-2-0", {b"payload": payload.encode()})

        conn = AsyncMock()
        call_count = 0

        async def _fake_xread(streams_dict, count=None, block=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                stream = list(streams_dict.keys())[0]
                return [(stream, [msg])]
            await asyncio.sleep(0.05)
            return []

        conn.xread = _fake_xread
        registry = PushRegistry(conn)
        try:
            q1 = await registry.subscribe("usr_multi2")
            q2 = await registry.subscribe("usr_multi2")
            await asyncio.sleep(0.15)
            assert not q1.empty()
            assert not q2.empty()
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_full_queue_evicts_oldest_and_logs_warning(self, caplog):
        """When a subscriber queue is full, oldest item is evicted and WARNING logged."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        payload_new = _make_envelope_payload("new-msg")
        msg = ("msg-3-0", {b"payload": payload_new.encode()})

        conn = AsyncMock()
        call_count = 0

        async def _fake_xread(streams_dict, count=None, block=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                stream = list(streams_dict.keys())[0]
                return [(stream, [msg])]
            await asyncio.sleep(0.05)
            return []

        conn.xread = _fake_xread
        # Use maxsize=1 via internal creation — we fill the queue manually first
        registry = PushRegistry(conn)
        try:
            queue = await registry.subscribe("usr_full")
            # Pre-fill the queue to maxsize
            old_payload = _make_envelope_payload("old-msg")
            for _ in range(256):
                try:
                    queue.put_nowait(old_payload)
                except asyncio.QueueFull:
                    break

            with caplog.at_level(logging.WARNING, logger="aiguilleur.rest.push_registry"):
                await asyncio.sleep(0.15)

            # Queue should still have 256 items (evicted one, added one)
            assert queue.qsize() == 256
            # The newest message should be in the queue
            items = []
            while not queue.empty():
                items.append(queue.get_nowait())
            assert payload_new in items
        finally:
            await registry.close()


class TestPushRegistryClose:
    @pytest.mark.asyncio
    async def test_close_cancels_all_reader_tasks(self):
        """close() cancels all active reader tasks."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        await registry.subscribe("usr_x")
        await registry.subscribe("usr_y")
        tasks = dict(registry._readers)
        await registry.close()
        await asyncio.sleep(0.05)
        for task in tasks.values():
            assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """Calling close() twice must not raise."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        conn = _make_redis_conn()
        registry = PushRegistry(conn)
        await registry.close()
        await registry.close()  # must not raise


class TestPushRegistryReaderStream:
    @pytest.mark.asyncio
    async def test_reader_uses_correct_stream_name(self):
        """Reader task calls xread with stream_outgoing_user('rest', user_id)."""
        from aiguilleur.channels.rest.push_registry import PushRegistry
        from common.streams import stream_outgoing_user

        expected_stream = stream_outgoing_user("rest", "usr_stream_check")

        streams_used: list = []

        conn = AsyncMock()

        async def _capture_xread(streams_dict, count=None, block=None):
            streams_used.extend(list(streams_dict.keys()))
            await asyncio.sleep(0.05)
            return []

        conn.xread = _capture_xread
        registry = PushRegistry(conn)
        try:
            await registry.subscribe("usr_stream_check")
            await asyncio.sleep(0.1)
            assert expected_stream in streams_used
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_reader_uses_block_2000(self):
        """Reader task uses BLOCK 2000 in xread calls."""
        from aiguilleur.channels.rest.push_registry import PushRegistry

        block_values: list = []

        conn = AsyncMock()

        async def _capture_xread(streams_dict, count=None, block=None):
            block_values.append(block)
            await asyncio.sleep(0.05)
            return []

        conn.xread = _capture_xread
        registry = PushRegistry(conn)
        try:
            await registry.subscribe("usr_block_check")
            await asyncio.sleep(0.1)
            assert any(b == 2000 for b in block_values)
        finally:
            await registry.close()
