"""RELAIS TUI — prompt_toolkit + rich REPL-style chat client.

Entry point for the ``relais`` and ``relais-tui`` CLI commands.

Usage::

    relais                      # start with default config
    RELAIS_TUI_API_KEY=xyz relais  # override API key via env var
    relais bundle install …     # bundle subcommands (no TUI started)
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import sys
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from uuid import uuid4

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from relais_tui.client import RelaisClient
from relais_tui.config import Config, ThemeConfig, load_config, save_config
from relais_tui.md_stream import MarkdownStream
from relais_tui.sse_parser import DoneEvent, ErrorEvent, ProgressEvent, TokenEvent

_log = logging.getLogger(__name__)

_CLEAR_PHRASES = frozenset([
    "✓ Conversation history cleared.",
    "Conversation history cleared.",
])


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def _get_versions() -> tuple[str, str]:
    """Return (relais_core_version, tui_version).

    Args: none

    Returns:
        Tuple of (core_version, tui_version) strings; unknown versions are "?".
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


def _build_splash(config_path: Path, log_path: Path) -> str:
    """Build the startup splash banner.

    Args:
        config_path: Resolved path to the loaded config file.
        log_path: Path where TUI logs are written.

    Returns:
        Rich markup string for the banner.
    """
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
        "[dim]Type [bold]/exit[/bold] to quit · [bold]/clear[/bold] to reset session · "
        "[bold]Esc+Enter[/bold] or [bold]Ctrl+J[/bold] for newline[/dim]\n"
    )


# ---------------------------------------------------------------------------
# Prompt toolkit setup
# ---------------------------------------------------------------------------


def _make_key_bindings() -> KeyBindings:
    """Build key bindings: Enter submits, Escape+Enter inserts a newline.

    Note: Most terminal emulators do not distinguish Shift+Enter from Enter.
    Use Escape then Enter (or Ctrl+J) to insert a literal newline in the prompt.

    Returns:
        A KeyBindings instance with submit and newline bindings.
    """
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()  # type: ignore[attr-defined]

    @kb.add("escape", "enter")
    def _newline_esc(event: object) -> None:
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    @kb.add("c-j")
    def _newline_ctrl_j(event: object) -> None:
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    return kb


def _make_prompt_message(theme: ThemeConfig) -> HTML:
    """Build the HTML-formatted prompt message using theme colors.

    Args:
        theme: Theme configuration with user_text and metadata colors.

    Returns:
        HTML-formatted prompt string for PromptSession.
    """
    user_color = theme.user_text
    arrow_color = theme.metadata
    return HTML(
        f'<style fg="{user_color}">you</style>'
        f'<style fg="{arrow_color}"> › </style>'
    )


def _make_session(config: Config) -> PromptSession:
    """Build a configured PromptSession with file history and key bindings.

    Args:
        config: TUI configuration with history_path.

    Returns:
        A ready-to-use PromptSession.
    """
    history_path = Path(config.history_path).expanduser()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    return PromptSession(
        history=FileHistory(str(history_path)),
        key_bindings=_make_key_bindings(),
        multiline=True,
        enable_open_in_editor=True,
    )


# ---------------------------------------------------------------------------
# Message sending / streaming
# ---------------------------------------------------------------------------


async def _stream_response(
    client: RelaisClient,
    content: str,
    session_id: str | None,
    console: Console,
    config: Config,
    config_path: Path,
) -> tuple[str | None, str]:
    """Stream a message to the RELAIS API and render the response.

    Yields tokens via MarkdownStream (sliding window), then prints the final
    complete response as rich Markdown.

    Args:
        client: The HTTP client.
        content: User message text.
        session_id: Current session ID (may be None).
        console: Rich console for output.
        config: TUI configuration.
        config_path: Path to config file for session ID persistence.

    Returns:
        Tuple of (new_session_id, assistant_content).  new_session_id may be
        None if the server did not return one.
    """
    theme = config.theme
    buf = ""
    new_session_id = session_id

    console.print(
        HTML(
            f'<style fg="{theme.assistant_text}">relais</style>'
            f'<style fg="{theme.metadata}"> › </style>'
        )
    )

    stream = MarkdownStream(console)

    try:
        async for ev in client.stream_message(content, session_id=session_id):
            if isinstance(ev, TokenEvent):
                buf += ev.text
                stream.update(buf)

            elif isinstance(ev, DoneEvent):
                if not buf and ev.content:
                    buf = ev.content
                stream.update(buf, final=True)

                if ev.session_id and ev.session_id != session_id:
                    new_session_id = ev.session_id

            elif isinstance(ev, ProgressEvent):
                console.print(
                    f"[{theme.metadata}]⋯ {ev.event}[/{theme.metadata}]"
                    + (f" · {ev.detail}" if ev.detail else ""),
                )

            elif isinstance(ev, ErrorEvent):
                stream.update(buf, final=True)
                console.print(f"[{theme.error}]Error: {ev.error}[/{theme.error}]")

    except asyncio.CancelledError:
        stream.update(buf, final=True)
        if buf:
            console.print(f"\n[{theme.metadata}]⏹ Streaming stopped.[/{theme.metadata}]")
        raise

    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, OSError) as exc:
        stream.update(buf, final=True)
        console.print(f"[{theme.error}]Connection error: {type(exc).__name__}[/{theme.error}]")
        _log.exception("Stream request failed: %s", exc)

    return new_session_id, buf


async def _do_clear(
    client: RelaisClient,
    session_id: str | None,
    console: Console,
    config: Config,
    config_path: Path,
) -> str:
    """Send /clear to the server and return a fresh session ID.

    Args:
        client: The HTTP client.
        session_id: Current session ID.
        console: Rich console for output.
        config: TUI configuration.
        config_path: Path to config file.

    Returns:
        New session ID string.
    """
    theme = config.theme
    stream = MarkdownStream(console)
    new_session_id = str(uuid4())

    try:
        async for ev in client.stream_message("/clear", session_id=session_id):
            if isinstance(ev, TokenEvent):
                pass  # discard /clear tokens
            elif isinstance(ev, DoneEvent):
                stream.update(ev.content or "Cleared.", final=True)
                if ev.session_id:
                    new_session_id = ev.session_id
            elif isinstance(ev, ErrorEvent):
                stream.update("", final=True)
                console.print(f"[{theme.error}]Clear failed: {ev.error}[/{theme.error}]")
    except Exception as exc:
        stream.update("", final=True)
        console.print(f"[{theme.error}]Clear error: {exc}[/{theme.error}]")

    # Persist new session ID
    updated = _dataclass_replace(config, last_session_id=new_session_id)
    save_config(updated, config_path)
    return new_session_id


# ---------------------------------------------------------------------------
# History display
# ---------------------------------------------------------------------------


async def _show_history(
    client: "RelaisClient",
    session_id: str,
    console: Console,
    config: Config,
    limit: int = 10,
) -> None:
    """Fetch and display the last *limit* turns of a session.

    Silently does nothing if the /v1/history endpoint is unavailable or
    returns no turns.

    Args:
        client: The HTTP client.
        session_id: Session whose history to display.
        console: Rich console for output.
        config: TUI configuration (for theme colors).
        limit: Maximum number of turns to display.
    """
    turns = await client.fetch_history(session_id, limit=limit)
    if not turns:
        return

    theme = config.theme
    sep = "─" * 40
    console.print(f"[{theme.metadata}]{sep}[/{theme.metadata}]")
    console.print(
        f"[{theme.metadata}]  Reprise · session {session_id[:8]}…  "
        f"({len(turns)} tour{'s' if len(turns) > 1 else ''})[/{theme.metadata}]"
    )
    console.print(f"[{theme.metadata}]{sep}[/{theme.metadata}]\n")

    for turn in turns:
        user_msg = (turn.get("user_content") or "").strip()
        asst_msg = (turn.get("assistant_content") or "").strip()
        if user_msg:
            console.print(
                f"[{theme.user_text}]you[/{theme.user_text}]"
                f"[{theme.metadata}] › [/{theme.metadata}]"
                f"{user_msg}"
            )
        if asst_msg:
            console.print(
                f"[{theme.assistant_text}]relais[/{theme.assistant_text}]"
                f"[{theme.metadata}] › [/{theme.metadata}]"
            )
            console.print(Markdown(asst_msg))
        console.print()

    console.print(f"[{theme.metadata}]{sep}[/{theme.metadata}]\n")


# ---------------------------------------------------------------------------
# Main application loop
# ---------------------------------------------------------------------------


async def run_app(config: Config, config_path: Path, log_path: Path) -> None:
    """Run the REPL-style chat loop.

    Displays the splash banner, performs a health check, then enters the
    main prompt loop.  Exits on /exit or EOF (Ctrl+D).

    Args:
        config: TUI configuration.
        config_path: Resolved path to the config file.
        log_path: Path where TUI logs are written.
    """
    theme = config.theme
    console = Console(highlight=False)

    console.print(
        _build_splash(config_path, log_path),
        markup=True,
        highlight=False,
    )

    client = RelaisClient(config)
    session_id: str | None = config.last_session_id or None

    # Health check
    ok = await client.healthz()
    if ok:
        console.print(f"[{theme.accent}]✓ Connected[/{theme.accent}] · {config.api_url}\n")
    else:
        console.print(
            f"[{theme.error}]✗ Cannot reach[/{theme.error}] {config.api_url}\n"
            f"[{theme.metadata}]Make sure the RELAIS stack is running "
            f"(./supervisor.sh start all)[/{theme.metadata}]\n"
        )

    # Restore last session context
    if ok and session_id:
        await _show_history(client, session_id, console, config)

    pt_session = _make_session(config)
    prompt_msg = _make_prompt_message(theme)

    try:
        while True:
            try:
                raw = await pt_session.prompt_async(prompt_msg)
            except (EOFError, KeyboardInterrupt):
                console.print(f"\n[{theme.metadata}]Goodbye.[/{theme.metadata}]")
                break

            text = raw.strip()
            if not text:
                continue

            cmd = text.lower()
            if cmd in ("/exit", "/quit"):
                console.print(f"[{theme.metadata}]Goodbye.[/{theme.metadata}]")
                break

            if cmd == "/clear":
                session_id = await _do_clear(client, session_id, console, config, config_path)
                continue

            new_sid, reply = await _stream_response(
                client, text, session_id, console, config, config_path
            )

            if new_sid and new_sid != session_id:
                session_id = new_sid
                updated = _dataclass_replace(config, last_session_id=session_id)
                save_config(updated, config_path)
                config = updated

            # Handle server-side /clear response
            if reply.strip() in _CLEAR_PHRASES:
                session_id = str(uuid4())
                updated = _dataclass_replace(config, last_session_id=session_id)
                save_config(updated, config_path)
                config = updated

            console.print()  # blank line between turns

    finally:
        await client.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _bootstrap_relais_home() -> None:
    """Set RELAIS_HOME from the project .env if not already in the environment.

    Walks up from this file to locate .env and loads it with dotenv
    (override=False so existing env vars are never clobbered).

    Returns: nothing.
    """
    import os as _os

    from dotenv import load_dotenv

    if _os.environ.get("RELAIS_HOME"):
        return

    project_root = Path(__file__).parents[4]
    load_dotenv(project_root / ".env", override=False)

    relais_home = _os.environ.get("RELAIS_HOME")
    if relais_home and not Path(relais_home).is_absolute():
        _os.environ["RELAIS_HOME"] = str((project_root / relais_home).resolve())


def _run_bundle_cli() -> None:
    """Dispatch ``relais bundle ...`` subcommands and exit.

    Returns: nothing (calls sys.exit internally).
    """
    import argparse

    from relais_tui.cli.bundle import add_bundle_subparser

    root = argparse.ArgumentParser(
        prog="relais", description="RELAIS — micro-brick AI assistant CLI."
    )
    root_sub = root.add_subparsers(dest="command", metavar="{bundle,...}")
    root_sub.required = True
    add_bundle_subparser(root_sub)

    args = root.parse_args()
    if not hasattr(args, "func"):
        root.print_help()
        sys.exit(2)
    sys.exit(args.func(args))


def main() -> None:
    """Load config and start the TUI application, or dispatch bundle CLI.

    Returns: nothing.
    """
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

    _log_file = log_path.open("a", buffering=1, encoding="utf-8")
    _handler = logging.StreamHandler(_log_file)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s")
    )
    logging.root.setLevel(logging.DEBUG)
    logging.root.addHandler(_handler)
    _log.info("RELAIS TUI starting — log=%s RELAIS_HOME=%s", log_path, relais_home)

    config = load_config(config_path)
    _log.info("config loaded — api_url=%s config=%s", config.api_url, config_path)

    from relais_tui.app import RelaisApp

    app = RelaisApp(config, config_path)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
