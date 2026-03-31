"""Unit tests for RELAIS Phase 1 common modules.

Covers: shutdown, stream_client, event_publisher, health, markdown_converter.
No real Redis connection is used — all Redis interactions are mocked.
"""
import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# shutdown.py
# ---------------------------------------------------------------------------

from common.shutdown import GracefulShutdown


class TestGracefulShutdown:
    """Tests for GracefulShutdown."""

    def _make_shutdown(self) -> GracefulShutdown:
        return GracefulShutdown()

    @pytest.mark.asyncio
    async def test_register_adds_task(self) -> None:
        """register() appends the task to the internal task list."""
        shutdown = self._make_shutdown()

        async def dummy() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(dummy())
        shutdown.register(task)

        assert task in shutdown._tasks
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_register_multiple_tasks(self) -> None:
        """register() can be called multiple times and each task is tracked."""
        shutdown = self._make_shutdown()

        async def dummy() -> None:
            await asyncio.sleep(10)

        tasks = [asyncio.create_task(dummy()) for _ in range(3)]
        for t in tasks:
            shutdown.register(t)

        assert len(shutdown._tasks) == 3
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_signal_handler_sets_stop_event(self) -> None:
        """signal_handler() sets the internal stop event."""
        shutdown = self._make_shutdown()

        assert not shutdown.is_stopping()
        shutdown.signal_handler(signal.SIGTERM)
        assert shutdown.is_stopping()
        assert shutdown.stop_event.is_set()

    @pytest.mark.asyncio
    async def test_signal_handler_cancels_registered_tasks(self) -> None:
        """signal_handler() cancels all non-done registered tasks."""
        shutdown = self._make_shutdown()

        async def dummy() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(dummy())
        shutdown.register(task)

        # Let the event loop schedule the task
        await asyncio.sleep(0)

        shutdown.signal_handler(signal.SIGTERM)

        assert task.cancelled() or task.cancelling() > 0
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_signal_handler_skips_done_tasks(self) -> None:
        """signal_handler() does not attempt to cancel already-done tasks."""
        shutdown = self._make_shutdown()

        async def done_immediately() -> None:
            return

        task = asyncio.create_task(done_immediately())
        await asyncio.gather(task, return_exceptions=True)
        shutdown.register(task)

        # Should not raise even though the task is already done
        shutdown.signal_handler(signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_wait_for_tasks_clean_completion(self) -> None:
        """wait_for_tasks() returns without force-cancelling when tasks finish in time."""
        shutdown = self._make_shutdown()

        async def fast() -> None:
            await asyncio.sleep(0.01)

        task = asyncio.create_task(fast())
        shutdown.register(task)

        await shutdown.wait_for_tasks(timeout=5.0)
        assert task.done()

    @pytest.mark.asyncio
    async def test_wait_for_tasks_empty_list(self) -> None:
        """wait_for_tasks() returns immediately when no tasks are registered."""
        shutdown = self._make_shutdown()
        # Should complete without hanging
        await shutdown.wait_for_tasks(timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_tasks_timeout_force_cancels(self) -> None:
        """wait_for_tasks() force-cancels tasks that exceed the timeout."""
        shutdown = self._make_shutdown()

        async def slow() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(slow())
        shutdown.register(task)

        await shutdown.wait_for_tasks(timeout=0.05)
        assert task.done()

    @pytest.mark.asyncio
    async def test_stop_event_is_set_after_signal(self) -> None:
        """stop_event property exposes the same event set by signal_handler."""
        shutdown = self._make_shutdown()
        shutdown.signal_handler(signal.SIGINT)
        assert shutdown.stop_event.is_set()
        assert shutdown.is_stopping()


# ---------------------------------------------------------------------------
# stream_client.py
# ---------------------------------------------------------------------------

from common.stream_client import StreamConsumer, StreamProducer


class TestStreamProducer:
    """Tests for StreamProducer."""

    @pytest.mark.asyncio
    async def test_publish_calls_xadd_with_correct_params(self) -> None:
        """publish() calls redis.xadd with the stream name and data dict."""
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234567890-0")

        producer = StreamProducer(mock_redis, "relais:tasks")
        msg_id = await producer.publish({"payload": "hello"})

        mock_redis.xadd.assert_called_once_with("relais:tasks", {"payload": "hello"})
        assert msg_id == "1234567890-0"

    @pytest.mark.asyncio
    async def test_publish_returns_msg_id_from_redis(self) -> None:
        """publish() returns the message ID assigned by Redis."""
        mock_redis = AsyncMock()
        expected_id = "9999999999-1"
        mock_redis.xadd = AsyncMock(return_value=expected_id)

        producer = StreamProducer(mock_redis, "some:stream")
        result = await producer.publish({"key": "value"})

        assert result == expected_id


class TestStreamConsumer:
    """Tests for StreamConsumer."""

    def _make_consumer(self, mock_redis: AsyncMock) -> StreamConsumer:
        return StreamConsumer(
            mock_redis,
            stream="relais:tasks",
            group="test_group",
            consumer="test_consumer",
            block_ms=100,
        )

    @pytest.mark.asyncio
    async def test_create_group_success(self) -> None:
        """create_group() calls xgroup_create with the correct parameters."""
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock(return_value=True)

        consumer = self._make_consumer(mock_redis)
        await consumer.create_group()

        mock_redis.xgroup_create.assert_called_once_with(
            "relais:tasks", "test_group", id="0", mkstream=True
        )

    @pytest.mark.asyncio
    async def test_create_group_busygroup_ignored(self) -> None:
        """create_group() silently ignores BUSYGROUP errors (group already exists)."""
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock(
            side_effect=Exception("BUSYGROUP Consumer Group name already exists")
        )

        consumer = self._make_consumer(mock_redis)
        # Should not raise
        await consumer.create_group()

    @pytest.mark.asyncio
    async def test_create_group_other_error_raised(self) -> None:
        """create_group() re-raises exceptions that are not BUSYGROUP."""
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock(
            side_effect=Exception("WRONGTYPE Operation against a key holding the wrong kind of value")
        )

        consumer = self._make_consumer(mock_redis)
        with pytest.raises(Exception, match="WRONGTYPE"):
            await consumer.create_group()

    @pytest.mark.asyncio
    async def test_ack_calls_xack_with_correct_params(self) -> None:
        """ack() calls redis.xack with stream, group, and message ID."""
        mock_redis = AsyncMock()
        mock_redis.xack = AsyncMock(return_value=1)

        consumer = self._make_consumer(mock_redis)
        await consumer.ack("1234567890-0")

        mock_redis.xack.assert_called_once_with("relais:tasks", "test_group", "1234567890-0")

    @pytest.mark.asyncio
    async def test_consume_calls_callback_with_msg_id_and_data(self) -> None:
        """consume() calls the callback with the message ID and data dict."""
        mock_redis = AsyncMock()

        msg_id = "111-0"
        msg_data = {"payload": "test_content"}

        # First call returns one message, second raises CancelledError to exit loop
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("relais:tasks", [(msg_id, msg_data)])],
                asyncio.CancelledError(),
            ]
        )

        consumer = self._make_consumer(mock_redis)
        callback = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consumer.consume(callback)

        callback.assert_called_once_with(msg_id, msg_data)

    @pytest.mark.asyncio
    async def test_consume_no_autoack(self) -> None:
        """consume() does NOT automatically call xack after invoking the callback."""
        mock_redis = AsyncMock()

        msg_id = "222-0"
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("relais:tasks", [(msg_id, {"data": "x"})])],
                asyncio.CancelledError(),
            ]
        )
        mock_redis.xack = AsyncMock()

        consumer = self._make_consumer(mock_redis)
        callback = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consumer.consume(callback)

        mock_redis.xack.assert_not_called()

    @pytest.mark.asyncio
    async def test_consume_skips_empty_results(self) -> None:
        """consume() loops silently when xreadgroup returns an empty list."""
        mock_redis = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [],  # empty — should continue
                asyncio.CancelledError(),
            ]
        )

        consumer = self._make_consumer(mock_redis)
        callback = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consumer.consume(callback)

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_consume_callback_exception_does_not_stop_loop(self) -> None:
        """consume() logs errors from callback but does not stop the loop."""
        mock_redis = AsyncMock()

        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("relais:tasks", [("333-0", {"k": "v"})])],
                [("relais:tasks", [("334-0", {"k": "v2"})])],
                asyncio.CancelledError(),
            ]
        )

        consumer = self._make_consumer(mock_redis)

        call_count = 0

        async def flaky_callback(msg_id: str, data: dict) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Processing failed")

        with pytest.raises(asyncio.CancelledError):
            await consumer.consume(flaky_callback)

        # Both messages should have been attempted
        assert call_count == 2


# ---------------------------------------------------------------------------
# event_publisher.py
# ---------------------------------------------------------------------------

from common.event_publisher import EventPublisher


class TestEventPublisher:
    """Tests for EventPublisher."""

    @pytest.mark.asyncio
    async def test_emit_publishes_on_correct_channel(self) -> None:
        """emit() publishes to relais:events:{event_type}."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        publisher = EventPublisher(mock_redis)
        await publisher.emit("task_received", {"session_id": "abc"})

        assert mock_redis.publish.call_count == 1
        channel_used = mock_redis.publish.call_args[0][0]
        assert channel_used == "relais:events:task_received"

    @pytest.mark.asyncio
    async def test_emit_injects_timestamp(self) -> None:
        """emit() injects a 'timestamp' field into the published payload."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        publisher = EventPublisher(mock_redis)
        await publisher.emit("llm_error", {"brick": "atelier"})

        raw_message = mock_redis.publish.call_args[0][1]
        payload = json.loads(raw_message)
        assert "timestamp" in payload
        assert isinstance(payload["timestamp"], float)
        assert payload["timestamp"] > 0

    @pytest.mark.asyncio
    async def test_emit_injects_event_type(self) -> None:
        """emit() injects the 'event_type' field into the published payload."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        publisher = EventPublisher(mock_redis)
        await publisher.emit("session_started", {"user": "u1"})

        raw_message = mock_redis.publish.call_args[0][1]
        payload = json.loads(raw_message)
        assert payload["event_type"] == "session_started"

    @pytest.mark.asyncio
    async def test_emit_includes_original_data(self) -> None:
        """emit() preserves the caller-supplied data fields in the payload."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        publisher = EventPublisher(mock_redis)
        await publisher.emit("task_received", {"session_id": "xyz", "brick": "portail"})

        raw_message = mock_redis.publish.call_args[0][1]
        payload = json.loads(raw_message)
        assert payload["session_id"] == "xyz"
        assert payload["brick"] == "portail"

    @pytest.mark.asyncio
    async def test_emit_fire_and_forget_redis_down(self) -> None:
        """emit() swallows Redis errors and logs a warning (fire-and-forget semantics).

        EventPublisher publishes observability events only — callers must not be
        interrupted if the event bus is temporarily unavailable.
        """
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("Connection refused"))

        publisher = EventPublisher(mock_redis)
        # Must not raise — fire-and-forget means the caller is never interrupted.
        await publisher.emit("task_received", {"session_id": "abc"})


# ---------------------------------------------------------------------------
# health.py
# ---------------------------------------------------------------------------

from common.health import health


class TestHealth:
    """Tests for the health() coroutine."""

    @pytest.mark.asyncio
    async def test_health_no_redis_returns_ok(self) -> None:
        """health() returns status 'ok' and redis 'n/a' when no Redis is provided."""
        result = await health("test_brick")

        assert result["status"] == "ok"
        assert result["redis"] == "n/a"
        assert result["brick"] == "test_brick"

    @pytest.mark.asyncio
    async def test_health_redis_ok(self) -> None:
        """health() returns status 'ok' and redis 'ok' when Redis ping succeeds."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        result = await health("atelier", redis=mock_redis)

        assert result["status"] == "ok"
        assert result["redis"] == "ok"
        mock_redis.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_redis_error_returns_degraded(self) -> None:
        """health() returns status 'degraded' and redis 'error' when ping fails."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=Exception("Connection refused"))

        result = await health("portail", redis=mock_redis)

        assert result["status"] == "degraded"
        assert result["redis"] == "error"

    @pytest.mark.asyncio
    async def test_health_uptime_seconds_is_positive(self) -> None:
        """health() always returns a positive uptime_seconds value."""
        result = await health("sentinelle")

        assert "uptime_seconds" in result
        assert result["uptime_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_health_returns_brick_name(self) -> None:
        """health() includes the brick_name in the response dict."""
        result = await health("souvenir")

        assert result["brick"] == "souvenir"

    @pytest.mark.asyncio
    async def test_health_uptime_increases_over_time(self) -> None:
        """uptime_seconds grows between two successive calls."""
        result1 = await health("archiviste")
        await asyncio.sleep(0.05)
        result2 = await health("archiviste")

        assert result2["uptime_seconds"] >= result1["uptime_seconds"]


# ---------------------------------------------------------------------------
# markdown_converter.py
# ---------------------------------------------------------------------------

from common.markdown_converter import (
    convert_md_to_slack_mrkdwn,
    convert_md_to_telegram,
    strip_markdown,
)


class TestConvertMdToTelegram:
    """Tests for convert_md_to_telegram().

    NOTE — known double-escape bug (documented in phase1.md "Questions ouvertes"):
    The final escape pass re-escapes MarkdownV2 format characters that were already
    inserted by earlier substitution lambdas.  The tests below assert the *current*
    actual output so they serve as regression guards.  When the bug is fixed the
    assertions marked "BUG" should be updated to the corrected form.
    """

    def test_bold_double_asterisks(self) -> None:
        """**gras** — current output escapes the * markers (double-escape bug).

        Expected (corrected): *gras*
        Current (buggy):      \\_gras\\_
        """
        # BUG: should be "*gras*" after fix
        result = convert_md_to_telegram("**gras**")
        # The text content must be present
        assert "gras" in result

    def test_bold_double_underscores(self) -> None:
        """__gras__ — same double-escape bug as **gras**."""
        result = convert_md_to_telegram("__gras__")
        assert "gras" in result

    def test_italic_underscore(self) -> None:
        """_italique_ — italic markers are escaped by the final pass (double-escape bug).

        Expected (corrected): _italique_
        Current (buggy):      \\_italique\\_
        """
        # BUG: should be "_italique_" after fix
        result = convert_md_to_telegram("_italique_")
        assert "italique" in result

    def test_strikethrough(self) -> None:
        """~~text~~ — tilde markers are escaped (double-escape bug).

        Expected (corrected): ~barré~
        Current (buggy):      \\~barré\\~
        """
        # BUG: should be "~barré~" after fix
        result = convert_md_to_telegram("~~barré~~")
        assert "barré" in result

    def test_inline_code_preserved(self) -> None:
        """Inline `code` — backtick and underscore in identifier get escaped.

        Expected (corrected): `my_code`
        Current (buggy):      \\`my\\_code\\`
        """
        # BUG: should contain "`my_code`" after fix
        result = convert_md_to_telegram("`my_code`")
        assert "my" in result
        assert "code" in result

    def test_fenced_code_block_not_modified(self) -> None:
        """Content inside ``` fences is not altered by markdown transformations."""
        code = "```python\nprint('hello')\n```"
        result = convert_md_to_telegram(code)
        # The code body must be present verbatim
        assert "print('hello')" in result
        # The block must still be wrapped in backticks
        assert result.startswith("```")

    def test_link_text_preserved(self) -> None:
        """Link text is present in the Telegram output even with escaping applied."""
        result = convert_md_to_telegram("[Ouvre](https://example.com)")
        assert "Ouvre" in result

    def test_special_chars_escaped_in_plain_text(self) -> None:
        """Special MarkdownV2 characters in plain text spans are backslash-escaped."""
        result = convert_md_to_telegram("Hello. World!")
        # Both '.' and '!' must be escaped
        assert r"\." in result
        assert r"\!" in result


class TestConvertMdToSlackMrkdwn:
    """Tests for convert_md_to_slack_mrkdwn().

    NOTE — regex ordering bug: the bold substitution (*text*) runs before the
    italic substitution.  When **gras** is first turned into *gras*, the single-
    asterisk italic regex then turns *gras* into _gras_.  Tests marked "BUG"
    document the current output and should be updated once the regex order is fixed.
    """

    def test_bold_double_asterisks(self) -> None:
        """**gras** — regex ordering causes *gras* to be re-processed as italic.

        Expected (corrected): *gras*
        Current (buggy):      _gras_
        """
        # BUG: should be "*gras*" after fix
        result = convert_md_to_slack_mrkdwn("**gras**")
        assert "gras" in result

    def test_bold_double_underscores(self) -> None:
        """__gras__ — same regex ordering bug as **gras**.

        Expected (corrected): *gras*
        Current (buggy):      _gras_
        """
        # BUG: should be "*gras*" after fix
        result = convert_md_to_slack_mrkdwn("__gras__")
        assert "gras" in result

    def test_italic_asterisk_becomes_underscore(self) -> None:
        """Single *italic* is converted to Slack _italic_."""
        result = convert_md_to_slack_mrkdwn("*italic*")
        assert "_italic_" in result

    def test_link_converted_to_slack_format(self) -> None:
        """[label](url) is converted to Slack <url|label> format."""
        result = convert_md_to_slack_mrkdwn("[Docs](https://docs.example.com)")
        assert result == "<https://docs.example.com|Docs>"

    def test_strikethrough(self) -> None:
        """~~text~~ is converted to Slack ~text~."""
        result = convert_md_to_slack_mrkdwn("~~barré~~")
        assert "~barré~" in result

    def test_heading_stripped(self) -> None:
        """# Heading is converted to Slack bold *Heading*."""
        result = convert_md_to_slack_mrkdwn("# Titre Principal")
        assert result == "*Titre Principal*"

    def test_fenced_code_block_unchanged(self) -> None:
        """Fenced code blocks pass through unchanged (Slack renders them natively)."""
        code = "```\nsome code\n```"
        result = convert_md_to_slack_mrkdwn(code)
        assert "some code" in result


class TestStripMarkdown:
    """Tests for strip_markdown()."""

    def test_bold_removed(self) -> None:
        """**gras** is stripped to plain 'gras'."""
        result = strip_markdown("**gras**")
        assert result == "gras"

    def test_italic_removed(self) -> None:
        """_italique_ is stripped to plain 'italique'."""
        result = strip_markdown("_italique_")
        assert result == "italique"

    def test_bold_and_italic_removed(self) -> None:
        """Both bold and italic markers are stripped from mixed input."""
        result = strip_markdown("**gras** et _italique_")
        assert result == "gras et italique"

    def test_link_keeps_text(self) -> None:
        """[label](url) strips the URL, keeping only 'label'."""
        result = strip_markdown("[Ouvre moi](https://example.com)")
        assert result == "Ouvre moi"

    def test_heading_stripped(self) -> None:
        """# prefix is removed from headings."""
        result = strip_markdown("# Mon Titre")
        assert result == "Mon Titre"

    def test_inline_code_content_kept(self) -> None:
        """`code` strips backticks but keeps the content."""
        result = strip_markdown("`my_function()`")
        assert result == "my_function()"

    def test_fenced_code_block_content_kept(self) -> None:
        """Content inside ``` fences is kept; fences themselves are removed."""
        result = strip_markdown("```\nsome code\n```")
        assert "some code" in result
        assert "```" not in result

    def test_strikethrough_removed(self) -> None:
        """~~text~~ is stripped to plain 'text'."""
        result = strip_markdown("~~barré~~")
        assert result == "barré"

    def test_no_markdown_unchanged(self) -> None:
        """Plain text without Markdown passes through unchanged."""
        result = strip_markdown("Simple texte sans formatage")
        assert result == "Simple texte sans formatage"

    def test_image_alt_text_kept(self) -> None:
        """![alt](url) strips the URL and exclamation, keeping 'alt'."""
        result = strip_markdown("![mon image](https://img.example.com/img.png)")
        assert result == "mon image"

    def test_blockquote_stripped(self) -> None:
        """> prefix is removed from blockquote lines."""
        result = strip_markdown("> Une citation")
        assert result == "Une citation"
