---
name: rest
description: >
  Enables, configures, and manages the RELAIS REST API channel adapter.
  Covers API key generation, portail.yaml registration, aiguilleur.yaml
  tuning (bind, port, CORS, timeout, traces), validation via /healthz and
  /docs, key revocation, and adapter disable. Activates when the user
  mentions the REST API, HTTP access, API keys, Bearer tokens, curl, or
  any programmatic / CI access to RELAIS.
metadata:
  author: RELAIS
  version: "1.0"
---

# rest

## Overview

The REST adapter exposes RELAIS as an HTTP/JSON API (and optional SSE
streaming). It runs as a native aiohttp server inside the `aiguilleur`
process. Auth is Bearer-token based; keys are stored in `portail.yaml`.

Endpoints once enabled:

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /healthz` | none | Liveness probe |
| `GET /openapi.json` | none | OpenAPI 3.0 spec |
| `GET /docs` | none | Swagger UI |
| `POST /v1/messages` | Bearer | Send message, receive reply |

## Prerequisites

- [ ] `RELAIS_API_KEY_SALT` set in `.env` (required for HMAC-SHA256 hashing of keys)
- [ ] Target port (default `8080`) not in use: `lsof -i :8080`

Check the salt:

```bash
grep RELAIS_API_KEY_SALT .env
```

If absent, generate and add one:

```bash
python3 -c "import secrets; print('RELAIS_API_KEY_SALT=' + secrets.token_hex(32))"
# → append the output to .env
```

> **Security**: without the salt, keys are hashed with an empty salt — functional
> but weaker. Always set `RELAIS_API_KEY_SALT` before the first key is created.

## Happy path: enable the REST adapter

### Step 1 — Enable in aiguilleur.yaml

Read the current config, then show the diff and wait for confirmation:

```yaml
# config/aiguilleur.yaml  (relevant section)
channels:
  rest:
    enabled: true           # was: false
    type: native
    bind: "127.0.0.1"       # change to "0.0.0.0" to expose on LAN
    port: 8080
    request_timeout: 30     # seconds before 504 Gateway Timeout
    cors_origins:
      - "*"                 # restrict to specific origins in production
    include_traces: false   # true = include pipeline traces in JSON response
```

Config options reference:

| Field | Default | Notes |
|-------|---------|-------|
| `bind` | `"127.0.0.1"` | `"0.0.0.0"` to expose beyond localhost |
| `port` | `8080` | Must be a free TCP port |
| `request_timeout` | `30` | Increase for slow LLM backends |
| `cors_origins` | `["*"]` | Restrict to `["https://your-app.com"]` in prod |
| `include_traces` | `false` | Set to `true` for pipeline debugging |

### Step 2 — Restart aiguilleur

```bash
supervisorctl restart aiguilleur
```

### Step 3 — Validate

```bash
curl http://127.0.0.1:8080/healthz
# → {"status": "ok", "channel": "rest"}

# Open Swagger UI in a browser:
# http://127.0.0.1:8080/docs
```

## Generate and register an API key

> **Show the raw key to the user exactly once.** After adding it to
> `portail.yaml`, the key cannot be recovered — only revoked.

### Step 1 — Generate a secure key

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Example output: xK3mP9wQ...  (44 chars)
```

Display the key to the user and instruct them to copy it now.

### Step 2 — Add to portail.yaml

Two scenarios:

**A — New user dedicated to REST access**

```yaml
users:
  usr_api_client:
    display_name: "API Client"
    role: user
    blocked: false
    identifiers:
      rest:
        api_keys:
          - "xK3mP9wQ..."   # raw key — hashed by UserRegistry at load time
    notes: "REST API access — CI pipeline"
```

**B — Add REST access to an existing user**

Read the user's current entry, then add the key under `identifiers.rest.api_keys`.
If the `rest:` context or `api_keys:` list does not exist yet, create them.

```yaml
identifiers:
  discord:
    dm: "123456789"
  rest:
    api_keys:
      - "xK3mP9wQ..."
```

Show the diff and wait for confirmation before writing.

### Step 3 — Restart aiguilleur (to reload UserRegistry)

```bash
supervisorctl restart aiguilleur
```

### Step 4 — Validate the key

```bash
curl -s -X POST http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer xK3mP9wQ..." \
  -H "Content-Type: application/json" \
  -d '{"content": "Bonjour"}' | python3 -m json.tool
```

Expected: `{"content": "...", "correlation_id": "...", "session_id": "..."}`.

A `401` means the key was not found — verify the key was saved correctly and
that aiguilleur was restarted after the portail.yaml change.

## SSE streaming mode

Add `Accept: text/event-stream` to receive token-by-token chunks:

```bash
curl -N -X POST http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer xK3mP9wQ..." \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"content": "Explique-moi Redis Streams"}'
```

Events emitted:
- `event: token` — `data: {"t": "<chunk>"}`
- `event: done` — `data: {"content": "...", "correlation_id": "...", "session_id": "..."}`
- `: keepalive` — heartbeat comment (every ~500 ms of silence)

## Revoke an API key

1. Read the user's entry in portail.yaml.
2. Remove the key from the `api_keys` list (or set `api_keys: []`).
3. Show the diff and wait for confirmation.
4. Write the updated file.
5. Restart aiguilleur: `supervisorctl restart aiguilleur`.

> If the user **has no other keys**, the REST channel simply stops working for
> that account. If the goal is to fully block the account, set `blocked: true`
> instead (takes effect after the next aiguilleur restart).

## Disable the REST adapter

```yaml
# config/aiguilleur.yaml
channels:
  rest:
    enabled: false
```

Then:

```bash
supervisorctl restart aiguilleur
```

## Diagnose

### 1. Liveness check

```bash
curl http://127.0.0.1:8080/healthz
# → {"status": "ok", "channel": "rest"}
# Connection refused → adapter not running; check aiguilleur logs
```

### 2. Logs

```bash
supervisorctl tail aiguilleur -f
# Look for: "REST adapter listening on http://127.0.0.1:8080"
# Missing → adapter failed to start; check for port conflict or import error
```

### 3. Auth failures (401)

Check in order:

- Salt set? `grep RELAIS_API_KEY_SALT .env`
- Key in portail.yaml? `grep -A5 "api_keys" ~/.relais/config/portail.yaml`
- Aiguilleur restarted after portail.yaml change?
- Bearer token copied exactly (no trailing space)?

### 4. Timeouts (504)

- Increase `request_timeout` in aiguilleur.yaml
- Check atelier logs for slow LLM backend
- Check `lsof -i :8080` for port conflicts

### 5. CORS errors (browser clients)

- Set `cors_origins` to the exact origin of the browser app (e.g. `["https://my-app.com"]`)
- Restart aiguilleur after the change

## Security rules

- **Never log the raw Bearer token** — auth.py already enforces this (logs token length only).
- **Always set `RELAIS_API_KEY_SALT`** before creating the first key.
- **Show the raw key only once** — after it is saved to portail.yaml, display it no further.
- **Do not expose port 8080 on `0.0.0.0`** in production without a reverse proxy (nginx/Caddy) providing TLS and rate limiting.
- **Restrict `cors_origins`** in production — `["*"]` is only safe on a localhost-only bind.
- **Confirm before revoking** — revocation is immediate on the next restart and cannot be undone.

## References

- `aiguilleur/channels/rest/` — adapter, server, auth, correlator, SSE helpers
- `portail/user_registry.py` — `_hash_api_key()`, `resolve_user()`, `api_keys` indexing
- `config/aiguilleur.yaml.default` — REST channel config template
- `config/portail.yaml.default` — user/identifier schema with REST `api_keys` example
- `GET /openapi.json` — full OpenAPI 3.0 spec at runtime
- `GET /docs` — Swagger UI for interactive testing
