# RELAIS TUI Client — Implementation Plan

**Status**: Approved, ready for implementation
**Date**: 2026-04-14
**Branch**: feat/whatsapp-adapter (or new branch feat/tui)

## Overview

Standalone Terminal User Interface client for RELAIS. Connects exclusively via the REST SSE API. Zero dependency on RELAIS internals — can be extracted to its own repo.

## Location

`tools/tui/` at repo root, with own `pyproject.toml`. Installable as `pip install relais-tui` or `uv pip install -e tools/tui`.

## File Tree

```
tools/tui/
  pyproject.toml
  README.md
  src/
    relais_tui/
      __init__.py
      __main__.py              # entry point (python -m relais_tui)
      app.py                   # Textual App subclass (widgets, Workers, commands)
      config.py                # Config dataclass, YAML load/save, defaults
      client.py                # httpx API client (send, stream_sse, healthz)
      sse_parser.py            # Stateful SSE line parser
      history.py               # Save/load/list conversation JSON files
      theme.py                 # YAML theme -> textual CSS variables
      widgets/
        __init__.py
        chat_log.py            # Scrollable conversation log (RichLog + markdown)
        input_box.py           # TextArea: Enter=send, Shift+Enter=newline
        status_bar.py          # Session, connection state, progress events
        history_modal.py       # Modal overlay for browsing past conversations
  tests/
    __init__.py
    test_config.py
    test_sse_parser.py
    test_client.py
    test_history.py
    test_app.py                # Textual Pilot-based integration tests
```

## Dependencies

```toml
[project]
name = "relais-tui"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "textual>=3.0,<4.0",
    "httpx>=0.28,<1.0",
    "pyyaml>=6.0",
]

[project.scripts]
relais-tui = "relais_tui.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/relais_tui"]

[dependency-groups]
dev = [
    "pytest>=9.0",
    "pytest-asyncio>=1.3",
    "textual-dev>=1.0",
]
```

## Config Schema

Default path: `~/.relais/tui/config.yaml` (created on first run with `0o600` permissions).

```yaml
# RELAIS REST API base URL
api_url: "http://localhost:8080"

# Bearer token for authentication (NEVER logged)
# Can also be set via RELAIS_TUI_API_KEY env var (takes precedence)
api_key: ""

# Directory for saved conversation history
history_path: "~/.relais/tui/history"

# HTTP request timeout in seconds (covers full SSE stream lifetime)
request_timeout: 120

# Session behavior on startup:
#   "new"  — always start a fresh session
#   "last" — resume the most recent session from history
session_behavior: "new"

# Theme customization (CSS color values)
theme:
  background: "#1a1a2e"
  user_text: "#8be9fd"
  assistant_text: "#f8f8f2"
  code_block: "#282a36"
  progress: "#6272a4"
  error: "#ff5555"
  metadata: "#6272a4"
  status_bar: "#16213e"
  accent: "#50fa7b"
```

## REST API Contract (reference)

Endpoint: `POST /v1/messages`
Auth: `Authorization: Bearer <api_key>`
SSE mode: `Accept: text/event-stream`

### SSE Events

| Event | Data | Description |
|-------|------|-------------|
| `token` | `{"t": "chunk text"}` | Streamed token fragment |
| `progress` | `{"event": "tool_call", "detail": "web_search"}` | Progress indicator |
| `done` | `{"content": "full reply", "correlation_id": "...", "session_id": "..."}` | Final response |
| `error` | `{"error": "reason", "correlation_id": "..."}` | Error (timeout, cancelled, internal) |
| `: keepalive` | (SSE comment, no event/data) | Heartbeat |

### JSON fallback

If server returns `Content-Type: application/json` instead of SSE:
```json
{"content": "full reply", "correlation_id": "...", "session_id": "..."}
```

### Session continuity

Send `session_id` from previous response in the next request body to continue the conversation.

## Architecture

```
App (textual.app.App)
  |
  +-- ChatLog widget          <- conversation display (Markdown via Rich)
  +-- InputBox widget          <- user types here
  +-- StatusBar widget         <- session_id, progress, connection state
  +-- HistoryModal (overlay)   <- triggered by /history
  |
  +-- RelaisClient             <- httpx.AsyncClient wrapper
  |     +-- SSEParser          <- stateful line parser
  |
  +-- Config                   <- frozen dataclass from YAML
  +-- HistoryManager           <- save/load/list JSON conversation files
```

### Data flow (message send)

1. User presses Enter in InputBox
2. App reads text, appends "user" bubble to ChatLog
3. App calls `client.stream_message(content, session_id)` in a textual Worker
4. Worker yields parsed SSE events via AsyncGenerator
5. App receives events via custom textual Messages:
   - `token` -> append text to current assistant bubble in ChatLog
   - `progress` -> update StatusBar text
   - `done` -> finalize bubble with markdown render, store session_id, save to history
   - `error` -> display error in ChatLog, re-enable input
6. On Ctrl+C during streaming: cancel Worker -> closes httpx stream

### Commands

| Command | Action |
|---------|--------|
| `/new` | Start fresh conversation (new session_id) |
| `/history` | Open modal with past conversations |
| `/config` | Display current config |
| `/quit` or Ctrl+D | Exit |

### Keyboard

| Key | Action |
|-----|--------|
| Enter | Send message |
| Shift+Enter | Newline in input |
| Ctrl+C | Cancel current streaming request |
| Ctrl+D | Quit |
| Up/Down | Scroll conversation |

## Design Decisions

### SSE parsing (manual, no httpx-sse)

Manual stateful parser (~60 lines). Mirrors the playground JS logic:
- Feed raw bytes from `httpx.Response.aiter_bytes()` into `SSEParser.feed(chunk)`
- Parser maintains a line buffer, splits on `\n`
- Lines starting with `event: ` set current event type
- Lines starting with `data: ` contain JSON payload
- Lines starting with `:` are comments (keepalive)
- Empty lines (`\n\n`) = event boundary -> emit and reset

### Textual async + httpx streaming

Everything stays in one asyncio loop (no threads):
1. `textual.Worker` runs `client.stream_message()` coroutine
2. Worker posts custom `textual.Message` subclasses to App
3. App message handlers update widgets on the main event loop
4. Worker cancellation closes the httpx stream via `response.aclose()`

### Markdown rendering

During streaming: raw text (no flicker from re-rendering).
At `done` event: replace raw text with `rich.markdown.Markdown` rendered version.

### History format

One JSON file per session: `{history_path}/{session_id}.json`

```json
{
  "session_id": "abc-123",
  "started_at": "2026-04-14T14:30:00Z",
  "updated_at": "2026-04-14T14:35:00Z",
  "messages": [
    {"role": "user", "content": "Hello", "timestamp": "2026-04-14T14:30:00Z"},
    {"role": "assistant", "content": "Hi!", "timestamp": "2026-04-14T14:30:05Z",
     "correlation_id": "..."}
  ]
}
```

Why JSON files over SQLite: simpler, debuggable with `cat`/`jq`, no migrations, low volume.

### Theme system

YAML dict -> string-replace in a CSS template -> assigned to `App.CSS` at startup.
Missing theme keys fall back to hardcoded defaults in `theme.py`.

### API key security

- Config file created with `0o600` permissions
- Env var `RELAIS_TUI_API_KEY` takes precedence over config file
- Key is never logged, never included in history files

## Implementation Steps

### Step 1: Scaffold + config.py

- Create `tools/tui/pyproject.toml`, package structure, `__init__.py`
- Implement `config.py`: frozen dataclass, YAML load/save, first-run creation with defaults
- Write `tests/test_config.py`
- **Depends on**: nothing
- **Risk**: Low

### Step 2: SSE parser (TDD)

- Implement `sse_parser.py`: stateful line-buffer parser
- Event types: `TokenEvent`, `ProgressEvent`, `DoneEvent`, `ErrorEvent`, `Keepalive`
- Write `tests/test_sse_parser.py` FIRST (RED)
- Test cases: complete events, partial chunks, multi-byte UTF-8 splits, keepalive, unknown events
- **Depends on**: nothing (pure logic)
- **Risk**: Medium (edge cases with partial lines)

### Step 3: HTTP client

- Implement `client.py`: `RelaisClient` wrapping `httpx.AsyncClient`
- Methods: `healthz()`, `send_message()` (JSON), `stream_message()` (SSE AsyncGenerator)
- JSON fallback: detect `Content-Type: application/json`, emit synthetic `DoneEvent`
- Write `tests/test_client.py` with mocked httpx
- **Depends on**: Step 2 (SSEParser)
- **Risk**: Medium (connection errors, timeouts)

### Step 4: Widgets

- `chat_log.py`: RichLog-based, `append_user()`, `append_assistant_start()`, `append_assistant_chunk()`, `append_assistant_done()` (markdown render)
- `input_box.py`: TextArea, Enter=send, Shift+Enter=newline, disabled during streaming
- `status_bar.py`: Static widget, session_id + progress text
- `history_modal.py`: ModalScreen with selectable conversation list
- **Depends on**: nothing (textual only)
- **Risk**: Low-Medium

### Step 5: History manager

- Implement `history.py`: `save()`, `load(session_id)`, `list_recent(limit=20)`
- JSON files in `{history_path}/{session_id}.json`
- Write `tests/test_history.py`
- **Depends on**: Step 1 (config for history_path)
- **Risk**: Low

### Step 6: App assembly (HIGH risk)

- Implement `app.py`: `RelaisTUIApp(textual.app.App)`
- Wire widgets in `compose()`
- Message send flow via textual Worker + custom Messages
- Command routing: `/new`, `/history`, `/config`, `/quit`
- Keyboard bindings: Ctrl+C (cancel worker), Ctrl+D (quit)
- Implement `__main__.py`: CLI arg parsing (`--config`), `app.run()`
- **Depends on**: Steps 1-5
- **Risk**: **High** (async coordination Worker + widgets)

### Step 7: Theme system

- Implement `theme.py`: CSS template with placeholders, YAML -> CSS conversion
- Fallback to defaults for missing keys
- **Depends on**: Step 1, Step 6
- **Risk**: Low

### Step 8: Integration tests + README

- `test_app.py`: textual Pilot tests (launch, type, send with mocked client, /new, /history)
- `README.md`: install, config reference, keybindings, screenshots placeholder
- **Depends on**: all previous steps
- **Risk**: Low

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| RichLog flicker during rapid token streaming | Medium | Buffer tokens (flush every 50ms/20 chars). `done` event re-renders with markdown |
| httpx stream hangs if server dies | Medium | Read timeout shorter than request_timeout. Show error on timeout |
| Large responses (>100KB) slow RichLog | Low | Textual virtual scroll handles this. Truncate + "save to file" if needed (future) |
| Config file exposes API key | Medium | `0o600` permissions + env var `RELAIS_TUI_API_KEY` fallback |
| Stale session_id (server TTL expired) | Low | Server creates fresh context silently. TUI can detect lack of history continuity |
| Textual version breaks | Low | Pin `>=3.0,<4.0`. Test with `textual-dev` |
