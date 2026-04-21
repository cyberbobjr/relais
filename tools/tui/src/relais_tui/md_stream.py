"""MarkdownStream — sliding-window streaming display for LLM token output.

Renders a progressively-growing markdown string with a fixed live window
at the bottom (LIVE_WINDOW lines), committing stable lines above it to
permanent console output.  Mirrors the pattern used by aider.

Usage::

    stream = MarkdownStream(console)
    for token in token_generator():
        accumulated += token
        stream.update(accumulated)
    stream.update(accumulated, final=True)
"""
from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text


class MarkdownStream:
    """Sliding-window markdown renderer for streaming LLM output.

    Keeps the last LIVE_WINDOW rendered lines in a Rich Live block and
    permanently prints any lines that scroll above the window.

    Args:
        console: Rich Console instance used for all output.
    """

    LIVE_WINDOW: int = 6
    MIN_DELAY: float = 0.033  # ~30 fps max

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live = Live(console=console, refresh_per_second=30, transient=True)
        self._live.start()
        self._live_active: bool = True
        self._num_printed: int = 0
        self._last_update: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, text: str, *, final: bool = False) -> None:
        """Push a new version of the accumulated text to the display.

        Non-final calls are rate-limited to MIN_DELAY between refreshes.
        When final=True the live block is stopped and remaining text is
        printed as stable markdown.

        Args:
            text: Full accumulated text so far (not a delta).
            final: If True, commit everything and close the live context.
        """
        if not self._live_active:
            return

        now = time.monotonic()

        if not final:
            if now - self._last_update < self.MIN_DELAY:
                return
            self._last_update = now
            self._render_sliding(text)
        else:
            self._commit_final(text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render_lines(self, text: str) -> list[str]:
        """Split *text* into lines for sliding-window accounting.

        Uses raw newline splitting so that single-newline content (common in
        streamed LLM output) is counted correctly regardless of Markdown
        paragraph-folding behaviour.

        Args:
            text: Accumulated text so far.

        Returns:
            List of raw line strings (may be empty for empty input).
        """
        if not text:
            return []
        return text.splitlines()

    def _render_sliding(self, text: str) -> None:
        """Render the sliding live window, printing stable lines above it."""
        if not text:
            self._live.update(Text(""))
            return

        lines = self._render_lines(text)
        total = len(lines)

        # Flush stable lines that have scrolled out of the window
        stable_end = max(0, total - self.LIVE_WINDOW)
        if stable_end > self._num_printed:
            for line in lines[self._num_printed:stable_end]:
                self._console.print(line, markup=False, highlight=False)
            self._num_printed = stable_end

        # Show remaining lines in the live block as Markdown
        window_text = "\n".join(lines[self._num_printed:])
        self._live.update(Markdown(window_text) if window_text else Text(""))

    def _commit_final(self, text: str) -> None:
        """Stop the live display and print the full response permanently.

        The transient Live block is erased on stop, so we print the entire
        accumulated text (not just the remaining window) as final Markdown.
        """
        self._live.stop()
        self._live_active = False

        if text:
            self._console.print(Markdown(text))
