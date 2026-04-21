"""RelaisApp — windowed full-screen TUI for the RELAIS chat client.

Layout::

    ┌─ Chat ──────────────────────────────────────────┐
    │  ██████╗ ███████╗██╗      █████╗ ██╗███████╗   │
    │  you › de quoi avons nous parlé ?               │
    │  relais ›                                        │
    │    ⋯ tool_call: read_file                        │
    │    ⋯ tool_result: read_file: …                   │
    │  La réponse finale… ⣾                            │
    └─────────────────────────────────────────────────┘
    ┌─ Input ─────────────────────────────────────────┐
    │  you › [TextArea multiline input]               │
    └─────────────────────────────────────────────────┘

Key bindings on the TextArea:
- Enter         → submit message
- Escape+Enter  → insert newline
- Ctrl+J        → insert newline
- Ctrl+C / Ctrl+D → quit
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from uuid import uuid4

import httpx
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from relais_tui.attachments import ImagePayload, PasteBlock
from relais_tui.chat_state import ChatState
from relais_tui.client import RelaisClient
from relais_tui.config import Config, save_config
from relais_tui.paste_handler import grab_image_from_clipboard, is_large_paste, summarize_paste
from relais_tui.sse_parser import DoneEvent, ErrorEvent, ProgressEvent, TokenEvent

_log = logging.getLogger(__name__)

_SPINNER_CHARS = "⣾⣽⣻⢿⡿⣟⣯⣷"

_CLEAR_PHRASES = frozenset([
    "✓ Conversation history cleared.",
    "Conversation history cleared.",
])


def _get_versions() -> tuple[str, str]:
    """Return (relais_core_version, tui_version).

    Returns:
        Tuple of (core_version, tui_version); unknown versions show as "?".
    """
    import tomllib

    try:
        tui_ver = importlib.metadata.version("relais-tui")
    except importlib.metadata.PackageNotFoundError:
        tui_ver = "?"

    try:
        root_toml = Path(__file__).parents[4] / "pyproject.toml"
        with root_toml.open("rb") as fh:
            relais_ver = tomllib.load(fh)["project"]["version"]
    except Exception:
        try:
            relais_ver = importlib.metadata.version("relais")
        except importlib.metadata.PackageNotFoundError:
            relais_ver = "?"

    return relais_ver, tui_ver


class RelaisApp:
    """Full-screen windowed TUI application for RELAIS chat.

    Args:
        config: TUI configuration (api_url, api_key, theme, …).
        config_path: Path to the YAML config file (for session ID persistence).
    """

    def __init__(self, config: Config, config_path: Path) -> None:
        self.config: Config = config
        self._config_path: Path = config_path
        self.chat_state: ChatState = ChatState()
        self._session_id: str | None = config.last_session_id or None
        self._should_exit: bool = False
        self._streaming: bool = False
        self._spinner_idx: int = 0
        self._spinner_task: asyncio.Task | None = None
        self._progress_lines: list[str] = []
        self._attachment: ImagePayload | None = None
        self._paste_block: PasteBlock | None = None

        # Banner is rendered once and stored as formatted-text tuples
        self._banner_text: list[tuple[str, str]] = self._build_banner_text()

        self._input_area = TextArea(
            height=3,
            multiline=True,
            scrollbar=True,
            prompt="you › ",
            focus_on_click=True,
        )
        self._wire_paste_interception()
        layout = self._build_layout()
        key_bindings = self._build_key_bindings()

        self._pt_app = Application(
            layout=layout,
            key_bindings=key_bindings,
            full_screen=True,
            mouse_support=False,
        )

        # Wire up chat_state listener → UI invalidation
        self.chat_state.add_listener(self._pt_app.invalidate)

        self._client: RelaisClient = RelaisClient(config)

    # ------------------------------------------------------------------
    # Paste interception
    # ------------------------------------------------------------------

    def _wire_paste_interception(self) -> None:
        """Monkey-patch buffer.insert_text to compact large pastes.

        When the user pastes ≥5 lines, the textarea shows a one-line
        summary (e.g. ``[lines pasted: +12 lines]``) while the full
        text is preserved in ``_paste_block`` for submission.
        """
        buf = self._input_area.buffer
        _original_insert = buf.insert_text

        def _intercepting_insert(data: str, **kwargs) -> None:
            if is_large_paste(data):
                block = summarize_paste(data)
                self._paste_block = block
                _original_insert(block.summary, **kwargs)
            else:
                _original_insert(data, **kwargs)

        buf.insert_text = _intercepting_insert  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------

    def _build_banner_text(self) -> list[tuple[str, str]]:
        """Build the ASCII-art banner as prompt_toolkit FormattedText tuples.

        Returns:
            List of (style, text) tuples shown at the top of the chat panel.
        """
        relais_ver, tui_ver = _get_versions()
        parts: list[tuple[str, str]] = []
        for line in [
            "██████╗ ███████╗██╗      █████╗ ██╗███████╗",
            "██╔══██╗██╔════╝██║     ██╔══██╗██║██╔════╝",
            "██████╔╝█████╗  ██║     ███████║██║███████╗",
            "██╔══██╗██╔══╝  ██║     ██╔══██║██║╚════██║",
            "██║  ██║███████╗███████╗██║  ██║██║███████║",
            "╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝╚══════╝",
        ]:
            parts.append(("fg:cyan bold", line + "\n"))
        parts.append(("fg:ansigray", f"core v{relais_ver}  ·  tui v{tui_ver}  ·  Autonomous AI assistant\n"))
        parts.append(("fg:ansigray", "/exit to quit · /clear to reset session · Esc+Enter or Ctrl+J for newline · Ctrl+P to attach image\n"))
        parts.append(("", "\n"))
        return parts

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        """Build the HSplit layout: framed chat panel + framed input area.

        Returns:
            A prompt_toolkit Layout ready to pass to Application.
        """
        chat_window = Window(
            content=FormattedTextControl(self.get_chat_text),
            wrap_lines=True,
            always_hide_cursor=True,
        )
        chat_frame = Frame(chat_window, title="Chat")
        input_frame = Frame(self._input_area, title="Input")

        root = HSplit([chat_frame, input_frame])
        return Layout(root, focused_element=self._input_area)

    def _build_key_bindings(self) -> KeyBindings:
        """Build key bindings for submit and newline insertion.

        Returns:
            KeyBindings instance wired to the input area.
        """
        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event) -> None:
            text = self._input_area.text
            self._input_area.text = ""
            self._handle_submit(text)

        @kb.add("escape", "enter")
        def _newline_esc(event) -> None:
            self._input_area.text += "\n"

        @kb.add("c-j")
        def _newline_ctrl_j(event) -> None:
            self._input_area.text += "\n"

        @kb.add("c-p")
        def _paste_image(event) -> None:
            payload = grab_image_from_clipboard()
            if payload:
                self._attachment = payload
                self._pt_app.invalidate()

        @kb.add("c-c")
        @kb.add("c-d")
        def _quit(event) -> None:
            self._should_exit = True
            self._pt_app.exit()

        return kb

    # ------------------------------------------------------------------
    # Formatted text rendering
    # ------------------------------------------------------------------

    def get_chat_text(self) -> list[tuple[str, str]]:
        """Format banner + all ChatMessages into (style, text) tuples.

        Called by FormattedTextControl on every UI refresh.  Shows the
        ASCII banner, conversation history, in-flight progress events,
        and an animated spinner when a response is streaming.

        Returns:
            List of (style_string, text_string) tuples for prompt_toolkit.
        """
        theme = self.config.theme
        parts: list[tuple[str, str]] = list(self._banner_text)
        msgs = self.chat_state.messages

        for i, msg in enumerate(msgs):
            is_last = i == len(msgs) - 1

            if msg.role == "user":
                parts.append((f"fg:{theme.user_text} bold", "you"))
                parts.append((f"fg:{theme.metadata}", " › "))
                parts.append((f"fg:{theme.user_text}", msg.content))
            else:
                parts.append((f"fg:{theme.assistant_text} bold", "relais"))
                parts.append((f"fg:{theme.metadata}", " › "))

                # While streaming the last message, show tool/event progress above content
                if is_last and self._streaming and self._progress_lines:
                    parts.append(("", "\n"))
                    for line in self._progress_lines[-8:]:
                        parts.append(("fg:ansigray italic", f"  ⋯ {line}\n"))

                parts.append((f"fg:{theme.assistant_text}", msg.content))

                # Animated spinner at the end of the last streaming message
                if is_last and self._streaming:
                    spinner = _SPINNER_CHARS[self._spinner_idx % len(_SPINNER_CHARS)]
                    parts.append(("fg:ansiyellow", f" {spinner}"))

            parts.append(("", "\n"))

        if self._attachment:
            parts.append(("fg:ansicyan bold", f"[attached] {self._attachment.name} ({self._attachment.mime_type})\n"))

        parts.append(("[SetCursorPosition]", ""))
        return parts

    # ------------------------------------------------------------------
    # Message flow helpers
    # ------------------------------------------------------------------

    def _add_user_message(self, text: str) -> None:
        """Add a user message to chat_state (invalidates UI via listener).

        Args:
            text: The user's message content.
        """
        self.chat_state.add_message("user", text)

    def _handle_submit(self, raw: str) -> None:
        """Process a submitted input line.

        Handles empty input (no-op), slash commands (/exit, /quit, /clear),
        and normal messages (adds to state, launches stream task).

        Args:
            raw: Raw text from the input area.
        """
        text = raw.strip()
        if not text:
            return

        # Restore full text if a large paste was compacted for display
        paste_block = self._paste_block
        self._paste_block = None
        if paste_block is not None:
            text = text.replace(paste_block.summary, paste_block.full_text, 1)

        cmd = text.lower()

        if cmd in ("/exit", "/quit"):
            self._should_exit = True
            self._pt_app.exit()
            return

        if cmd == "/clear":
            self._launch_clear_task()
            return

        attachment = self._attachment
        self._attachment = None
        self._add_user_message(text)
        self.chat_state.add_message("assistant", "")
        self._launch_stream_task(text, attachment)

    def _launch_stream_task(self, text: str, attachment: ImagePayload | None = None) -> None:
        """Schedule _stream_to_state as an asyncio task.

        Args:
            text: The user message to stream.
            attachment: Optional image payload to send inline with the message.
        """
        loop = asyncio.get_event_loop()
        loop.create_task(self._stream_to_state(text, self._session_id, attachment))

    def _launch_clear_task(self) -> None:
        """Schedule _do_clear as an asyncio task."""
        loop = asyncio.get_event_loop()
        loop.create_task(self._do_clear())

    # ------------------------------------------------------------------
    # Spinner animation
    # ------------------------------------------------------------------

    async def _spin(self) -> None:
        """Increment spinner index and invalidate the UI at 150 ms intervals.

        Runs as a background task while ``_streaming`` is True.
        """
        while self._streaming:
            self._spinner_idx += 1
            self._pt_app.invalidate()
            await asyncio.sleep(0.15)

    # ------------------------------------------------------------------
    # Async streaming
    # ------------------------------------------------------------------

    async def _stream_to_state(
        self,
        content: str,
        session_id: str | None,
        attachment: ImagePayload | None = None,
    ) -> None:
        """Stream a message from the API and write tokens/progress to chat_state.

        Manages the spinner task lifecycle, appends progress event lines
        (tool calls, tool results) to ``_progress_lines`` so they appear
        in the chat panel while streaming, then clears them on completion.

        Args:
            content: User message to send.
            session_id: Current session ID (may be None).
            attachment: Optional inline image to attach to the request.
        """
        self._streaming = True
        self._progress_lines = []
        loop = asyncio.get_event_loop()
        self._spinner_task = loop.create_task(self._spin())

        media_refs: list | None = None
        if attachment is not None:
            media_refs = [{
                "media_id": "",
                "path": attachment.name,
                "mime_type": attachment.mime_type,
                "size_bytes": 0,
                "data_base64": attachment.data,
            }]

        try:
            async for ev in self._client.stream_message(content, session_id=session_id, media_refs=media_refs):
                if isinstance(ev, TokenEvent):
                    self.chat_state.update_last_message(ev.text)

                elif isinstance(ev, DoneEvent):
                    last = self.chat_state.last_message()
                    if last and not last.content and ev.content:
                        self.chat_state.set_last_message_content(ev.content)

                    if ev.session_id and ev.session_id != session_id:
                        self._session_id = ev.session_id
                        self._persist_session_id(ev.session_id)

                elif isinstance(ev, ProgressEvent):
                    detail = ev.detail or ""
                    line = ev.event + (f": {detail}" if detail else "")
                    self._progress_lines.append(line)
                    self._pt_app.invalidate()
                    _log.debug("progress: %s %s", ev.event, detail)

                elif isinstance(ev, ErrorEvent):
                    _log.warning("stream error event: %s", ev.error)
                    last = self.chat_state.last_message()
                    if last and last.role == "assistant":
                        self.chat_state.set_last_message_content(
                            f"[Error: {ev.error}]"
                        )

        except asyncio.CancelledError:
            _log.info("Streaming cancelled")
            raise

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, OSError) as exc:
            _log.exception("Stream request failed: %s", exc)
            last = self.chat_state.last_message()
            if last and last.role == "assistant" and not last.content:
                self.chat_state.set_last_message_content(
                    f"[Connection error: {type(exc).__name__}]"
                )

        finally:
            self._streaming = False
            self._progress_lines = []
            if self._spinner_task and not self._spinner_task.done():
                self._spinner_task.cancel()
                try:
                    await self._spinner_task
                except asyncio.CancelledError:
                    pass
            self._spinner_task = None
            self._pt_app.invalidate()

    async def _do_clear(self) -> None:
        """Send /clear to the server, reset chat_state and session_id."""
        new_session_id = str(uuid4())

        try:
            async for ev in self._client.stream_message(
                "/clear", session_id=self._session_id
            ):
                if isinstance(ev, DoneEvent) and ev.session_id:
                    new_session_id = ev.session_id
        except Exception as exc:
            _log.exception("Clear failed: %s", exc)

        self.chat_state.clear()
        self._session_id = new_session_id
        self._persist_session_id(new_session_id)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def _show_history(self, session_id: str, limit: int = 10) -> None:
        """Fetch and populate chat_state with prior session turns.

        Silently does nothing if the history endpoint returns no turns
        (new session or server unavailable).

        Args:
            session_id: Session whose history to display.
            limit: Maximum number of turns to fetch.
        """
        turns = await self._client.fetch_history(session_id, limit=limit)
        if not turns:
            return

        for turn in turns:
            user_msg = (turn.get("user_content") or "").strip()
            asst_msg = (turn.get("assistant_content") or "").strip()
            if user_msg:
                self.chat_state.add_message("user", user_msg)
            if asst_msg:
                self.chat_state.add_message("assistant", asst_msg)

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _persist_session_id(self, session_id: str) -> None:
        """Save the new session_id to the config file.

        Args:
            session_id: The new session ID to persist.
        """
        updated = _dataclass_replace(self.config, last_session_id=session_id)
        self.config = updated
        try:
            save_config(updated, self._config_path)
        except Exception:
            _log.exception("Failed to persist session_id")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the full-screen application.

        Performs health check, loads history if available, then starts
        the prompt_toolkit event loop.  Always closes the client on exit.
        """
        try:
            ok = await self._client.healthz()
            if ok:
                _log.info("Connected to RELAIS at %s", self.config.api_url)
                if self._session_id:
                    await self._show_history(self._session_id)
            else:
                _log.warning("Cannot reach RELAIS at %s", self.config.api_url)
                self.chat_state.add_message(
                    "assistant",
                    f"Cannot reach {self.config.api_url} — "
                    "make sure the RELAIS stack is running.",
                )

            await self._pt_app.run_async()

        finally:
            await self._client.close()
