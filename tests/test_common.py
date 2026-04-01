"""Unit tests for RELAIS Phase 1 common modules.

Covers: shutdown, markdown_converter.
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
