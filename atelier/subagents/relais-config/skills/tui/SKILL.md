---
name: tui
description: >
  Reads, inspects, and modifies the RELAIS TUI client configuration
  (config/tui/config.yaml): API endpoint, Bearer key, request timeout,
  history path, and theme colors. Activates when the user mentions the
  TUI, the `relais-tui` / `relais` CLI, terminal client, TUI theme or
  colors, TUI API key, or any client-side setting of the terminal
  interface.
metadata:
  author: RELAIS
  version: "2.0"
---

# tui

## Overview

The RELAIS TUI (`tools/tui/`) is a Textual-based terminal client that
connects to the REST channel adapter over HTTP/SSE. Its configuration
lives at `<RELAIS_HOME>/config/tui/config.yaml` and is read once at
startup — no brick is involved, so **no `supervisorctl restart` is
needed**: changes take effect on the next TUI launch.

For the server-side REST adapter that the TUI consumes, see the `rest`
skill.

## Config file location

| Condition | Path |
|-----------|------|
| `RELAIS_HOME` set | `$RELAIS_HOME/config/tui/config.yaml` |
| `RELAIS_HOME` unset (default) | `~/.relais/config/tui/config.yaml` |

The file is **auto-created with `0o600` permissions** on the first TUI
launch. If it does not exist yet, ask the user to run `relais-tui` once,
or write the defaults manually (see schema below).

Template source: `config/tui/config.yaml.default` in the project root.

## Schema reference

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `api_url` | string | `"http://localhost:8080"` | Base URL of the REST adapter. |
| `api_key` | string | `""` | Bearer token. Overridden at runtime by `RELAIS_TUI_API_KEY` env var if non-empty. |
| `history_path` | string | `"~/.relais/storage/tui/history"` | Command history file. `~` is expanded at load time. |
| `request_timeout` | int (seconds) | `120` | HTTP timeout for non-SSE calls. |
| `theme.background` | CSS color | `"#1a1a2e"` | |
| `theme.user_text` | CSS color | `"#8be9fd"` | |
| `theme.assistant_text` | CSS color | `"#f8f8f2"` | |
| `theme.code_block` | CSS color | `"#282a36"` | |
| `theme.progress` | CSS color | `"#6272a4"` | |
| `theme.error` | CSS color | `"#ff5555"` | |
| `theme.metadata` | CSS color | `"#6272a4"` | |
| `theme.status_bar` | CSS color | `"#16213e"` | |
| `theme.accent` | CSS color | `"#50fa7b"` | |

Source of truth: `tools/tui/src/relais_tui/config.py` (`Config` and
`ThemeConfig` dataclasses). Any CSS-compatible color string is valid
(hex `#rrggbb`, named `"red"`, `ansi_blue`, etc.) — Textual validates
at mount time.

## Read the current config

```bash
cat ~/.relais/config/tui/config.yaml
# or: cat $RELAIS_HOME/config/tui/config.yaml
```

> **Security**: never echo `api_key` in clear text. Report it as
> "set (N chars)" or "not set" depending on whether the field is
> non-empty.

## Happy path: point the TUI at the REST adapter

1. Read `~/.relais/config/tui/config.yaml`.
2. Ask the user for the REST adapter URL (default `http://localhost:8080`).
   Cross-reference the `rest` skill if the adapter is not yet enabled.
3. Ask for the API key — prefer env var `RELAIS_TUI_API_KEY` over
   storing in YAML (avoids the key appearing in the file).
4. Show the before/after diff and wait for explicit confirmation.
5. Write the updated file.
6. Inform the user: changes apply on the next TUI launch, no restart
   needed.

Example diff:

```yaml
# before
api_url: http://localhost:8080
api_key: ""

# after
api_url: http://192.168.1.10:8080
api_key: "xK3mP9wQ..."   # or leave "" and set RELAIS_TUI_API_KEY
```

## Change the theme

1. Read the current config.
2. Ask which colors to change (show the full palette with current values).
3. Show the before/after diff and wait for confirmation.
4. Write the file.
5. Inform the user: restart the TUI to apply.

Example diff (dark → OLED black accented in orange):

```yaml
# before
theme:
  background: '#1a1a2e'
  accent: '#50fa7b'

# after
theme:
  background: '#000000'
  accent: '#ff8800'
```

## Tune runtime behavior

### `request_timeout`

Increase when the LLM backend is slow (e.g. a local model):

```yaml
request_timeout: 300   # 5 minutes
```

### `history_path`

Change to a custom location (ensure the parent directory exists and is
writable):

```yaml
history_path: "~/Documents/relais-history"
```

## Revoke / rotate the API key

**Option A — Env var (recommended):**

Unset `RELAIS_TUI_API_KEY` in the shell (or `.env`) and optionally
clear the `api_key` field in the YAML. Regenerate on the server side
via the `rest` skill.

**Option B — YAML only:**

1. Read the config file.
2. Replace `api_key` with the new key (or set to `""`).
3. Show the diff and confirm.
4. Write the file (`0o600` permissions enforced on every save).

Cross-reference the `rest` skill to rotate the corresponding server-side
entry in `portail.yaml`.

## Diagnose

| Symptom | Cause | Fix |
|---------|-------|-----|
| Config file missing | TUI never launched | Run `relais-tui` once, or create the file manually from defaults |
| Permission denied on config | Wrong file mode | `chmod 600 ~/.relais/config/tui/config.yaml` |
| `401 Unauthorized` from REST | Wrong or missing key | Check `RELAIS_TUI_API_KEY` env var; verify key in REST `portail.yaml` |
| `Connection refused` | REST adapter not running | See `rest` skill; `curl http://127.0.0.1:8080/healthz` |
| Request timeout | Backend too slow | Raise `request_timeout`; also check `request_timeout` in `aiguilleur.yaml` |
| Invalid theme color error | Bad color string | Run `relais-tui` from a terminal to see Textual's error; fix the color value |
| History not saved | `history_path` not writable | Check path expansion and directory permissions |
| Env var ignored | `RELAIS_TUI_API_KEY` empty string | Only non-empty values override; unset the var to fall back to YAML |

## Security rules

- **Never echo `api_key` in clear text** — report length only.
- **Prefer `RELAIS_TUI_API_KEY`** over YAML storage on shared machines.
- **`0o600` permissions** are enforced by `save_config()` on every write.
- **Always confirm before overwriting** — the file may contain a working key.
- **Note for running TUI**: the TUI reads config at startup; a running
  instance will not pick up changes made to the file mid-session.

## References

- `tools/tui/src/relais_tui/config.py` — schema source of truth (`Config`, `ThemeConfig`)
- `config/tui/config.yaml.default` — shipped template with default values
- `common/init.py` — `DEFAULT_FILES` entry that copies the template on first run
- `rest` skill — server-side REST adapter configuration (API keys, port, CORS)
- `RELAIS_TUI_API_KEY` — env var that overrides `api_key` at runtime
- `RELAIS_HOME` — env var that controls the config root path
