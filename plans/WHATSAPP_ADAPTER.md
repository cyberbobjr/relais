> **NOTE (2026-04-10):** This plan has been fully implemented. The WhatsApp channel
> code has been consolidated into the `channels/whatsapp/` package (adapter, core
> logic, tools, CLI). The old paths referenced below (`aiguilleur/channels/whatsapp/`,
> `scripts/install_whatsapp.sh`, `scripts/pair_whatsapp.py`, `scripts/unpair_whatsapp.py`)
> no longer exist. The `relais-config` subagent now uses three LangChain `BaseTool`
> implementations (`whatsapp_install`, `whatsapp_configure`, `whatsapp_uninstall`)
> loaded via `tool_tokens: [module:channels.whatsapp.tools]`. The CLI entry point is
> `python -m channels.whatsapp`. This document is preserved as historical context.

# Plan — WhatsApp Aiguilleur Adapter (Baileys Gateway)

**Objective:** Implement a `WhatsAppAiguilleur` NativeAiguilleur adapter for the RELAIS pipeline that bridges a Baileys-based HTTP gateway ([fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api)) with the Redis Streams bus. The admin's personal WhatsApp number is used as the RELAIS channel (shared bot number model). Session pairing is triggered via `/settings whatsapp` (admin-only) in Commandant, which displays a QR code as ASCII art in the admin's current channel.

**Status:** READY (amended after review — 43 decisions integrated + source verification of fazer-ai/baileys-api completed 2026-04-09)
**Branch:** `feat/whatsapp-adapter`
**Base:** `main`

---

## Context Brief (cold-start safe)

RELAIS is a micro-brick async AI pipeline. The **Aiguilleur** brick manages channel adapters. Each adapter is either:
- `NativeAiguilleur` — Python class with `async def run()`, spawned in a dedicated thread with `asyncio.run()`
- `ExternalAiguilleur` — subprocess

The **Discord adapter** is the canonical reference: `aiguilleur/channels/discord/adapter.py`.

### Architectural Decision: Shared Bot Number (Model A)

WhatsApp operates as a **shared bot number**, identical to how Discord and Telegram work in RELAIS:
- The admin's personal WhatsApp number is linked to RELAIS via Baileys (Linked Device)
- RELAIS sees all incoming messages on that number
- **"Note to self" conversation** = admin talking to RELAIS (like a DM to the bot)
- **Other conversations** = external contacts writing to the admin — RELAIS identifies them via their JID, resolves to a RELAIS user via `portail.yaml`, and adapts behavior (respond, ignore, role-based prompt)
- **`fromMe: true` in non-self conversations** = admin replying manually → RELAIS ignores (no interference)
- **One connection, one adapter, one webhook** — no multi-session complexity

### Owner Identity Model [AMENDED: review point 1]

The admin's own phone number (the bot number) is mapped in `portail.yaml` using a dedicated `self` context, distinct from `dm`:

```yaml
usr_admin:
  display_name: "Admin"
  role: admin
  identifiers:
    whatsapp:
      self: "+33612345678"   # ← the bot's own number (self-chat = admin talking to RELAIS)
    discord:
      dm: "123456789"
```

- `identifiers.whatsapp.self` = "I am the owner of this WhatsApp number"
- `identifiers.whatsapp.dm` = "someone contacts me via this number" (used for external contacts)

`UserRegistry.resolve_user("whatsapp:+33612345678", "whatsapp")` resolves via `(channel="whatsapp", context="self", raw_id="+33612345678")` → `usr_admin`.

This avoids conflating "I am this number" with "someone contacts me on this number", consistent with Discord's multi-context model (`dm`/`server`).

### Access Policy [AMENDED: review point 9]

**Recommended production configuration for WhatsApp:**
- `unknown_user_policy: deny` in `portail.yaml` — RELAIS does not respond to unknown contacts
- DM only (group messages filtered by the adapter)
- Text only (non-text messages skipped in MVP)
- Pairing reserved to admin role (enforced by Sentinelle via `actions`)
- Contacts must be explicitly added to `portail.yaml` under `identifiers.whatsapp.dm`

### Baileys Gateway (fazer-ai/baileys-api)

[Baileys](https://github.com/WhiskeySockets/Baileys) (8 900+ stars, Node.js) connects directly to WhatsApp servers via WebSocket — no browser needed.

**fazer-ai/baileys-api** wraps Baileys in a Bun + Elysia.js HTTP server:
- **Session creation**: `POST /connections/:phoneNumber` (creates Baileys socket, registers webhook)
- **QR code delivery**: pushed via webhook as `connection.update` event with `qrDataUrl` (base64 PNG)
- **Incoming messages**: pushed via webhook as `messages.upsert` event (array of messages)
- **Send messages**: `POST /connections/:phoneNumber/send-message` with `{"jid": "<jid>", "messageContent": {"text": "<text>"}}` (messageContent is a union type — use `{text: ...}` for text messages) [AMENDED: source verification]
- **Health check**: `GET /status` — **no auth required** (returns sanitized config); `GET /status/auth` requires `x-api-key` [AMENDED: source verification]
- **No per-connection status endpoint** — connection state tracked exclusively via webhook `connection.update` events [AMENDED: source verification]
- **Session storage**: Redis (must share RELAIS Redis via TCP — see Redis section below)
- **Auth**: API key via `x-api-key` header on all endpoints **except** `GET /status` (managed via `bun scripts/manage-api-keys.ts`; keys stored as plaintext in Redis with in-process LRU cache, TTL 5min) [AMENDED: review point 6, source verification]
- **Webhook security**: `webhookVerifyToken` sent in body (NOT HMAC signature — sufficient for localhost-only; document limit if exposed to network)
- **No dedicated QR endpoint** — QR is delivered exclusively via webhook push
- **No pairing code flow** — QR scan only

**Key difference from WA-RS**: no Rust nightly, no SQLite, no cargo build. Just `bun install && bun start` or Docker.

> **IMPORTANT**: Pin to a verified commit. The exact SHA is stored as a constant in `scripts/install_whatsapp.sh` (see Step 0). [AMENDED: review point 15]

### Commandant (slash commands)

`commandant/commands.py` is the single source of truth for commands:
- `COMMAND_REGISTRY: dict[str, CommandSpec]` maps names to `CommandSpec(name, description, handler)`
- `KNOWN_COMMANDS = frozenset(COMMAND_REGISTRY)` auto-syncs with Sentinelle's gate
- Handlers receive `(envelope: Envelope, redis_conn: Any)` and publish replies themselves
- Adding a command = adding a handler function + one `COMMAND_REGISTRY` entry. No changes to `main.py` or Sentinelle needed.
- **Command authorization** is controlled by `user_record.actions` in the user's role in `portail.yaml` (NOT `sentinelle.yaml`). Roles with `actions: ["*"]` (e.g. admin) have access to all commands. The handler relies on Sentinelle for authorization — no additional role check in the handler. [AMENDED: review point 10]

Existing commands: `/clear`, `/help`. No `/settings` command exists yet.

### Portail User Registry (contact identity)

`portail.yaml` supports WhatsApp in its identifier structure:
```yaml
usr_admin:
  identifiers:
    whatsapp:
      self: "+33612345678"   # owner's number (self-chat → admin talking to RELAIS)

usr_guest:
  display_name: "Pierre"
  role: guest
  identifiers:
    whatsapp:
      dm: "+33699999999"     # contact's number (external DM)
```

- `UserRegistry.resolve_user("whatsapp:+33699999999", "whatsapp")` → lookup `(channel="whatsapp", context="dm", raw_id="+33699999999")` → maps **contacts** to RELAIS users
- `UserRegistry.resolve_user("whatsapp:+33612345678", "whatsapp")` → lookup `(channel="whatsapp", context="self", raw_id="+33612345678")` → maps **owner** to `usr_admin` [AMENDED: review point 1]

Contact-to-user association is done manually (edit `portail.yaml`) or via a future `/settings whatsapp link` command (out of scope for this plan).

### Session Credential Storage (two levels)

| Data | Storage | Managed by |
|---|---|---|
| **Signal protocol keys** (encryption, WhatsApp Web session) | **Redis** (baileys-api keyspace, `baileys` ACL user) | baileys-api — transparent to RELAIS |
| **Owner identity** (which RELAIS user owns the bot number) | **portail.yaml** (`identifiers.whatsapp.self`) | Portail — UserRegistry (manual config) [AMENDED: review point 1] |
| **Contact identity** (external contacts) | **portail.yaml** (`identifiers.whatsapp.dm`) | Portail — UserRegistry (manual config) |

### JID ↔ E.164 Normalization

Baileys uses JIDs (`33699999999@s.whatsapp.net`), portail.yaml stores E.164 (`+33699999999`). JIDs may contain device suffixes (`33699999999:2@s.whatsapp.net`). The adapter normalizes at the boundary: [AMENDED: review point 26]
- **Inbound**: `normalize_whatsapp_id("33699999999:2@s.whatsapp.net")` → strip `@` domain → strip `:` device suffix → prepend `+` → `"+33699999999"` → `sender_id = "whatsapp:+33699999999"`
- **Outbound**: `e164_to_jid("+33699999999")` → `"33699999999@s.whatsapp.net"` → used in `reply_to` and send API

Both functions live in `aiguilleur/channels/whatsapp/adapter.py`.

### Redis Architecture

RELAIS Redis runs on **Unix socket only** (`port 0`). baileys-api (Node.js) requires TCP.

**Solution**: enable TCP on the RELAIS Redis in addition to the Unix socket:
- Add `port 6379` to `config/redis.conf`
- Create a dedicated ACL user `baileys` with access restricted to its own keyspace (prefix: `@baileys-api:` — note the `@` literal) [AMENDED: review point 5, source verification]
- baileys-api connects via `REDIS_URL=redis://baileys:pass_baileys@localhost:6379`
- RELAIS bricks continue to use the Unix socket — no change to existing code

### Outgoing Message Formatting [AMENDED: review point 33]

Each adapter cleans outgoing messages before sending using `common/markdown_converter.py`. This module already provides `convert_md_to_telegram()`, `convert_md_to_slack_mrkdwn()`, and `strip_markdown()`.

For WhatsApp, add `convert_md_to_whatsapp()`:
- `**bold**` → `*bold*` (WhatsApp native)
- `*italic*` / `_italic_` → `_italic_` (WhatsApp native)
- Strip code fences, headings, horizontal rules
- Keep links as plain URLs
- Keep line breaks (WhatsApp renders them natively)

The adapter applies this converter on all outgoing content before sending via baileys-api. QR codes sent to other channels (Discord, Telegram) during pairing use their native formatting (code fences are fine there).

### Webhook Networking [AMENDED: review point 7]

`WHATSAPP_WEBHOOK_HOST` is used as both the bind address for the aiohttp webhook server and the hostname in the `webhookUrl` sent to the gateway. **This only works when gateway and adapter run in the same network namespace** (same host, same Docker network).

For Docker deployments where baileys-api runs in a container, use `host.docker.internal` (macOS/Windows) or `--network=host` (Linux) so the gateway can reach the adapter's webhook. This is documented in `docs/WHATSAPP_SETUP.md`.

### Pairing Flow (`/settings whatsapp` — admin-only)

```
Admin types: /settings whatsapp (on Discord, Telegram, etc.)
    → Sentinelle checks user_record.actions (must include "*" or "settings")
    → Routes to relais:commands
    → Commandant handler:
        1. Verifies adapter health via GET http://127.0.0.1:{port}/status (no auth) [AMENDED: review point 4, source verification]
        2. Verifies reply_to is present in AiguilleurCtx — rejects if absent [AMENDED: review point 30]
        3. Calls POST /connections/:phoneNumber on baileys-api (with x-api-key header)
        4. On success: stores pairing context in Redis key KEY_WHATSAPP_PAIRING
           {channel, sender_id, session_id, correlation_id, reply_to,
            state: "pending_qr", timestamp} TTL=300s [AMENDED: review point 18]
        5. Replies: "WhatsApp pairing started. QR code arriving..."
           (with action=ACTION_MESSAGE_OUTGOING)
        On HTTP error: does NOT store pairing key — replies with error message [AMENDED: review point 18]
    → baileys-api emits connection.update webhook with qrDataUrl
    → WhatsApp adapter webhook handler:
        1. Detects connection.update event with qr field
        2. Verifies webhookVerifyToken (constant-time comparison)
        3. Reads KEY_WHATSAPP_PAIRING from Redis → finds originating channel
        4. Converts base64 PNG QR to ASCII art (qrcode Python library)
        5. Builds Envelope with ASCII QR as content text
           (action=ACTION_MESSAGE_OUTGOING, reply_to from pairing context)
        6. Publishes to stream_outgoing(originator_channel)
    → Admin scans QR with WhatsApp mobile
    → baileys-api emits connection.update with connection="open"
    → Webhook handler:
        1. Sends confirmation to originator channel
        2. Deletes KEY_WHATSAPP_PAIRING
    → Adapter enters normal operation (inbound/outbound message loop)

Error events during pairing:
    → connection.update with connection="close" → error message to admin, cleanup pairing key
    → connection.update with lastDisconnect containing "wrong_phone_number" → specific error message [AMENDED: review point 28]
    → connection.update with connection="reconnecting" → log INFO, no user message [AMENDED: review point 28]
    → Pairing key expires (TTL 300s) → next QR silently ignored, admin re-runs /settings whatsapp
```

### Message Flow (normal operation)

```
Inbound:
  Contact sends WhatsApp message to admin's number
    → baileys-api webhook: messages.upsert (type="notify", array of messages)
    → Adapter webhook handler:
        1. Verify webhookVerifyToken
        2. Filter: type=="notify" only (ignore "append" = history sync)
        3. For each message in array:
           a. Check deduplication (OrderedDict LRU, message_id already seen? → skip) [AMENDED: review point 17]
           b. Determine if "self" conversation (JID matches bot's own number)
           c. If non-self + fromMe:true → skip (admin replying manually)
           d. If self + fromMe:true → check sent_message_ids anti-loop set → skip if RELAIS sent it
           e. Extract text via _extract_text_content() → skip if None (non-text, MVP)
           f. Normalize JID → E.164 (strip device suffix) → sender_id [AMENDED: review point 26]
           g. Filter: skip @g.us JIDs (group messages, adapter is sole source of truth) [AMENDED: review point 29]
           h. Build Envelope, xadd to STREAM_INCOMING
        4. Always return HTTP 200, log individual xadd failures [AMENDED: review point 22]

Outbound:
  Pipeline produces reply → relais:messages:outgoing:whatsapp
    → Adapter outgoing consumer:
        1. XREADGROUP with consumer "whatsapp_{pid}"
        2. Deserialize Envelope
        3. If ACTION_MESSAGE_PROGRESS → skip, XACK
        4. Apply convert_md_to_whatsapp() on content [AMENDED: review point 33]
        5. Split long content (_split_whatsapp_message, max 4096)
        6. POST /connections/{phone}/send-message for each part (with x-api-key header)
        7. Track message_id in sent_message_ids OrderedDict (anti-loop) [AMENDED: review point 17]
        8. On send error: route to relais:messages:outgoing:failed (DLQ), then XACK [AMENDED: review point 21]
        9. On success: XACK

Note on deduplication: seen_message_ids and sent_message_ids are in-memory OrderedDict LRU
(max 1000 entries). They do NOT survive adapter restarts. Webhook retries after a restart may
produce duplicates on STREAM_INCOMING. This is accepted as MVP debt — Redis-backed dedup
(SETNX with TTL) is a future improvement. [AMENDED: review point 16]
```

**Key files to read before executing any step:**
- `aiguilleur/channels/discord/adapter.py` — canonical adapter reference
- `aiguilleur/core/native.py` — NativeAiguilleur base class (`stop_event` is `threading.Event`, must be polled)
- `commandant/commands.py` — command registry + handler pattern
- `common/envelope.py` — Envelope dataclass, `from_parent()` (does NOT set `action` — caller must set it)
- `common/envelope_actions.py` — `ACTION_MESSAGE_INCOMING`, `ACTION_MESSAGE_OUTGOING`, `ACTION_MESSAGE_PROGRESS`
- `common/contexts.py` — `CTX_AIGUILLEUR`, `CTX_PORTAIL`, `AiguilleurCtx`, `ensure_ctx()`
- `common/streams.py` — `STREAM_INCOMING`, `stream_outgoing()`
- `common/config_loader.py` — `resolve_config_path()`, `get_relais_home()`
- `common/markdown_converter.py` — `convert_md_to_whatsapp()` (to be added)
- `portail/user_registry.py` — `UserRegistry`, `resolve_user()`, identifier structure

**Invariants (must hold after every step):**
1. `pytest tests/ -x --timeout=30 -m "not integration"` passes (no regressions)
2. No mutation of `envelope.context` namespaces other than `CTX_AIGUILLEUR`
3. Webhook server binds only to localhost unless `WHATSAPP_WEBHOOK_HOST` overrides
4. Redis `XACK` only after successful publish to `STREAM_INCOMING`
5. baileys-api starts before Aiguilleur (priority 5 < priority 10)
6. Pairing key TTL (300s) prevents stale pairing contexts from leaking
7. All envelopes published to `stream_outgoing()` have `action = ACTION_MESSAGE_OUTGOING`
8. All envelopes published to `stream_outgoing()` have `context[CTX_AIGUILLEUR]["reply_to"]` set — no fallback on `sender_id` [AMENDED: review point 30]
9. `sender_id` always uses normalized E.164 format: `"whatsapp:+33699999999"` (device suffix stripped) [AMENDED: review point 26]
10. Redis key names use constants from `common/streams.py`
11. `x-api-key` header sent on all mutation requests to baileys-api (not required for `GET /status`) [AMENDED: review point 6, source verification]
12. Outgoing delivery failures routed to `relais:messages:outgoing:failed` before XACK [AMENDED: review point 21]

---

## Dependency Graph

```
Step 0 (baileys-api gateway setup)
    └── Step 1a (Envelope validation — action required on ALL call sites)  [AMENDED: review point 2]
            └── Step 1b (adapter core: webhook + outgoing)  [depends on 0 + 1a + 3 (aiguilleur.yaml)]
                    └── Step 2 (/settings whatsapp command)  [depends on 1b]
Step 3 (config + env vars + Redis ACL)                       [parallel with 1a]
                            └── Step 4 (tests)               [depends on 1b+2+3]
                                    └── Step 5 (docs)        [depends on all]
```

Steps 1a and 3 can run in **parallel** after Step 0.
Step 1b depends on Step 1a **and** Step 3 (for `aiguilleur.yaml` in `DEFAULT_FILES`). [AMENDED: review point 3]
Step 2 depends on Step 1b.

---

## Step 0 — Baileys Gateway Setup

**Branch:** `feat/whatsapp-adapter`
**Model tier:** Default
**PR:** Yes (same PR as all other steps)

### Context Brief

fazer-ai/baileys-api is a Node.js (Bun) HTTP wrapper around Baileys. It connects to WhatsApp via WebSocket (no browser), stores sessions in Redis, and pushes events via webhook.

The gateway is **not a RELAIS brick** — it is an external dependency like Redis. It runs alongside RELAIS and the adapter communicates with it via HTTP.

### Task List

#### 0a. Operational Prerequisites (outside PR) [AMENDED: review point 14]

These steps are **not versioned** — they set up the local environment. They are automated by `scripts/install_whatsapp.sh` (see 0b).

- [ ] Ensure Bun is installed: `curl -fsSL https://bun.sh/install | bash`
- [ ] Clone and pin baileys-api to `$RELAIS_HOME/vendor/baileys-api` [AMENDED: review point 13]
- [ ] Create an API key (skip in `NODE_ENV=development`):
  ```bash
  cd $RELAIS_HOME/vendor/baileys-api
  bun scripts/manage-api-keys.ts create user relais-adapter
  # → outputs the API key — store as WHATSAPP_API_KEY in .env
  ```

#### 0b. Create `scripts/install_whatsapp.sh` (versioned, in PR) [AMENDED: review points 14, 15]

- [ ] Create `scripts/install_whatsapp.sh` with:
  - **Pinned SHA as a constant** at the top of the script — this is the **first task** of Step 0. Resolve by reading the fazer-ai/baileys-api repo and choosing the latest stable commit.
  - Check Bun is in PATH, fail with clear message if not
  - Clone baileys-api to `$RELAIS_HOME/vendor/baileys-api` (using `get_relais_home()` equivalent in shell: `${RELAIS_HOME:-./.relais}`)
  - `git checkout $PINNED_SHA`
  - `bun install`
  - Print instructions for API key creation
  - Idempotent: skip clone if directory already exists, verify SHA matches

#### 0c. Create `scripts/run_baileys.py` (wrapper for supervisord) [AMENDED: review point 11]

- [ ] Create `scripts/run_baileys.py`:
  ```python
  """Wrapper script for supervisord — checks prerequisites before launching baileys-api."""
  import os
  import shutil
  import sys

  from common.config_loader import get_relais_home

  def main() -> None:
      vendor_dir = os.path.join(get_relais_home(), "vendor", "baileys-api")
      if not os.path.isdir(vendor_dir):
          print(f"baileys-api not installed at {vendor_dir}. Run: scripts/install_whatsapp.sh", file=sys.stderr)
          sys.exit(0)  # clean exit — no crash loop

      bun = shutil.which("bun")
      if not bun:
          print("bun not found in PATH. Install: curl -fsSL https://bun.sh/install | bash", file=sys.stderr)
          sys.exit(0)  # clean exit — no crash loop

      os.chdir(vendor_dir)
      os.execvp(bun, [bun, "start"])

  if __name__ == "__main__":
      main()
  ```

#### 0d. Add baileys-api to supervisord [AMENDED: review point 11]

- [ ] Add a `[group:optional]` section to `supervisord.conf`:
  ```ini
  [group:optional]
  programs=baileys-api
  ```

- [ ] Add a `[program:baileys-api]` section at **priority 5**:
  ```ini
  ; priority 5 — WhatsApp Baileys gateway (external Node.js service)
  [program:baileys-api]
  command=python scripts/run_baileys.py
  directory=%(here)s
  priority=5
  autostart=false
  autorestart=true
  stopasgroup=true
  killasgroup=true
  stdout_logfile=./.relais/logs/baileys-api.log
  redirect_stderr=true
  environment=REDIS_URL="redis://baileys:%(ENV_REDIS_PASS_BAILEYS)s@localhost:6379",PORT="3025",NODE_ENV="production"
  ```
  Notes:
  - `autostart=false` — only runs when the user explicitly enables WhatsApp
  - `scripts/run_baileys.py` checks prerequisites and exits cleanly if missing — no crash loop
  - No `IGNORE_GROUP_MESSAGES` env var — group filtering is handled by the adapter [AMENDED: review point 29]

- [ ] Modify `supervisor.sh` to start specific groups instead of `all`: [AMENDED: review point 11]
  ```bash
  # Replace: run_supervisorctl start all
  # With:    run_supervisorctl start infra:* core:* relays:*
  ```
  Users who want baileys run: `supervisorctl start optional:baileys-api`

#### 0e. Verify upstream API before coding [AMENDED: review points 5, 25, 27] — DONE

> **Source verification completed.** All items below verified against `fazer-ai/baileys-api` source code. Discrepancies integrated into plan with `[AMENDED: source verification]` markers.

- [x] `src/controllers/connections/index.ts` — endpoint paths and body format verified
- [x] `src/baileys/connection.ts` — webhook/QR/reconnect logic verified
- [x] `src/baileys/types.ts` — webhook event types verified (`BaileysConnectionWebhookPayload` wraps `BaileysEventMap`)
- [x] `src/redis/` — Redis key prefix: **`@baileys-api:`** (with literal `@`). Patterns:
  - `@baileys-api:connections:{phone}:authState` — session auth state (Redis hash)
  - `@baileys-api:api-keys:{key}` — API keys (plaintext, value = `{"role":"user"|"admin"}`)
  - `@baileys-api:idempotency:send-message:{phone}:{chatwootMessageId}` — send dedup
- [x] Health: `GET /status` — **no auth required** (returns sanitized config); `GET /status/auth` requires `x-api-key`
- [x] Send: `POST /connections/{phone}/send-message` — body: `{"jid": "<jid>", "messageContent": {"text": "<text>"}}` (union type, not bare string)
- [x] Response shape: `{data: {key: {id, remoteJid, fromMe}, messageTimestamp: "<string>"}}` — confirmed
- [x] **No per-connection status endpoint** — only `POST /:phone` (create/reconnect) and `DELETE /:phone` (logout). Connection state tracked via webhook `connection.update` events only.
- [x] `phoneNumber` route param requires `+` prefix (regex: `^\+\d{5,15}$`)
- [x] `POST /connections/:phone` body: `webhookUrl` (required), `webhookVerifyToken` (required, min 6 chars), `includeMedia` (default **true** — must pass `false` explicitly for MVP), `syncFullHistory` (default false), `groupsEnabled` (default true), `clientName` (optional, default "Chrome")
- [x] Webhook body: `{event, data, webhookVerifyToken, extra?, awaitResponse?}` — token at top level of every payload
- [x] Error responses from send-message are **plain text** (not JSON): 409 = `"Message is already being processed"`, 500 = `"Message not sent"`
- [x] Webhook events with dedicated handlers: `connection.update`, `messages.upsert`, `messages.update`, `message-receipt.update`, `messaging-history.set`, `groups.update`, `group-participants.update`, `groups.activity`

### Verification Commands
```bash
# Health check (baileys-api must be running — no auth required on /status)
curl -sf http://localhost:3025/status && echo "baileys-api OK"
# Authenticated health check (verifies API key is valid)
curl -sf -H "x-api-key: $WHATSAPP_API_KEY" http://localhost:3025/status/auth && echo "baileys-api auth OK"

# Verify supervisord entry
grep -q "baileys-api" supervisord.conf && echo "supervisord OK"

# Verify install script exists
test -x scripts/install_whatsapp.sh && echo "install script OK"
```

### Exit Criteria
- [ ] `scripts/install_whatsapp.sh` exists with pinned SHA constant
- [ ] `scripts/run_baileys.py` exists — checks prerequisites, exits cleanly if missing
- [ ] baileys-api installed at `$RELAIS_HOME/vendor/baileys-api/` pinned to a specific commit
- [ ] `curl http://localhost:3025/status` returns 200 (no auth needed)
- [ ] `supervisord.conf` has `[group:optional]` with `[program:baileys-api]` at priority 5
- [ ] `supervisor.sh` starts `infra:* core:* relays:*` instead of `all`
- [x] API endpoints, Redis key prefix (`@baileys-api:`), response shapes, and connection status (webhook-only) verified against source code
- [x] Results of 0e documented — amendments applied inline with `[AMENDED: source verification]` markers

### Rollback
```bash
rm -f scripts/install_whatsapp.sh scripts/run_baileys.py
git checkout -- supervisord.conf supervisor.sh
```

---

## Step 1a — Enforce Envelope `action` Validation

**Branch:** `feat/whatsapp-adapter`
**Model tier:** Default
**PR:** Yes (same PR)

### Context Brief

`Envelope.from_parent()` does not set `action` — the docstring says "each producing brick must set action explicitly before publishing." But nothing enforces this, and **multiple existing code paths publish envelopes without action**: [AMENDED: review point 2]

- `atelier/main.py` — normal success response published to `outgoing_pending` with `action = ""`
- `sentinelle/main.py` — outgoing handler is a pure pass-through, does not stamp or validate action
- `commandant/commands.py` — `/help` and `/clear` handlers use `from_parent()` without setting action
- `souvenir/handlers/clear_handler.py` — clear confirmation uses `from_parent()` without setting action

**All of these must be fixed before activating validation.** The sequence is: fix all call sites → verify tests pass → activate the raise in `to_json()`.

### Task List

- [ ] **Fix all existing call sites** that publish envelopes without setting `action`:
  - Grep for `from_parent(` and `create_response_to(` across the entire codebase
  - For each: add `response.action = ACTION_MESSAGE_OUTGOING` (or appropriate action constant) after the call
  - Known sites to fix:
    - `atelier/main.py` — normal response path (add `ACTION_MESSAGE_OUTGOING_PENDING` before publish to `outgoing_pending`)
    - `commandant/commands.py` — `handle_clear`, `handle_help` (add `ACTION_MESSAGE_OUTGOING`)
    - `souvenir/handlers/clear_handler.py` — clear confirmation (add `ACTION_MESSAGE_OUTGOING`)
    - Any other site found by grep

- [ ] Run full test suite — **all tests must pass before the next sub-step**

- [ ] **Then** add validation in `common/envelope.py` `to_json()` that raises `ValueError` if `action` is empty or `None`:
  ```python
  def to_json(self) -> str:
      if not self.action:
          raise ValueError(
              "Envelope.action must be set before serialization. "
              "Set it explicitly after from_parent() — e.g. env.action = ACTION_MESSAGE_OUTGOING"
          )
      ...
  ```

- [ ] Run full test suite again — fix any tests broken by the new validation

### Verification Commands
```bash
pytest tests/ -x --timeout=30 -m "not integration"
ruff check common/envelope.py
```

### Exit Criteria
- [ ] All existing code paths set `action` before calling `to_json()`
- [ ] `to_json()` raises `ValueError` if `action` is empty/None
- [ ] All existing tests pass — no regressions
- [ ] Grep for `to_json()` confirms no remaining unprotected paths

### Rollback
```bash
git checkout main -- common/envelope.py atelier/main.py commandant/commands.py souvenir/
```

---

## Step 1b — Core Adapter Implementation

**Branch:** `feat/whatsapp-adapter`
**Model tier:** Strongest (Sonnet 4.6 or Opus)
**PR:** Yes

**Depends on:** Step 0 (gateway verified), Step 1a (action validation active), Step 3 (aiguilleur.yaml in DEFAULT_FILES) [AMENDED: review point 3]

### Context Brief

Read these files before starting:
- `aiguilleur/channels/discord/adapter.py` — full reference implementation (consumer name = `f"discord_{os.getpid()}"`, stop watcher polls `threading.Event` with `asyncio.sleep(0.5)`, **reads `self._adapter.config` live on every message**)
- `aiguilleur/core/native.py` — NativeAiguilleur.run() contract (`stop_event` is `threading.Event`, NOT `asyncio.Event`)
- `common/envelope.py` — `Envelope.from_parent()` does NOT set `action` — caller must set it
- `common/contexts.py` — `ensure_ctx(envelope, CTX_AIGUILLEUR)`, `AiguilleurCtx`
- `common/streams.py` — `STREAM_INCOMING`, `stream_outgoing()`, `KEY_WHATSAPP_PAIRING` (added in Step 3)
- `common/markdown_converter.py` — `convert_md_to_whatsapp()` (added in this step)

> **baileys-api API** (verified against source in Step 0e — DONE):
> - Health: `GET /status` — **no auth required**; `GET /status/auth` requires `x-api-key` [AMENDED: source verification]
> - Create connection: `POST /connections/:phoneNumber` with JSON body `{webhookUrl, webhookVerifyToken, includeMedia: false, syncFullHistory: false, ...}` — phoneNumber must include `+` prefix [AMENDED: source verification]
> - Send message: `POST /connections/:phoneNumber/send-message` with `{"jid": "<jid>", "messageContent": {"text": "<text>"}}` — messageContent is a union type (`{text}`, `{image}`, `{audio}`, etc.) [AMENDED: source verification]
> - Webhook events: `{"event": "messages.upsert", "data": {"messages": [...], "type": "notify"|"append"}, "webhookVerifyToken": "..."}` and `{"event": "connection.update", "data": {"connection": "open"|"connecting"|"close", "qrDataUrl": "data:image/png;base64,..."}}`
> - Auth: `x-api-key: <key>` header on all requests except `GET /status`
> - Webhook security: `webhookVerifyToken` at top level of every webhook body (NOT HMAC) — sufficient for localhost-only
> - **Response shape for send-message**: `{data: {key: {id, remoteJid, fromMe}, messageTimestamp: "<string>"}}` — confirmed [AMENDED: source verification]
> - **Error responses from send-message**: plain text (not JSON) — 409: `"Message is already being processed"`, 500: `"Message not sent"` [AMENDED: source verification]
> - **No per-connection status endpoint** — connection state tracked via webhook `connection.update` events only [AMENDED: source verification]

### Symbol Reference [AMENDED: review point 24]

All pseudocode in this step uses these exact attribute names, consistent with `__init__`:

| Symbol | Type | Source |
|---|---|---|
| `self._adapter` | `WhatsAppAiguilleur` | passed to `__init__`, used for live config access |
| `self._redis` | async Redis client | passed to `__init__` |
| `self._gateway_url` | `str` | from env `WHATSAPP_GATEWAY_URL` |
| `self._phone_number` | `str` | from env `WHATSAPP_PHONE_NUMBER` |
| `self._api_key` | `str` | from env `WHATSAPP_API_KEY` |
| `self._webhook_secret` | `str` | from env `WHATSAPP_WEBHOOK_SECRET` |
| `self._webhook_port` | `int` | from env `WHATSAPP_WEBHOOK_PORT` |
| `self._webhook_host` | `str` | from env `WHATSAPP_WEBHOOK_HOST` |
| `self._stop` | `threading.Event` | from `adapter.stop_event` |
| `self._log` | `logging.Logger` | `logging.getLogger("relais.whatsapp")` |
| `self._self_jid` | `str` | derived from `_phone_number` via `e164_to_jid()` |
| `self._http` | `aiohttp.ClientSession` | created in `start()` |
| `self.seen_message_ids` | `OrderedDict` | dedup LRU, max 1000 [AMENDED: review point 17] |
| `self.sent_message_ids` | `OrderedDict` | anti-loop LRU, max 1000 [AMENDED: review point 17] |
| `self.consumer_name` | `str` | `f"whatsapp_{os.getpid()}"` |

### Task List

- [ ] Create `aiguilleur/channels/whatsapp/__init__.py` with module docstring `"""WhatsApp channel adapter (Baileys gateway)."""`
- [ ] Create `aiguilleur/channels/whatsapp/adapter.py` implementing:
  - **`WhatsAppAiguilleur(NativeAiguilleur)`** — adapter lifecycle wrapper
  - **`_RelaisWhatsAppClient`** — business logic class (instantiated inside `run()`)

- [ ] Add `convert_md_to_whatsapp()` to `common/markdown_converter.py` [AMENDED: review point 33]

#### JID ↔ E.164 Normalization (module-level functions)

- [ ] Implement in `aiguilleur/channels/whatsapp/adapter.py`:
  ```python
  def normalize_whatsapp_id(jid: str) -> str:
      """'33699999999@s.whatsapp.net' → '+33699999999' (strips device suffix)."""
      return "+" + jid.split("@")[0].split(":")[0]   # [AMENDED: review point 26]

  def e164_to_jid(e164: str) -> str:
      """'+33699999999' → '33699999999@s.whatsapp.net'"""
      return e164.lstrip("+") + "@s.whatsapp.net"
  ```

#### Adapter Lifecycle (`WhatsAppAiguilleur`)

- [ ] `WhatsAppAiguilleur.run()` must: [AMENDED: review point 20]
  1. Read env vars: `WHATSAPP_GATEWAY_URL`, `WHATSAPP_API_KEY`, `WHATSAPP_PHONE_NUMBER`, `WHATSAPP_WEBHOOK_SECRET`, `WHATSAPP_WEBHOOK_PORT`, `WHATSAPP_WEBHOOK_HOST`.
  2. Wrap the entire body in a try/except:
     - **Config errors** (missing env var, invalid format): log `ERROR` with clear message + `return` (clean exit, no raise → no crash loop)
     - **Transient errors** (network, Redis): let them propagate → NativeAiguilleur restarts with backoff
  3. Instantiate `_RelaisWhatsAppClient` with `self` (the adapter) + redis client
  4. Call `await client.ensure_gateway_ready()` — health check only, does NOT block on pairing
  5. Call `await client.start()` which runs until `self.stop_event.is_set()`
  6. Call `await client.close()` in a `finally` block

#### Business Logic (`_RelaisWhatsAppClient`)

- [ ] `__init__(adapter, redis)` [AMENDED: review point 24]
  - `self._adapter = adapter` — for live config access [AMENDED: review point 31]
  - `self._redis = redis`
  - `self._log = logging.getLogger("relais.whatsapp")`
  - `self._stop = adapter.stop_event`
  - Read env vars into `self._gateway_url`, `self._phone_number`, `self._api_key`, `self._webhook_secret`, `self._webhook_port`, `self._webhook_host`
  - `self.sent_message_ids: OrderedDict[str, None] = OrderedDict()` — anti-loop LRU [AMENDED: review point 17]
  - `self.seen_message_ids: OrderedDict[str, None] = OrderedDict()` — dedup LRU [AMENDED: review point 17]
  - `self.consumer_name = f"whatsapp_{os.getpid()}"`
  - `self._self_jid` — derived from `_phone_number` via `e164_to_jid()`

- [ ] `async ensure_gateway_ready()` — **health check only, no pairing, no webhook server**: [AMENDED: review point 23, source verification]
  ```
  1. Poll GET /status (no auth required) every 2s, up to 30s.
     On config error (no gateway URL): log ERROR, return (don't block).
     On timeout: log WARNING "Gateway not reachable — adapter will start but incoming won't work until gateway is up."
  2. No per-connection status endpoint exists — connection state is tracked via webhook
     connection.update events. Log INFO "Gateway reachable. Connection state will be reported via webhook."
  3. Return (never block, never raise)
  ```

- [ ] `async start()` — runs concurrently via `asyncio.gather`: [AMENDED: review point 23]
  - **Webhook server** (aiohttp) — started HERE, not in ensure_gateway_ready
  - Outgoing consumer loop
  - Stop watcher coroutine (`_stop_watcher`)

- [ ] `async _stop_watcher()`:
  ```python
  async def _stop_watcher(self) -> None:
      """Poll threading.Event (NOT asyncio.Event) to detect shutdown."""
      while not self._stop.is_set():
          await asyncio.sleep(0.5)
      # trigger cleanup — cancel the gather
  ```

- [ ] `async close()` — shuts down aiohttp site + cancels outgoing task + closes `aiohttp.ClientSession`

- [ ] Live config access: always read `self._adapter.config` for `profile`, `prompt_path`, `streaming` — never cache these values. [AMENDED: review point 31]

#### Webhook Server

- [ ] Use `aiohttp.web.Application` with routes:
  - `POST /webhook` — main webhook handler
  - `GET /health` — returns `{"status": "ok"}` (used by `/settings whatsapp` health guard)

- [ ] `_verify_webhook_token(body: dict) -> bool`:
  - Extract `webhookVerifyToken` from body
  - Constant-time comparison with `self._webhook_secret`
  - Return `False` if missing or mismatch

- [ ] `_handle_webhook(request)`:
  1. Parse JSON body
  2. Verify webhook token via `_verify_webhook_token(body)` — return 401 if invalid
  3. Route by event type:
     - `"messages.upsert"` → `_handle_messages_upsert(payload)`
     - `"connection.update"` with `qrDataUrl` field → `_handle_qr_event(payload)`
     - `"connection.update"` with `connection == "open"` → `_handle_connected_event(payload)`
     - `"connection.update"` with `connection == "close"` → `_handle_close_event(payload)`
     - `"connection.update"` with `connection == "reconnecting"` → log INFO, no user message [AMENDED: review point 28]
     - All other events → return HTTP 200 (fire-and-forget)
  4. Always return HTTP 200 (webhook is fire-and-forget) [AMENDED: review point 22]

#### QR Event Handler (pairing)

- [ ] `_handle_qr_event(payload)` — **QR relay as ASCII art to pairing originator**:
  ```python
  async def _handle_qr_event(self, payload: dict) -> None:
      """Convert QR to ASCII art and relay to the channel that initiated pairing."""
      pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
      if not pairing_raw:
          self._log.debug("QR received but no active pairing session — ignoring")
          return

      pairing = json.loads(pairing_raw)

      import qrcode
      import io
      qr_raw = payload["data"].get("qr", "")
      if not qr_raw:
          self._log.warning("QR data URL received but no raw QR string — cannot render ASCII")
          return

      qr = qrcode.QRCode(border=1)
      qr.add_data(qr_raw)
      buf = io.StringIO()
      qr.print_ascii(out=buf, invert=True)
      ascii_qr = buf.getvalue()

      content = (
          "Scan this QR code with WhatsApp:\n"
          "WhatsApp > Settings > Linked Devices > Link a Device\n\n"
          f"```\n{ascii_qr}```"
      )

      env = Envelope(
          content=content,
          sender_id=pairing["sender_id"],
          channel=pairing["channel"],
          session_id=pairing["session_id"],
          correlation_id=pairing["correlation_id"],
          action=ACTION_MESSAGE_OUTGOING,
      )
      ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

      await self._redis.xadd(
          stream_outgoing(pairing["channel"]),
          {"payload": env.to_json()},
      )

      # Update pairing state
      pairing["state"] = "qr_displayed"
      await self._redis.set(KEY_WHATSAPP_PAIRING, json.dumps(pairing), ex=300)

      self._log.info("ASCII QR code relayed to %s channel for pairing", pairing["channel"])
  ```

#### Connected Event Handler (pairing confirmation)

- [ ] `_handle_connected_event(payload)`:
  ```python
  async def _handle_connected_event(self, payload: dict) -> None:
      """Notify originator that WhatsApp is now connected."""
      pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
      if not pairing_raw:
          self._log.info("WhatsApp connected (no active pairing context — likely a reconnect)")
          return

      pairing = json.loads(pairing_raw)
      env = Envelope(
          content="WhatsApp successfully linked! The adapter is now operational.",
          sender_id=pairing["sender_id"],
          channel=pairing["channel"],
          session_id=pairing["session_id"],
          correlation_id=pairing["correlation_id"],
          action=ACTION_MESSAGE_OUTGOING,
      )
      ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

      await self._redis.xadd(
          stream_outgoing(pairing["channel"]),
          {"payload": env.to_json()},
      )
      await self._redis.delete(KEY_WHATSAPP_PAIRING)
      self._log.info("WhatsApp pairing confirmed — adapter fully operational")
  ```

#### Close Event Handler (pairing or runtime) [AMENDED: review point 28]

- [ ] `_handle_close_event(payload)`:
  ```python
  async def _handle_close_event(self, payload: dict) -> None:
      """Handle connection close — notify admin if during pairing."""
      error_detail = payload.get("data", {}).get("lastDisconnect", {}).get("error", "unknown")

      pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
      if not pairing_raw:
          self._log.warning("WhatsApp connection closed (runtime): %s — baileys-api will auto-reconnect", error_detail)
          return

      pairing = json.loads(pairing_raw)

      # Specific error handling [AMENDED: review point 28]
      if "wrong_phone_number" in str(error_detail).lower():
          msg = (
              "WhatsApp pairing failed: wrong phone number.\n"
              "Check WHATSAPP_PHONE_NUMBER and re-run /settings whatsapp."
          )
      else:
          msg = (
              f"WhatsApp pairing failed: connection closed ({error_detail}).\n"
              "Re-run /settings whatsapp to try again."
          )

      env = Envelope(
          content=msg,
          sender_id=pairing["sender_id"],
          channel=pairing["channel"],
          session_id=pairing["session_id"],
          correlation_id=pairing["correlation_id"],
          action=ACTION_MESSAGE_OUTGOING,
      )
      ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

      await self._redis.xadd(
          stream_outgoing(pairing["channel"]),
          {"payload": env.to_json()},
      )
      await self._redis.delete(KEY_WHATSAPP_PAIRING)
      self._log.warning("WhatsApp pairing failed: %s", error_detail)
  ```

#### Incoming Message Processing

- [ ] `_handle_messages_upsert(payload)`:
  ```python
  async def _handle_messages_upsert(self, payload: dict) -> None:
      """Process incoming messages.upsert webhook event."""
      # Only process real-time messages, not history sync
      if payload["data"].get("type") != "notify":
          return

      for message in payload["data"].get("messages", []):
          try:
              await self._process_single_message(message)
          except Exception:
              self._log.exception("Failed to process message %s", message.get("key", {}).get("id", "?"))
      # Always return (caller returns HTTP 200) [AMENDED: review point 22]
  ```

- [ ] `_process_single_message(message)`:
  ```python
  async def _process_single_message(self, message: dict) -> None:
      msg_id = message.get("key", {}).get("id", "")

      # Deduplication — webhook retry protection (OrderedDict LRU) [AMENDED: review point 17]
      if msg_id in self.seen_message_ids:
          return
      self.seen_message_ids[msg_id] = None
      if len(self.seen_message_ids) > 1000:
          self.seen_message_ids.popitem(last=False)  # evict oldest

      jid = message.get("key", {}).get("remoteJid", "")
      from_me = message.get("key", {}).get("fromMe", False)
      is_self_chat = jid == self._self_jid

      # --- Filter group messages (adapter is sole source of truth) --- [AMENDED: review point 29]
      if "@g.us" in jid:
          return

      # --- Routing logic (Model A + Note-to-self) ---
      if is_self_chat:
          # "Note to self" conversation
          if not from_me:
              return  # impossible in practice, but defensive
          # fromMe in self-chat: admin talking to RELAIS
          # Anti-loop: skip if this is a message RELAIS sent
          if msg_id in self.sent_message_ids:
              return
          sender_e164 = normalize_whatsapp_id(self._self_jid)
      else:
          # External conversation
          if from_me:
              return  # admin replying manually — RELAIS does not interfere
          sender_e164 = normalize_whatsapp_id(jid)

      # Extract text content
      text = self._extract_text_content(message)
      if text is None:
          return  # non-text message — skip in MVP

      # Build Envelope
      sender_id = f"whatsapp:{sender_e164}"
      reply_jid = jid if not is_self_chat else self._self_jid

      # Read config live from adapter [AMENDED: review point 31]
      config = self._adapter.config

      envelope = Envelope(
          content=text,
          sender_id=sender_id,
          channel="whatsapp",
          session_id=f"whatsapp:{sender_e164}",
          action=ACTION_MESSAGE_INCOMING,
      )
      ctx = ensure_ctx(envelope, CTX_AIGUILLEUR)
      ctx["channel_profile"] = config.profile
      ctx["channel_prompt_path"] = config.prompt_path
      ctx["streaming"] = config.streaming
      ctx["content_type"] = "text"
      ctx["reply_to"] = reply_jid

      await self._redis.xadd(STREAM_INCOMING, {"payload": envelope.to_json()})
  ```

- [ ] `_extract_text_content(message) -> str | None`:
  ```python
  @staticmethod
  def _extract_text_content(message: dict) -> str | None:
      """Extract text from various WhatsApp message formats."""
      msg = message.get("message", {})
      if msg is None:
          return None
      # Priority order: plain text, extended text, image caption, video caption
      return (
          msg.get("conversation")
          or (msg.get("extendedTextMessage") or {}).get("text")
          or (msg.get("imageMessage") or {}).get("caption")
          or (msg.get("videoMessage") or {}).get("caption")
      )
  ```

#### Outgoing Consumer Loop [AMENDED: review points 21, 33]

- [ ] Consumer group: `"whatsapp_relay_group"`, consumer: `f"whatsapp_{os.getpid()}"`
- [ ] Create group on startup (idempotent: catch BUSYGROUP — see Discord adapter pattern)
- [ ] Read from `stream_outgoing("whatsapp")` with `XREADGROUP`, count=10, block=1000ms
- [ ] For each message:
  1. Deserialize `Envelope.from_json(msg["payload"])`
  2. Extract `to_jid = envelope.context[CTX_AIGUILLEUR]["reply_to"]`
  3. If `envelope.action == ACTION_MESSAGE_PROGRESS`: skip (no WhatsApp typing indicator in MVP), **always XACK**
  4. Apply `convert_md_to_whatsapp()` on `envelope.content` [AMENDED: review point 33]
  5. Split content via `_split_whatsapp_message()`, send each part via `_send_message()`
  6. Track `message_id` in `sent_message_ids` OrderedDict (anti-loop) [AMENDED: review point 17]
  7. On send error: route to `relais:messages:outgoing:failed` with `{source, message_id, payload, reason}`, then XACK [AMENDED: review point 21]
  8. On success: XACK
- [ ] Check `self._stop.is_set()` each loop iteration (poll `threading.Event`)

#### Send Message [AMENDED: review point 25]

- [ ] `_send_message(to_jid, text)`:
  ```python
  async def _send_message(self, to_jid: str, text: str) -> str | None:
      """Send text message via baileys-api. Returns message_id or None on error."""
      url = f"{self._gateway_url}/connections/{self._phone_number}/send-message"
      headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
      async with self._http.post(
          url, json={"jid": to_jid, "messageContent": {"text": text}}, headers=headers
      ) as resp:
          if resp.status >= 400:
              # Error responses are plain text (409: "Message is already being processed", 500: "Message not sent")
              body = await resp.text()
              self._log.warning("baileys-api send error %d: %s", resp.status, body)
              return None
          data = await resp.json()
          # Response shape (verified): {data: {key: {id, remoteJid, fromMe}, messageTimestamp: "<string>"}}
          msg_id = data.get("data", {}).get("key", {}).get("id")
          if msg_id:
              self.sent_message_ids[msg_id] = None
              if len(self.sent_message_ids) > 1000:
                  self.sent_message_ids.popitem(last=False)  # evict oldest [AMENDED: review point 17]
          return msg_id
  ```

#### Message Splitting

- [ ] `_split_whatsapp_message(text, max_len=4096) -> list[str]` — same algorithm as `_split_discord_message` in Discord adapter: split on `\n\n`, then `\n`, then space, then hard-cut.

### Verification Commands
```bash
python -m py_compile aiguilleur/channels/whatsapp/adapter.py
PYTHONPATH=. python -c "from aiguilleur.channels.whatsapp.adapter import WhatsAppAiguilleur; print('OK')"
ruff check aiguilleur/channels/whatsapp/
python -m py_compile common/markdown_converter.py
```

### Exit Criteria
- [ ] `aiguilleur/channels/whatsapp/adapter.py` exists and passes `py_compile`
- [ ] `WhatsAppAiguilleur` class exported from the module
- [ ] `convert_md_to_whatsapp()` added to `common/markdown_converter.py`
- [ ] Webhook handler routes all `connection.update` variants (QR, open, close, reconnecting, wrong_phone_number)
- [ ] `fromMe` + "Note to self" routing logic implemented
- [ ] Deduplication via `seen_message_ids` OrderedDict LRU + anti-loop via `sent_message_ids` OrderedDict LRU
- [ ] `_extract_text_content()` handles `conversation`, `extendedTextMessage`, captions
- [ ] All envelopes have `action` and `reply_to` set
- [ ] `_verify_webhook_token()` uses constant-time comparison
- [ ] Consumer name uses PID: `f"whatsapp_{os.getpid()}"`
- [ ] Stop watcher polls `threading.Event` with `asyncio.sleep(0.5)`
- [ ] Client reads `self._adapter.config` live — never caches config fields
- [ ] Outgoing failures routed to `relais:messages:outgoing:failed` (DLQ) before XACK
- [ ] Outgoing content cleaned via `convert_md_to_whatsapp()`
- [ ] `run()` catches config errors and returns cleanly (no crash loop)
- [ ] Webhook always returns HTTP 200, logs individual failures
- [ ] `ruff check` passes with no errors
- [ ] `pytest tests/ -x --timeout=30 -m "not integration"` passes (no regressions)

### Rollback
```bash
git checkout main -- aiguilleur/channels/whatsapp/ common/markdown_converter.py
```

---

## Step 2 — Commandant `/settings whatsapp` Command

**Branch:** `feat/whatsapp-adapter`
**Model tier:** Default
**PR:** Yes (same PR)

### Context Brief

Read these files before starting:
- `commandant/commands.py` — `COMMAND_REGISTRY`, `CommandSpec`, existing handlers (`handle_clear`, `handle_help`)
- `common/streams.py` — `stream_outgoing()`, `KEY_WHATSAPP_PAIRING`
- `common/envelope.py` — `Envelope.from_parent()` (does NOT set `action` — set it after)

The Commandant handler for `/settings whatsapp` must:
1. Verify the adapter is healthy (GET on webhook /health endpoint) [AMENDED: review point 4]
2. Verify `reply_to` is present — reject if absent [AMENDED: review point 30]
3. Call baileys-api to create a connection (triggering QR generation)
4. Store pairing context in Redis **only after** successful HTTP call [AMENDED: review point 18]
5. Reply immediately to the user

The handler does NOT wait for the QR or for pairing to complete — it fires and forgets. The async relay (QR display, confirmation, error) happens in the adapter's webhook handler.

**Command authorization** is controlled by `user_record.actions` in the user's role in `portail.yaml`. Roles with `actions: ["*"]` (e.g. admin) have access. Sentinelle enforces this upstream — the handler does not re-check the role. [AMENDED: review point 10]

### Task List

#### 2a. Add `/settings` command handler

- [ ] In `commandant/commands.py`, add:

  ```python
  import json
  import time
  import os

  try:
      import aiohttp
      _HAS_AIOHTTP = True
  except ImportError:
      _HAS_AIOHTTP = False


  async def handle_settings(envelope: Envelope, redis_conn: Any) -> None:
      """Handle /settings <subcommand>. Currently supports: whatsapp."""
      args = _parse_settings_args(envelope.content)

      if args.subcommand == "whatsapp":
          await _handle_settings_whatsapp(envelope, redis_conn)
      else:
          usage = (
              "Usage: /settings <subcommand>\n\n"
              "Available subcommands:\n"
              "  whatsapp — Link the bot's WhatsApp account via QR code (admin only)"
          )
          response = Envelope.from_parent(envelope, usage)
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(
              stream_outgoing(envelope.channel),
              {"payload": response.to_json()},
          )
  ```

- [ ] Implement `_handle_settings_whatsapp()`:

  ```python
  async def _handle_settings_whatsapp(envelope: Envelope, redis_conn: Any) -> None:
      """Initiate WhatsApp QR pairing flow."""
      if not _HAS_AIOHTTP:
          response = Envelope.from_parent(
              envelope,
              "WhatsApp integration requires aiohttp. Install with: uv sync --extra whatsapp",
          )
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
          return
          # [AMENDED: review point 35 — uv sync instead of pip install]

      # --- Verify reply_to is present --- [AMENDED: review point 30]
      aiguilleur_ctx = envelope.context.get(CTX_AIGUILLEUR, {})
      reply_to: str | None = aiguilleur_ctx.get("reply_to")
      if not reply_to:
          response = Envelope.from_parent(
              envelope,
              "Cannot determine reply destination — try from a different channel.",
          )
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
          return

      # --- Read env vars ---
      gateway_url = os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025")
      api_key = os.environ.get("WHATSAPP_API_KEY", "")
      phone_number = os.environ.get("WHATSAPP_PHONE_NUMBER", "")
      webhook_host = os.environ.get("WHATSAPP_WEBHOOK_HOST", "127.0.0.1")
      webhook_port = os.environ.get("WHATSAPP_WEBHOOK_PORT", "8765")
      webhook_secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")

      if not phone_number:
          response = Envelope.from_parent(
              envelope,
              "WHATSAPP_PHONE_NUMBER env var is not set. "
              "Set it to the bot's phone number in international format (e.g. +33612345678) and restart.",
          )
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
          return

      # --- Verify adapter health --- [AMENDED: review point 4]
      try:
          async with aiohttp.ClientSession() as session:
              async with session.get(
                  f"http://{webhook_host}:{webhook_port}/health",
                  timeout=aiohttp.ClientTimeout(total=3),
              ) as health_resp:
                  if health_resp.status != 200:
                      raise aiohttp.ClientError("unhealthy")
      except (aiohttp.ClientError, asyncio.TimeoutError):
          response = Envelope.from_parent(
              envelope,
              "WhatsApp adapter is not running. "
              "Enable whatsapp in aiguilleur.yaml, restart Aiguilleur, then retry.",
          )
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
          return

      # --- Call baileys-api to create/reconnect the connection ---
      headers = {"x-api-key": api_key, "Content-Type": "application/json"}
      connection_payload = {
          "webhookUrl": f"http://{webhook_host}:{webhook_port}/webhook",
          "webhookVerifyToken": webhook_secret,
          "includeMedia": False,   # default is True upstream — must be explicit for MVP text-only [AMENDED: source verification]
          "syncFullHistory": False,
      }
      # [AMENDED: review point 29 — removed groupsEnabled, adapter handles group filtering]

      try:
          async with aiohttp.ClientSession() as session:
              async with session.post(
                  f"{gateway_url}/connections/{phone_number}",
                  json=connection_payload,
                  headers=headers,
                  timeout=aiohttp.ClientTimeout(total=10),
              ) as resp:
                  if resp.status >= 400:
                      body = await resp.text()
                      response = Envelope.from_parent(
                          envelope,
                          f"Failed to initiate WhatsApp pairing (HTTP {resp.status}). "
                          f"Is baileys-api running at {gateway_url}?",
                      )
                      response.action = ACTION_MESSAGE_OUTGOING
                      await redis_conn.xadd(
                          stream_outgoing(envelope.channel),
                          {"payload": response.to_json()},
                      )
                      return
                      # [AMENDED: review point 18 — pairing key NOT written on error]
      except (aiohttp.ClientError, asyncio.TimeoutError):
          response = Envelope.from_parent(
              envelope,
              f"Cannot reach baileys-api at {gateway_url}. "
              "Start it with: supervisorctl start optional:baileys-api",
          )
          response.action = ACTION_MESSAGE_OUTGOING
          await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
          return

      # --- Store pairing context AFTER successful HTTP call --- [AMENDED: review point 18]
      pairing_context = {
          "channel": envelope.channel,
          "sender_id": envelope.sender_id,
          "session_id": envelope.session_id,
          "correlation_id": envelope.correlation_id,
          "reply_to": reply_to,
          "state": "pending_qr",
          "timestamp": time.time(),
      }
      await redis_conn.set(
          KEY_WHATSAPP_PAIRING,
          json.dumps(pairing_context),
          ex=300,  # 5 min TTL — covers multiple QR refresh cycles
      )
      # [AMENDED: review point 8 — global key for MVP, guard for double /settings via overwrite]

      # --- Reply — QR will arrive async via webhook → adapter → this channel ---
      response = Envelope.from_parent(
          envelope,
          "WhatsApp pairing initiated. A QR code will appear shortly.\n"
          "Open WhatsApp > Settings > Linked Devices > Link a Device, "
          "then scan the QR code when it appears.",
      )
      response.action = ACTION_MESSAGE_OUTGOING
      await redis_conn.xadd(stream_outgoing(envelope.channel), {"payload": response.to_json()})
  ```

- [ ] Helper to parse `/settings` args:
  ```python
  @dataclass
  class SettingsArgs:
      subcommand: str
      extra: str

  def _parse_settings_args(text: str) -> SettingsArgs:
      """Parse '/settings whatsapp' → SettingsArgs(subcommand='whatsapp', extra='')."""
      parts = text.strip().split(maxsplit=2)
      subcommand = parts[1].lower() if len(parts) > 1 else ""
      extra = parts[2] if len(parts) > 2 else ""
      return SettingsArgs(subcommand=subcommand, extra=extra)
  ```

#### 2b. Register in COMMAND_REGISTRY

- [ ] Add entry to `COMMAND_REGISTRY`:
  ```python
  "settings": CommandSpec(
      name="settings",
      description="Configure integrations. Usage: /settings whatsapp",
      handler=handle_settings,
  ),
  ```
  `KNOWN_COMMANDS` updates automatically. Sentinelle picks it up at next import.

### Verification Commands
```bash
python -m py_compile commandant/commands.py
PYTHONPATH=. python -c "from commandant.commands import KNOWN_COMMANDS; assert 'settings' in KNOWN_COMMANDS; print('OK')"
ruff check commandant/
```

### Exit Criteria
- [ ] `/settings` command registered in `COMMAND_REGISTRY`
- [ ] `handle_settings` dispatches to `_handle_settings_whatsapp` for `whatsapp` subcommand
- [ ] Unknown subcommands get a usage reply
- [ ] Adapter health checked before calling gateway [AMENDED: review point 4]
- [ ] `reply_to` validated — no fallback on `sender_id` [AMENDED: review point 30]
- [ ] Pairing context stored in Redis with 300s TTL — **only after** successful HTTP call [AMENDED: review point 18]
- [ ] All response envelopes have `action = ACTION_MESSAGE_OUTGOING`
- [ ] baileys-api called with correct payload and `x-api-key` header
- [ ] Graceful error handling: missing env vars, adapter not running, gateway unreachable, HTTP errors
- [ ] `ruff check` passes

### Rollback
```bash
git checkout main -- commandant/commands.py
```

---

## Step 3 — Configuration, Environment Variables & Redis ACL

**Branch:** `feat/whatsapp-adapter` (same PR)
**Model tier:** Default

### Context Brief

The Aiguilleur config cascade (from CLAUDE.md):
- System default: `config/aiguilleur.yaml.default` (checked in)
- User override: `~/.relais/config/aiguilleur.yaml`

**Currently `aiguilleur.yaml` is NOT bootstrapped by `initialize_user_dir()`** — it must be added to `DEFAULT_FILES`. [AMENDED: review point 3]

Env vars are the standard way to pass secrets. The `.env.example` file documents required vars.

Redis ACL is in `config/redis.conf`. RELAIS bricks use per-brick passwords with pattern-restricted key access.

### Task List

#### 3a. Aiguilleur channel config [AMENDED: review point 3]

- [ ] Update `config/aiguilleur.yaml.default` — add `whatsapp` under `channels:` (replace the commented-out example):
  ```yaml
  channels:
    # ... existing entries ...

    whatsapp:
      enabled: false          # disabled by default; user enables in ~/.relais/config/aiguilleur.yaml
      streaming: false        # baileys-api does not support streaming in MVP
      profile: default
      prompt_path: "channels/whatsapp_default.md"
      max_restarts: 5
  ```

- [ ] Add `aiguilleur.yaml` to `DEFAULT_FILES` in `common/init.py`:
  ```python
  ("config/aiguilleur.yaml", "config/aiguilleur.yaml.default"),
  ```

- [ ] Update the fallback in `aiguilleur/channel_config.py` `load_channels_config()` to log a warning:
  ```python
  except FileNotFoundError:
      logger.warning(
          "aiguilleur.yaml not found — falling back to discord-only default. "
          "Run initialize_user_dir() or copy config/aiguilleur.yaml.default to %s",
          _CHANNELS_CONFIG_FILE,
      )
      return {
          "discord": ChannelConfig(name="discord", enabled=True, streaming=True)
      }
  ```

#### 3b. Channel prompt [AMENDED: review point 32]

- [ ] **Update** (not create) `prompts/channels/whatsapp_default.md` — merge the existing French content with the plan's English conventions:
  ```markdown
  # WhatsApp Channel Prompt

  You are responding via WhatsApp.

  ## Channel Constraints

  - No complex Markdown: WhatsApp does not render headings (#), code blocks, or links in Markdown format.
  - Use `*text*` for bold and `_text_` for italic (WhatsApp native syntax).
  - Recommended max length: 500 characters per message.
  - For lists, use `•` or simple numbers.
  - Links are clickable — use them directly without Markdown formatting.
  - Emoji are fine — WhatsApp renders them natively.

  ## Tone

  Casual but professional. The user is on mobile — get to the point.
  ```
  This file is already in `DEFAULT_FILES` (source = destination, no `.default` suffix).

#### 3c. Environment variables

- [ ] Update `.env.example` — add WhatsApp section:
  ```bash
  # WhatsApp adapter (Baileys gateway — fazer-ai/baileys-api)
  # Required when whatsapp.enabled: true in aiguilleur.yaml
  WHATSAPP_GATEWAY_URL=http://localhost:3025
  WHATSAPP_PHONE_NUMBER=+33612345678        # bot's phone number in E.164 format
  WHATSAPP_API_KEY=                          # generated via: bun scripts/manage-api-keys.ts create user relais
  WHATSAPP_WEBHOOK_SECRET=your-webhook-secret
  WHATSAPP_WEBHOOK_PORT=8765
  WHATSAPP_WEBHOOK_HOST=127.0.0.1           # also used as callback URL for gateway (same-host only)

  # Redis password for baileys-api (TCP connection)
  REDIS_PASS_BAILEYS=pass_baileys
  ```

#### 3d. Dependencies

- [ ] Update `pyproject.toml` — add `aiohttp` and `qrcode` as optional dependencies (PEP 621 + Hatchling):
  ```toml
  [project.optional-dependencies]
  whatsapp = ["aiohttp>=3.9", "qrcode>=7.0"]
  ```

#### 3e. Redis ACL [AMENDED: review point 5]

- [ ] Update `config/redis.conf`:
  - Add `~relais:whatsapp:*` to the `aiguilleur` user's key patterns
  - Add `~relais:whatsapp:*` to the `commandant` user's key patterns
  - Add `&relais:config:reload:*` (Pub/Sub channel pattern) to the `aiguilleur` user
  - Add a new `baileys` user for the gateway (prefix verified in Step 0e: `@baileys-api:`):
    ```
    user baileys on >pass_baileys ~@baileys-api:* +@all
    # Covers: @baileys-api:connections:*, @baileys-api:api-keys:*, @baileys-api:idempotency:*
    ```
  - Enable TCP: add `port 6379` (keep `unixsocket` as-is), bind to `127.0.0.1` only

- [ ] Verification checklist:
  ```bash
  redis-cli -s ~/.relais/redis.sock ACL LIST | grep -E "aiguilleur|commandant|baileys"
  # Verify patterns include relais:whatsapp:* for aiguilleur and commandant
  # Verify baileys user has keyspace prefix ~@baileys-api:*
  ```

#### 3f. Redis key constants

- [ ] Add to `common/streams.py`:
  ```python
  KEY_WHATSAPP_PAIRING = "relais:whatsapp:pairing"
  ```

### Verification Commands
```bash
python -c "import yaml; yaml.safe_load(open('config/aiguilleur.yaml.default'))"
test -f prompts/channels/whatsapp_default.md && echo "OK"
grep -q "WHATSAPP_GATEWAY_URL" .env.example && echo "OK"
grep -q "KEY_WHATSAPP_PAIRING" common/streams.py && echo "OK"
grep -q "port 6379" config/redis.conf && echo "OK"
grep -q "baileys" config/redis.conf && echo "OK"
grep -q "aiguilleur.yaml" common/init.py && echo "OK"
```

### Exit Criteria
- [ ] `config/aiguilleur.yaml.default` has `whatsapp` entry under `channels:` with `enabled: false`
- [ ] `initialize_user_dir()` copies `aiguilleur.yaml.default` to user config if missing [AMENDED: review point 3]
- [ ] `load_channels_config()` logs warning on fallback [AMENDED: review point 3]
- [ ] `prompts/channels/whatsapp_default.md` updated (English, WhatsApp native format) [AMENDED: review point 32]
- [ ] `.env.example` documents all `WHATSAPP_*` + `REDIS_PASS_BAILEYS` env vars
- [ ] `pyproject.toml` has `whatsapp` optional dependencies (PEP 621 format)
- [ ] `config/redis.conf` has TCP enabled (localhost only), `baileys` user (prefix from Step 0e), updated ACLs
- [ ] `common/streams.py` has `KEY_WHATSAPP_PAIRING` constant

---

## Step 4 — Tests

**Branch:** `feat/whatsapp-adapter` (same PR)
**Model tier:** Default

### Context Brief

Test conventions (from CLAUDE.md and existing tests):
- Files: `tests/test_whatsapp_adapter.py` and `tests/test_commandant_settings.py`
- Use `pytest-asyncio` for async tests
- Use `@pytest.mark.unit` for fast tests
- Mock Redis with `unittest.mock.AsyncMock`
- Never test against a real baileys-api instance (unit tests only)
- Mock HTTP responses must match the shape verified in Step 0e [AMENDED: review point 41]

### Task List

#### 4a. Adapter tests (`tests/test_whatsapp_adapter.py`)

**Normalization:**
- [ ] **Test 1**: `normalize_whatsapp_id("33699999999@s.whatsapp.net")` → `"+33699999999"`
- [ ] **Test 2**: `normalize_whatsapp_id("33699999999:2@s.whatsapp.net")` → `"+33699999999"` (device suffix stripped) [AMENDED: review point 26]
- [ ] **Test 3**: `e164_to_jid("+33699999999")` → `"33699999999@s.whatsapp.net"`

**Webhook routing:**
- [ ] **Test 4**: Webhook token valid → 200
- [ ] **Test 5**: Webhook token invalid → 401, no `xadd`
- [ ] **Test 6**: Non-message events return 200 without `xadd`
- [ ] **Test 7**: `connection.update` with `reconnecting` → 200, log only, no user message [AMENDED: review point 28]

**Incoming message processing:**
- [ ] **Test 8**: `_extract_text_content` handles `conversation`, `extendedTextMessage.text`, `imageMessage.caption`, `None`
- [ ] **Test 9**: `_process_single_message` builds correct Envelope — channel, sender_id (E.164), content, `action=ACTION_MESSAGE_INCOMING`, `reply_to` as JID
- [ ] **Test 10**: `messages.upsert` with `type="append"` → ignored (history sync)
- [ ] **Test 11**: Group JID (`@g.us`) → ignored
- [ ] **Test 12**: `fromMe: true` in non-self conversation → ignored (admin replying manually)
- [ ] **Test 13**: `fromMe: true` in self conversation → treated as admin message
- [ ] **Test 14**: Anti-loop: message in `sent_message_ids` → ignored (RELAIS's own reply)
- [ ] **Test 15**: Deduplication: same `message_id` twice → only one `xadd`
- [ ] **Test 16**: Batch: individual `xadd` failure does not prevent other messages from processing, always returns 200 [AMENDED: review point 22]

**Owner identity (self-chat):** [AMENDED: review point 37]
- [ ] **Test 17**: Self-chat message produces `sender_id = "whatsapp:+33612345678"` (owner number) that resolves via `identifiers.whatsapp.self` in portail.yaml
- [ ] **Test 18**: `UserRegistry.resolve_user("whatsapp:+33612345678", "whatsapp")` with `identifiers.whatsapp.self: "+33612345678"` → resolves to `usr_admin`
- [ ] **Test 19**: `UserRegistry.resolve_user("whatsapp:+33612345678", "whatsapp")` without `self` field → does NOT resolve (no false positive via `dm`)

**QR/pairing:**
- [ ] **Test 20**: `_handle_qr_event` relays ASCII QR to originator channel with correct `reply_to`
- [ ] **Test 21**: `_handle_qr_event` ignores QR when no pairing context
- [ ] **Test 22**: `_handle_connected_event` sends confirmation with correct `reply_to` + deletes pairing key
- [ ] **Test 23**: `_handle_close_event` during pairing → error message to admin + deletes pairing key
- [ ] **Test 24**: `_handle_close_event` with `wrong_phone_number` → specific error message [AMENDED: review point 28]
- [ ] **Test 25**: `_handle_close_event` outside pairing (runtime) → log only, no message sent

**Outgoing:**
- [ ] **Test 26**: `_send_message` uses correct URL, body, and `x-api-key` header. Mock response matches Step 0e verified shape. [AMENDED: review point 41]
- [ ] **Test 27**: `_split_whatsapp_message` splits long messages — all parts <= 4096
- [ ] **Test 28**: Outgoing consumer sends + tracks `message_id` in `sent_message_ids` + ACKs
- [ ] **Test 29**: Outgoing send failure → routes to `relais:messages:outgoing:failed` (DLQ) + ACKs [AMENDED: review point 21]

**Lifecycle:** [AMENDED: review point 20]
- [ ] **Test 30**: Missing env var → `run()` logs error and returns cleanly (no raise, no crash loop)

**Live config:** [AMENDED: review point 42]
- [ ] **Test 31**: Client reads `self._adapter.config` on each message — mock a config change between two messages → verify second message uses updated value

**Markdown conversion:** [AMENDED: review point 33]
- [ ] **Test 32**: `convert_md_to_whatsapp("**bold**")` → `"*bold*"`
- [ ] **Test 33**: `convert_md_to_whatsapp("*italic*")` → `"_italic_"`
- [ ] **Test 34**: `convert_md_to_whatsapp("```code```")` → code block stripped

**Portail resolution (integration-style, no Redis):**
- [ ] **Test 35**: `UserRegistry.resolve_user("whatsapp:+33699999999", "whatsapp")` resolves correctly when `identifiers.whatsapp.dm: "+33699999999"` is set

#### 4b. Commandant settings tests (`tests/test_commandant_settings.py`)

- [ ] **Test 36**: `/settings whatsapp` stores pairing context with `reply_to` and `state` in Redis — **after** successful HTTP call [AMENDED: review point 18]
- [ ] **Test 37**: `/settings whatsapp` calls baileys-api `POST /connections/:phone` with `x-api-key` header
- [ ] **Test 38**: `/settings whatsapp` replies with `ACTION_MESSAGE_OUTGOING`
- [ ] **Test 39**: `/settings whatsapp` graceful error when gateway unreachable — pairing key NOT in Redis [AMENDED: review point 18]
- [ ] **Test 40**: `/settings whatsapp` graceful error when HTTP 4xx — pairing key NOT in Redis [AMENDED: review point 39]
- [ ] **Test 41**: `/settings whatsapp` error when `WHATSAPP_PHONE_NUMBER` not set
- [ ] **Test 42**: `/settings whatsapp` rejected when adapter health check fails [AMENDED: review point 38]
- [ ] **Test 43**: `/settings whatsapp` rejected when adapter health check times out [AMENDED: review point 38]
- [ ] **Test 44**: `/settings whatsapp` rejected when `reply_to` absent from context [AMENDED: review point 30]
- [ ] **Test 45**: `/settings unknown` replies with usage text
- [ ] **Test 46**: `_parse_settings_args` parses correctly (unit, no async)

#### 4c. Envelope validation test

- [ ] **Test 47**: `Envelope.to_json()` raises `ValueError` when `action` is empty

### Verification Commands
```bash
pytest tests/test_whatsapp_adapter.py tests/test_commandant_settings.py -v -x --timeout=30
```

### Exit Criteria
- [ ] All 47 tests pass
- [ ] No import errors
- [ ] Tests follow `@pytest.mark.unit` convention

---

## Step 5 — Documentation

**Branch:** `feat/whatsapp-adapter` (same PR)
**Model tier:** Default

### Context Brief

Docs language is **English only**. Brick names (Aiguilleur, Portail, etc.) stay French. README is install/config/run only.

### Task List

- [ ] Create `docs/WHATSAPP_SETUP.md` — full setup guide:
  - Prerequisites: Bun (or Docker as secondary), a WhatsApp account
  - **Run `scripts/install_whatsapp.sh`** (automated install with pinned SHA) [AMENDED: review point 14]
  - API key creation
  - Environment variables
  - Redis configuration: enable TCP port (localhost only), create `baileys` ACL user
  - supervisord: `supervisorctl start optional:baileys-api` [AMENDED: review point 11]
  - Enable WhatsApp in `aiguilleur.yaml`, restart Aiguilleur [AMENDED: review point 4]
  - **Recommended: set `unknown_user_policy: deny` in portail.yaml** [AMENDED: review point 9]
  - **Map owner number** in `portail.yaml` under `identifiers.whatsapp.self` [AMENDED: review point 1]
  - **Pairing via `/settings whatsapp`**: step-by-step
    - Admin types `/settings whatsapp` on any channel
    - ASCII QR code appears in the channel
    - Scan with WhatsApp > Settings > Linked Devices > Link a Device
    - Confirmation message appears
  - Session persistence: Signal keys in Redis (baileys-api), survives restarts
  - Contact identity: manually add contacts in `portail.yaml` under `identifiers.whatsapp.dm` (E.164 format)
  - "Note to self" model: admin's self-chat = talking to RELAIS
  - Security: webhook token is NOT HMAC — sufficient for localhost only
  - **Networking limitation**: webhook host is used as both bind and callback — same-host only. For Docker: `host.docker.internal` or `--network=host` [AMENDED: review point 7]
  - Troubleshooting: gateway not reachable, QR not appearing, session expired, `fromMe` loop, wrong_phone_number
  - **Known MVP limitation**: in-memory deduplication does not survive adapter restarts [AMENDED: review point 16]

- [ ] Update `docs/ARCHITECTURE.md`:
  - Add WhatsApp adapter to Aiguilleur section
  - Document webhook-based inbound (aiohttp on `WHATSAPP_WEBHOOK_PORT`)
  - Document outgoing via baileys-api REST API with DLQ on failure [AMENDED: review point 21]
  - Document "Note to self" routing model, `fromMe` filtering, and `identifiers.whatsapp.self` [AMENDED: review point 1]
  - Document pairing flow through Commandant
  - Note baileys-api is an external dependency (not a RELAIS brick)
  - **Add `aiguilleur.yaml` to the list of files bootstrapped by `initialize_user_dir()`** [AMENDED: review point 34]
  - Note hot-reload limitation: adding/removing channels requires Aiguilleur restart [AMENDED: review point 4]

- [ ] Update `README.md`:
  - Add WhatsApp to supported channels
  - List required env vars (`WHATSAPP_*`)
  - Link to `docs/WHATSAPP_SETUP.md`
  - Note: baileys-api in `[group:optional]`, not started by default [AMENDED: review point 11]
  - **Add `aiguilleur.yaml` to bootstrapped files list** [AMENDED: review point 34]

- [ ] Update `docs/REDIS_BUS_API.md`:
  - Add `whatsapp_relay_group` consumer group on `relais:messages:outgoing:whatsapp`
  - Add `KEY_WHATSAPP_PAIRING` key documentation (type: String, TTL: 300s, purpose: QR pairing context with `reply_to` and `state`)
  - Document `relais:config:reload:portail` Pub/Sub usage (message must be exactly `"reload"`)

- [ ] Update `docs/ENV.md`:
  - Add all `WHATSAPP_*` env vars with descriptions
  - Add `REDIS_PASS_BAILEYS`

- [ ] Update `CLAUDE.md`:
  - Add `/settings` to Commandant command list
  - Add `KEY_WHATSAPP_PAIRING` Redis key
  - Note "Note to self" routing model with `identifiers.whatsapp.self`
  - Update Redis section: TCP enabled on port 6379 (localhost only)
  - Note `[group:optional]` supervisord group
  - Note `aiguilleur.yaml` now in `DEFAULT_FILES`

### Verification Commands
```bash
grep -q "whatsapp" docs/ARCHITECTURE.md && echo "ARCH OK"
grep -q "WHATSAPP" README.md && echo "README OK"
grep -q "WHATSAPP" docs/ENV.md && echo "ENV OK"
grep -q "settings" CLAUDE.md && echo "CLAUDE.MD OK"
test -f docs/WHATSAPP_SETUP.md && echo "SETUP OK"
```

### Exit Criteria
- [ ] `docs/WHATSAPP_SETUP.md` exists with full setup guide including all amendments
- [ ] `docs/ARCHITECTURE.md` mentions WhatsApp adapter, Note-to-self with `whatsapp.self`, hot-reload limitation, DLQ pattern
- [ ] `README.md` lists WhatsApp env vars and `aiguilleur.yaml` in bootstrapped files
- [ ] `docs/REDIS_BUS_API.md` documents new stream + Redis key
- [ ] `docs/ENV.md` lists all new env vars
- [ ] `CLAUDE.md` updated with all new concepts
- [ ] No French text in docs (brick names in French OK)

---

## PR Strategy

All steps ship as a **single PR** (`feat/whatsapp-adapter` → `main`) since they form one coherent feature.

**PR title:** `feat(aiguilleur+commandant): WhatsApp adapter via Baileys with /settings pairing`

**PR description template:**
```markdown
## Summary
- Integrates fazer-ai/baileys-api (Bun + Baileys) as an optional supervisord-managed WhatsApp gateway (`[group:optional]`)
- Adds `WhatsAppAiguilleur` NativeAiguilleur adapter in `aiguilleur/channels/whatsapp/`
- Adds `/settings whatsapp` command in Commandant with adapter health guard
- Shared bot number model: admin's personal WhatsApp linked as RELAIS channel
- Owner identity via `identifiers.whatsapp.self` in portail.yaml (distinct from contact `dm`)
- "Note to self" = admin talking to RELAIS; external contacts resolved via portail.yaml
- Enforces `Envelope.action` validation in `to_json()` across all existing call sites
- Inbound: aiohttp webhook receives baileys-api events → Redis STREAM_INCOMING
- Outbound: Redis consumer group on `relais:messages:outgoing:whatsapp` → baileys-api REST, failures to DLQ
- Outgoing content cleaned via `convert_md_to_whatsapp()` from `common/markdown_converter.py`

## Architecture
- **Model**: Shared bot number (like Discord/Telegram). One WhatsApp account = RELAIS.
- **"Note to self"**: Admin writes in self-chat → RELAIS processes. Admin replies manually in other chats → ignored.
- **Owner identity**: `portail.yaml` `identifiers.whatsapp.self` maps the bot number to `usr_admin`.
- **Contact identity**: portail.yaml `identifiers.whatsapp.dm` maps E.164 numbers to RELAIS users (manual config).
- **JID normalization**: `33699999999:2@s.whatsapp.net` ↔ `+33699999999` at adapter boundary (device suffix stripped).
- **Access policy**: Recommended `unknown_user_policy: deny`, DM only, text only.

## Pairing Flow
1. Admin: `/settings whatsapp` (on Discord, Telegram, etc.)
2. Handler verifies adapter health + reply_to presence
3. Handler calls baileys-api, stores pairing context in Redis (300s TTL) only on success
4. baileys-api emits QR via webhook → adapter converts to ASCII art → relays to admin's channel
5. Admin scans QR with WhatsApp mobile
6. baileys-api emits `connection: "open"` → adapter sends confirmation
7. WhatsApp is now active — messages flow through the pipeline

## Gateway
External Node.js service: https://github.com/fazer-ai/baileys-api (pinned commit in scripts/install_whatsapp.sh)
- Install: `scripts/install_whatsapp.sh` (automated) or Docker as secondary
- Session storage: Redis (shared via TCP, dedicated `baileys` ACL user)
- Based on Baileys (8 900+ stars, WebSocket, no browser)

## Test Plan
- [ ] `pytest tests/test_whatsapp_adapter.py tests/test_commandant_settings.py -v -x --timeout=30` — 47 tests pass
- [ ] `pytest tests/ -x --timeout=30 -m "not integration"` — no regressions
- [ ] `ruff check aiguilleur/channels/whatsapp/ commandant/ common/markdown_converter.py` — no lint errors
- [ ] Manual: start baileys-api, `/settings whatsapp` on Discord, scan QR, send test message E2E
```

---

## Known Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Bun not installed on target machine | `scripts/install_whatsapp.sh` checks and reports. `scripts/run_baileys.py` exits cleanly if missing — no crash loop. [AMENDED: review points 11, 14] |
| baileys-api is relatively new (62 stars) | Baileys itself has 8 900+ stars; the wrapper is thin and replaceable. Pin to verified commit in `scripts/install_whatsapp.sh`. |
| QR code expires (~60s) | Pairing key TTL is 300s (covers multiple QR refresh cycles); baileys-api auto-regenerates QR; admin can re-run `/settings whatsapp` |
| WhatsApp bans (ToS violation) | Same risk as any unofficial client; documented in setup guide |
| ASCII QR rendering quality | Depends on channel font (monospace). Discord code blocks render well. Telegram may need adjustment. Fallback: admin opens baileys-api dashboard directly. |
| baileys-api uses RELAIS Redis (shared) | Dedicated `baileys` ACL user restricted to `~@baileys-api:*`. TCP on localhost only. No key collision. [AMENDED: review point 5, source verification] |
| `fromMe` anti-loop failure | Dual protection: `sent_message_ids` OrderedDict LRU (tracks RELAIS outbound) + "Note to self" routing (only processes self-chat). Belt and suspenders. [AMENDED: review point 17] |
| Webhook replay / dedup after restart | `seen_message_ids` is in-memory only — does NOT survive restarts. Documented as MVP debt. Future: Redis SETNX with TTL. [AMENDED: review point 16] |
| Webhook security is token-based, not HMAC | Sufficient for localhost-only (adapter and gateway on same machine). Documented in setup guide. |
| Outgoing message loss (5xx gateway) | Aligned with Discord: route to `relais:messages:outgoing:failed` (DLQ) before XACK. [AMENDED: review point 21] |
| `portail.yaml` write needed for contacts | Not part of this plan (pairing ≠ contact registration). Manual edit or future `/settings whatsapp link` command. |
| Docker networking (webhook unreachable) | Documented: use `host.docker.internal` or `--network=host`. Primary mode is native (supervisord). Same-host limitation documented. [AMENDED: review point 7] |
| Unknown contacts get RELAIS response | Recommended `unknown_user_policy: deny` in setup guide. [AMENDED: review point 9] |
| Hot-reload cannot add new channels | Documented: enable in aiguilleur.yaml + restart Aiguilleur. `/settings whatsapp` has adapter health guard. [AMENDED: review point 4] |
| `supervisor.sh start all` breaks with optional service | Replaced with `start infra:* core:* relays:*`. baileys-api in `[group:optional]`. [AMENDED: review point 11] |
| Wrong phone number during pairing | Specific error message via `_handle_close_event`. [AMENDED: review point 28] |

---

## File Manifest (new/modified)

| File | Status | Notes |
|---|---|---|
| `scripts/install_whatsapp.sh` | NEW | Automated install with pinned SHA [AMENDED: review points 14, 15] |
| `scripts/run_baileys.py` | NEW | Supervisord wrapper with prereq checks [AMENDED: review point 11] |
| `aiguilleur/channels/whatsapp/__init__.py` | NEW | |
| `aiguilleur/channels/whatsapp/adapter.py` | NEW | Adapter + normalization + anti-loop + DLQ |
| `common/envelope.py` | MODIFIED | Add `action` validation in `to_json()` |
| `common/streams.py` | MODIFIED | Add `KEY_WHATSAPP_PAIRING` |
| `common/init.py` | MODIFIED | `initialize_user_dir()` copies `aiguilleur.yaml.default` [AMENDED: review point 3] |
| `common/markdown_converter.py` | MODIFIED | Add `convert_md_to_whatsapp()` [AMENDED: review point 33] |
| `atelier/main.py` | MODIFIED | Fix `action` on normal response path [AMENDED: review point 2] |
| `commandant/commands.py` | MODIFIED | Add `/settings` handler, fix `action` on existing handlers [AMENDED: review point 2] |
| `souvenir/handlers/clear_handler.py` | MODIFIED | Fix `action` on clear confirmation [AMENDED: review point 2] |
| `aiguilleur/channel_config.py` | MODIFIED | Warning on fallback [AMENDED: review point 3] |
| `config/redis.conf` | MODIFIED | TCP port (localhost), `baileys` user, updated ACLs |
| `docs/WHATSAPP_SETUP.md` | NEW | Setup + pairing guide |
| `prompts/channels/whatsapp_default.md` | MODIFIED | English, WhatsApp native format [AMENDED: review point 32] |
| `tests/test_whatsapp_adapter.py` | NEW | 35 tests |
| `tests/test_commandant_settings.py` | NEW | 12 tests |
| `supervisord.conf` | MODIFIED | Add `[group:optional]` + `[program:baileys-api]` |
| `supervisor.sh` | MODIFIED | Start specific groups instead of `all` [AMENDED: review point 11] |
| `config/aiguilleur.yaml.default` | MODIFIED | Add whatsapp under channels |
| `.env.example` | MODIFIED | Add `WHATSAPP_*` + `REDIS_PASS_BAILEYS` vars |
| `pyproject.toml` | MODIFIED | Add whatsapp optional deps (PEP 621) |
| `docs/ARCHITECTURE.md` | MODIFIED | |
| `docs/REDIS_BUS_API.md` | MODIFIED | |
| `docs/ENV.md` | MODIFIED | |
| `README.md` | MODIFIED | |
| `CLAUDE.md` | MODIFIED | |

---

## Review Decisions Log

All decisions from the plan review (43 points) are integrated inline with `[AMENDED: review point N]` markers. Summary:

| # | Topic | Decision |
|---|---|---|
| 1 | Owner identity | `identifiers.whatsapp.self` in portail.yaml |
| 2 | Envelope.action debt | Step 1a enlarged to all call sites |
| 3 | aiguilleur.yaml bootstrap | Added to DEFAULT_FILES + warning in fallback |
| 4 | Hot-reload limitation | Documented + health guard in /settings |
| 5 | Redis ACL prefix | `@baileys-api:*` (verified — literal `@` prefix) |
| 6 | x-api-key on healthcheck | `GET /status` needs no auth; `GET /status/auth` needs key; mutations need key |
| 7 | Webhook host dual use | Documented same-host limitation |
| 8 | Global pairing key | Kept for MVP with overwrite guard |
| 9 | Access policy | Recommend unknown_user_policy=deny |
| 10 | /settings auth | Rely on Sentinelle, no double check |
| 11 | supervisor.sh start all | [group:optional] + wrapper script + start specific groups |
| 12 | Group separation | Covered by point 11 |
| 13 | RELAIS_HOME paths | $RELAIS_HOME/vendor/ everywhere |
| 14 | PR vs operations | scripts/install_whatsapp.sh versioned |
| 15 | Pinned SHA | Constant in scripts/install_whatsapp.sh |
| 16 | Dedup persistence | In-memory for MVP, risk documented |
| 17 | LRU implementation | OrderedDict |
| 18 | Pairing key on error | Written after HTTP success only |
| 19 | Adapter not running | Covered by point 4 health guard |
| 20 | Config error crash loop | try/except in run(), config vs transient |
| 21 | Outbound DLQ | Aligned with Discord pattern |
| 22 | Batch HTTP 200 | Always 200, log individual failures |
| 23 | Lifecycle clarity | ensure=health check, start=launch all |
| 24 | Symbol consistency | All symbols fixed in pseudocode |
| 25 | Send response shape | `{data: {key: {id, remoteJid, fromMe}, messageTimestamp}}` — verified |
| 26 | JID device suffix | .split("@")[0].split(":")[0] |
| 27 | Connection status endpoint | No dedicated endpoint — state via webhook `connection.update` only |
| 28 | wrong_phone_number | Handled in close event + reconnecting logged |
| 29 | Triple group filter | Adapter filter only, removed gateway redundancy |
| 30 | reply_to fallback | Guard explicit, no fallback on sender_id |
| 31 | Live config access | Read self._adapter.config per message |
| 32 | whatsapp_default.md | MODIFIED, merged English + WhatsApp native |
| 33 | Outgoing formatting | convert_md_to_whatsapp() via markdown_converter.py |
| 34 | Docs for aiguilleur.yaml | Absorbed in Step 5 |
| 35 | pip vs uv | uv sync --extra whatsapp |
| 36 | Doc language | English only (confirmed, not an issue) |
| 37 | Test self-chat identity | Added to Step 4 |
| 38 | Test health guard | Added to Step 4 |
| 39 | Test pairing key cleanup | Added to Step 4 |
| 40 | Test dedup persistence | Rejected (no persistent dedup in MVP) |
| 41 | Test HTTP shape | Mocks match Step 0e contract |
| 42 | Test wrong_phone + live config | Added to Step 4 |
| 43 | Test supervisor impact | Rejected (covered by design) |

---

## Plan Mutation Protocol

To modify this plan:
- **Split a step**: Rename existing step, add new step with dependency noted
- **Skip a step**: Mark `[SKIPPED: reason]` — do not delete
- **Abandon plan**: Mark header `Status: ABANDONED` with reason
- **Change scope**: Add `[AMENDED: date — reason]` note to affected step
