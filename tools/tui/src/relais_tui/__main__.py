"""RELAIS TUI — Textual chat application.

Entry point for the ``relais`` and ``relais-tui`` CLI commands.

Usage::

    relais                     # start with default config (~/.relais/config/tui/config.yaml)
    RELAIS_TUI_API_KEY=xyz relais  # override API key via env var
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import sys
import tomllib
from dataclasses import replace as _dataclass_replace
from io import StringIO
from pathlib import Path
from uuid import uuid4

import httpx
from rich.console import Console
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import Worker
from textual.widgets import Footer, Header, Input, LoadingIndicator, Log, Static, TabbedContent, TabPane

from relais_tui.config import Config, load_config, save_config
from relais_tui.client import RelaisClient
from relais_tui.sse_parser import DoneEvent, ErrorEvent, ProgressEvent, TokenEvent
from relais_tui.screens.bundles_screen import BundlesScreen

_log = logging.getLogger(__name__)

# Phrases emitted by Souvenir's ClearHandler that indicate a successful /clear.
# Kept at module level to avoid recreating the tuple on every DoneEvent.
_CLEAR_PHRASES = frozenset([
    "✓ Conversation history cleared.",
    "Conversation history cleared.",
])

def _get_versions() -> tuple[str, str]:
    """Return (relais_core_version, tui_version).

    TUI version comes from the installed package metadata.
    RELAIS core version is read from the root pyproject.toml because the core
    package is not installed inside the TUI venv.
    """
    try:
        tui_ver = importlib.metadata.version("relais-tui")
    except importlib.metadata.PackageNotFoundError:
        tui_ver = "?"

    try:
        # __file__ lives at tools/tui/src/relais_tui/__main__.py
        # parents[4] = project root
        root_toml = Path(__file__).parents[4] / "pyproject.toml"
        with root_toml.open("rb") as fh:
            relais_ver = tomllib.load(fh)["project"]["version"]
    except Exception:
        try:
            relais_ver = importlib.metadata.version("relais")
        except importlib.metadata.PackageNotFoundError:
            relais_ver = "?"

    return relais_ver, tui_ver


def _build_splash(config_path: Path, log_path: Path) -> str:
    relais_ver, tui_ver = _get_versions()
    return (
        "[bold cyan]\n"
        "██████╗ ███████╗██╗      █████╗ ██╗███████╗\n"
        "██╔══██╗██╔════╝██║     ██╔══██╗██║██╔════╝\n"
        "██████╔╝█████╗  ██║     ███████║██║███████╗\n"
        "██╔══██╗██╔══╝  ██║     ██╔══██║██║╚════██║\n"
        "██║  ██║███████╗███████╗██║  ██║██║███████║\n"
        "╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝╚══════╝[/bold cyan]\n"
        f"[dim]core v{relais_ver}  ·  tui v{tui_ver}  ·  Autonomous AI assistant[/dim]\n"
        f"[dim]config  {config_path}[/dim]\n"
        f"[dim]logs    {log_path}[/dim]\n"
    )

_OFFLINE_BANNER = """\
[bold red]
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║          ⚠  RELAIS BACKEND IS NOT ACCESSIBLE  ⚠             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝[/bold red]

[yellow]The REST adapter could not be reached at:[/yellow]
  [italic]{url}[/italic]

[dim]• Make sure the RELAIS stack is running  (./supervisor.sh start all)
• Check api_url in  ~/.relais/config/tui/config.yaml
• Type [bold]/exit[/bold] or press [bold]Ctrl+Q[/bold] to quit[/dim]
"""

_CSS = """
Screen {
    layout: vertical;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    layout: vertical;
    padding: 0;
}

Header {
    height: 1;
    background: #0f3460;
    color: #8be9fd;
}

#chat-log {
    height: 1fr;
    padding: 0 1;
    background: #1a1a2e;
    scrollbar-gutter: stable;
    border: none;
}

#streaming {
    height: auto;
    min-height: 1;
    padding: 0 1;
    background: #1a1a2e;
    color: #f8f8f2;
}

#spinner {
    display: none;
    height: 1;
    background: #1a1a2e;
    color: #50fa7b;
}

#spinner.active {
    display: block;
}

#msg-input {
    height: 3;
    background: #16213e;
    border: tall #0f3460;
    color: #f8f8f2;
    padding: 0 1;
}

#msg-input:focus {
    border: tall #50fa7b;
}

#status {
    height: 1;
    background: #16213e;
    color: #6272a4;
    padding: 0 1;
}

/* Full-screen offline overlay */
#offline-overlay {
    display: none;
    layer: dialog;
    width: 100%;
    height: 100%;
    background: #1a1a2e 80%;
    align: center middle;
    padding: 2 4;
    color: #f8f8f2;
}

#offline-overlay.visible {
    display: block;
}
"""


class RelaisApp(App[None]):
    """RELAIS terminal chat client.

    Connects to the RELAIS REST SSE API and provides an interactive
    streaming chat interface.
    """

    CSS = _CSS

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear_chat", "Clear"),
        Binding("escape", "stop_stream", "Stop", show=False),
        Binding("ctrl+b", "switch_tab('bundles')", "Bundles"),
    ]

    # Accumulates streaming tokens for the in-progress assistant reply.
    _buf: reactive[str] = reactive("", layout=False)

    def __init__(self, config: Config, config_path: Path, log_path: Path) -> None:
        """Initialise the application.

        Args:
            config: TUI configuration (URL, API key, session behaviour, …).
            config_path: Resolved path to the loaded config file.
            log_path: Path where TUI logs are written.
        """
        super().__init__()
        self._config = config
        self._config_path = config_path
        self._log_path = log_path
        self._client = RelaisClient(config)
        # Restore last session from config; may be overwritten by DoneEvent
        self._session_id: str | None = config.last_session_id or None
        self._busy = False
        self._stream_worker: Worker | None = None

        # Dynamically apply theme from config to CSS
        self._apply_theme_to_css()

    def _apply_theme_to_css(self) -> None:
        """Replace color placeholders in CSS with values from config theme."""
        theme = self._config.theme
        replacements = {
            "#1a1a2e": theme.background,
            "#8be9fd": theme.user_text,
            "#f8f8f2": theme.assistant_text,
            "#282a36": theme.code_block,
            "#6272a4": theme.metadata,
            "#16213e": theme.status_bar,
            "#50fa7b": theme.accent,
            "#ff5555": theme.error,
        }
        css = self.CSS
        for old, new in replacements.items():
            css = css.replace(old, new)
        
        # Specific override for UI components
        css = css.replace("#0f3460", theme.status_bar) 

        self.CSS = css

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield Header(show_clock=True)
        with TabbedContent(initial="chat"):
            with TabPane("Chat", id="chat"):
                yield Log(id="chat-log", highlight=True)
                yield Static("", id="streaming", markup=True)
                yield LoadingIndicator(id="spinner")
                yield Input(
                    placeholder="Type a message  ·  ESC = stop  ·  Ctrl+L = clear  ·  /exit or Ctrl+Q = quit",
                    id="msg-input",
                )
                yield Static("Connecting…", id="status", markup=True)
                yield Static("", id="offline-overlay", markup=True)
            with TabPane("Bundles", id="bundles"):
                yield BundlesScreen()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """Run after the DOM is ready: show splash, health-check, focus input."""
        log = self.query_one("#chat-log", Log)
        log.write_line(self._render_markup(_build_splash(self._config_path, self._log_path)))
        self._healthcheck()
        if self._session_id:
            self.run_worker(self._load_history(), exclusive=False)
        self.query_one("#msg-input", Input).focus()

    async def on_unmount(self) -> None:
        """Close the HTTP client when the application exits."""
        await self._client.close()

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch__buf(self, value: str) -> None:
        """Reflect the streaming buffer in the #streaming widget."""
        streaming = self.query_one("#streaming", Static)
        if value:
            accent = self._config.theme.accent
            streaming.update(f"[bold {accent}]RELAIS:[/bold {accent}] {value}[blink]▌[/blink]")
        else:
            streaming.update("")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send the typed message when the user presses Enter."""
        content = event.value.strip()
        if not content or self._busy:
            return
        event.input.clear()
        if content.lower() in ("/exit", "/quit"):
            self.exit()
            return
        self._busy = True
        self._stream_worker = self._send(content)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_chat(self) -> None:
        """Clear the chat log and generate a new session_id."""
        self._session_id = str(uuid4())
        self._save_session_id()
        self.query_one("#chat-log", Log).clear()

    def action_stop_stream(self) -> None:
        """Cancel the in-progress stream (ESC key).

        When a reply is streaming, cancels the worker and lets the
        CancelledError handler display a stop notice in the chat log.
        If no stream is active the action is silently ignored.

        Returns:
            None. Side effect: the active worker is cancelled when busy.
        """
        if self._busy and self._stream_worker is not None:
            self._stream_worker.cancel()

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch the active TabbedContent tab by id (e.g. Ctrl+B → bundles).

        Args:
            tab_id: The ``id`` attribute of the target ``TabPane``.
        """
        self.query_one(TabbedContent).active = tab_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_markup(self, text: str) -> str:
        """Convert Rich markup string to an ANSI-escaped plain string.

        The ``Log`` widget does not understand Rich markup natively, so we
        pre-render it through a Rich ``Console`` that writes ANSI escape
        codes to a ``StringIO`` buffer.

        Args:
            text: A string that may contain Rich markup tags such as
                ``[bold]``, ``[green]``, ``[dim]``, etc.

        Returns:
            The rendered string with ANSI escape codes (no raw markup tags).
        """
        buf = StringIO()
        console = Console(force_terminal=True, file=buf, width=120)
        console.print(text, end="")
        return buf.getvalue()

    def _save_session_id(self) -> None:
        """Persist the current session ID back to the config file on disk.

        Updates ``self._config`` (via ``dataclasses.replace``) and then
        writes the full config to ``self._config_path`` using
        ``save_config``.

        Returns:
            None. Side effect: config file is updated on disk.
        """
        session_str = self._session_id or ""
        self._config = _dataclass_replace(self._config, last_session_id=session_str)
        save_config(self._config, self._config_path)

    async def _load_history(self) -> None:
        """Load conversation history for the current session and display it.

        Calls ``RelaisClient.fetch_history()`` and renders up to 20 turns
        in the chat log.  Silently exits if no turns are returned or if the
        session ID is not set.

        Returns:
            None. Side effect: turns are written to the ``#chat-log`` widget.
        """
        if not self._session_id:
            return
        turns = await self._client.fetch_history(self._session_id, limit=20)
        if not turns:
            return
        log = self.query_one("#chat-log", Log)
        total = len(turns)
        displayed = min(total, 20)
        if total > displayed:
            log.write_line(
                self._render_markup(f"[dim]... {total - displayed} turns précédents[/dim]")
            )
        log.write_line(self._render_markup("[dim]--- Historique ---[/dim]"))
        for turn in turns[-displayed:]:
            if turn.get("user_content"):
                log.write_line(
                    self._render_markup(f"[bold]Vous :[/bold] {turn['user_content']}")
                )
            if turn.get("assistant_content"):
                log.write_line(
                    self._render_markup(
                        f"[green]Assistant :[/green] {turn['assistant_content']}"
                    )
                )
        log.write_line(self._render_markup("[dim]--- Fin historique ---[/dim]"))

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    @work(exclusive=False)
    async def _healthcheck(self) -> None:
        """Ping /healthz and update the status bar (or show offline overlay)."""
        ok = await self._client.healthz()
        status = self.query_one("#status", Static)
        if ok:
            status.update(f"[green]Connected[/green] · {self._config.api_url}")
        else:
            status.update(
                f"[bold red]Unreachable[/bold red] · {self._config.api_url}"
            )
            overlay = self.query_one("#offline-overlay", Static)
            overlay.update(_OFFLINE_BANNER.format(url=self._config.api_url))
            overlay.add_class("visible")
            # Disable input while offline
            self.query_one("#msg-input", Input).disabled = True

    @work(exclusive=True)
    async def _send(self, content: str) -> None:
        """Send a message and stream the reply into the chat log.

        Runs as an exclusive worker so concurrent sends are not possible.

        Args:
            content: The user message text.
        """
        log = self.query_one("#chat-log", Log)
        status = self.query_one("#status", Static)

        _log.debug("_send: entered, content_len=%d, session_id=%s", len(content), self._session_id)
        user_color = self._config.theme.user_text
        log.write_line(self._render_markup(f"[bold {user_color}]You:[/bold {user_color}] {content}"))

        buf = ""
        self._buf = ""
        spinner = self.query_one("#spinner", LoadingIndicator)
        spinner.add_class("active")

        try:
            async for ev in self._client.stream_message(
                content, session_id=self._session_id
            ):
                if isinstance(ev, TokenEvent):
                    if not buf:
                        # First token received — hide the spinner
                        spinner.remove_class("active")
                    buf += ev.text
                    self._buf = buf
                    # Yield to the Textual event loop so the reactive watcher
                    # can schedule a repaint before the next token arrives.
                    # Without this, multiple tokens from a single TCP chunk are
                    # processed in a tight loop and only the last value is
                    # painted (the message appears as a block at the end).
                    await asyncio.sleep(0)

                elif isinstance(ev, DoneEvent):
                    spinner.remove_class("active")
                    # Update session_id only when the server returns a new one
                    # and it actually differs from the current value — avoids
                    # redundant disk writes on every reply.
                    new_id = ev.session_id or self._session_id
                    if new_id and new_id != self._session_id:
                        self._session_id = new_id
                        self._save_session_id()
                    else:
                        self._session_id = new_id
                    # Non-streaming fallback: server returned full content at once
                    if not buf and ev.content:
                        buf = ev.content
                    # Detect /clear confirmation — generate a new session
                    if buf.strip() in _CLEAR_PHRASES:
                        self._session_id = str(uuid4())
                        self._save_session_id()
                        log.clear()
                        buf = ""
                    self._buf = ""
                    if buf:
                        accent = self._config.theme.accent
                        log.write_line(self._render_markup(f"[bold {accent}]RELAIS:[/bold {accent}] {buf}"))
                    session_short = (
                        (self._session_id[:8] + "…") if self._session_id else "–"
                    )
                    status.update(f"[green]Connected[/green] · session={session_short}")

                elif isinstance(ev, ProgressEvent):
                    status.update(
                        f"[yellow]{ev.event}[/yellow] · {ev.detail or '…'}"
                    )

                elif isinstance(ev, ErrorEvent):
                    spinner.remove_class("active")
                    self._buf = ""
                    log.write_line(self._render_markup(f"[bold red]Error:[/bold red] {ev.error}"))
                    status.update(f"[red]Error[/red] · {ev.error[:80]}")

        except asyncio.CancelledError:
            self._buf = ""
            log.write_line(self._render_markup("[dim]⏹ Stream interrompu.[/dim]"))
            raise  # must re-raise so Textual marks the worker as cancelled

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, OSError) as exc:
            self._buf = ""
            _log.exception("Stream request failed: %s", exc)
            error_label = type(exc).__name__
            log.write_line(self._render_markup(f"[bold red]Connection error:[/bold red] {error_label}"))
            status.update(f"[red]Connection error[/red] · {error_label}")

        except Exception as exc:  # noqa: BLE001
            self._buf = ""
            _log.exception("Unexpected error in _send: %s", exc)
            log.write_line(self._render_markup(f"[bold red]Error:[/bold red] {exc}"))
            status.update(f"[red]Error[/red] · {type(exc).__name__}")

        finally:
            spinner.remove_class("active")
            self._buf = ""  # safety reset — already cleared per-branch but ensures cleanup on unexpected exit
            self._stream_worker = None  # clear reference before opening the gate
            self._busy = False
            self.query_one("#msg-input", Input).focus()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _bootstrap_relais_home() -> None:
    """Set RELAIS_HOME from the project .env if not already in the environment.

    Walks up from this file (tools/tui/src/relais_tui/__main__.py, so
    parents[4] = project root) to locate .env and loads it with dotenv
    (override=False so existing env vars are never clobbered).  Skips if
    RELAIS_HOME is already set by the caller (e.g. the ./relais wrapper).
    After loading, any relative RELAIS_HOME is resolved against the project
    root so downstream code always sees an absolute path.
    """
    import os as _os

    from dotenv import load_dotenv

    if _os.environ.get("RELAIS_HOME"):
        return

    project_root = Path(__file__).parents[4]
    load_dotenv(project_root / ".env", override=False)

    # Resolve relative path against the project root
    relais_home = _os.environ.get("RELAIS_HOME")
    if relais_home and not Path(relais_home).is_absolute():
        _os.environ["RELAIS_HOME"] = str((project_root / relais_home).resolve())


def _run_bundle_cli() -> None:
    """Dispatch ``relais bundle ...`` subcommands and exit.

    Called when ``sys.argv[1] == "bundle"`` so the TUI is never launched for
    CLI-only operations.  Exits with the handler's return code.
    """
    import argparse

    from relais_tui.cli.bundle import add_bundle_subparser

    root = argparse.ArgumentParser(prog="relais", description="RELAIS — micro-brick AI assistant CLI.")
    root_sub = root.add_subparsers(dest="command", metavar="{bundle,...}")
    root_sub.required = True
    add_bundle_subparser(root_sub)

    args = root.parse_args()
    if not hasattr(args, "func"):
        root.print_help()
        sys.exit(2)
    sys.exit(args.func(args))


def main() -> None:
    """Load config and start the TUI application, or dispatch bundle CLI."""
    _bootstrap_relais_home()

    if len(sys.argv) > 1 and sys.argv[1] == "bundle":
        _run_bundle_cli()

    from relais_tui.config import _default_config_path  # noqa: PLC2701

    relais_home = os.environ.get("RELAIS_HOME")
    config_path = _default_config_path().expanduser().resolve()
    log_path = (
        (Path(relais_home) / "logs" / "tui.log").resolve()
        if relais_home
        else Path("~/.relais/logs/tui.log").expanduser()
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # buffering=1 → flush après chaque ligne, sans délai
    _log_file = log_path.open("a", buffering=1, encoding="utf-8")
    _handler = logging.StreamHandler(_log_file)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s")
    )
    logging.root.setLevel(logging.DEBUG)
    logging.root.addHandler(_handler)

    config = load_config(config_path)
    _log.info("RELAIS TUI starting — api_url=%s config=%s logs=%s", config.api_url, config_path, log_path)

    RelaisApp(config, config_path, log_path).run()


if __name__ == "__main__":
    main()
