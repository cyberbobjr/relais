# REDIS_BUS_API — RELAIS Message Bus Reference

This document is the **canonical reference** for all Redis Streams and Pub/Sub channels used by RELAIS bricks.

> **Rule**: Any brick that publishes or subscribes to a Redis channel MUST conform to the schemas defined here.
> Do not infer message formats from brick implementations — consult this document first.

---

## Overview

RELAIS uses two Redis primitives for inter-brick communication:

| Primitive | Persistence | Delivery | Use case |
|-----------|------------|----------|----------|
| **Stream** (`XADD` / `XREADGROUP`) | Persisted, replayable | At-least-once via consumer groups | Pipeline messages, audit logs |
| **Pub/Sub** (`PUBLISH` / `SUBSCRIBE`) | Ephemeral, fire-and-forget | Best-effort, real-time only | Streaming signals, monitoring events |

All stream messages carry a single `payload` field containing a JSON-serialized object.

---

## Envelope (shared schema)

Every pipeline stream message wraps its content in an **Envelope**.

```json
{
  "content": "Hello!",
  "sender_id": "discord:123456789",
  "channel": "discord",
  "session_id": "sess-abc",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": 1711234567.890,
  "action": "message_validated",
  "traces": [
    {"brick": "portail", "action": "validated", "timestamp": 1711234567.900}
  ],
  "context": {
    "portail": {
      "user_id": "usr_admin",
      "user_record": {"user_id": "usr_admin", "display_name": "Admin"},
      "llm_profile": "default",
      "session_start": 1711234567.890
    },
    "aiguilleur": {
      "channel_profile": "default",
      "channel_prompt_path": "channels/discord_default.md",
      "content_type": "text",
      "reply_to": "999888777"
    }
  },
  "media_refs": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | `string` | Main text content of the message |
| `sender_id` | `string` | Originating user ID, prefixed by channel: `discord:{id}`, `telegram:{id}` |
| `channel` | `string` | Channel name: `discord`, `telegram`, `tui`, `api`, … |
| `session_id` | `string` | Stable session identifier (used by Souvenir for context lookup) |
| `correlation_id` | `string` | UUID, propagated end-to-end for tracing |
| `timestamp` | `float` | Unix epoch (seconds) |
| `action` | `string` | Self-describing action token (e.g. `message_incoming`, `message_validated`, `message_task`) from `common/envelope_actions.py`. **Required** — `Envelope.to_json()` raises `ValueError` if `action` is empty. Producing sites must set it explicitly after `Envelope.from_parent()` / `Envelope.create_response_to()` (both intentionally clear the parent action). |
| `traces` | `array` | Pipeline trace list (each entry: `{brick: string, action: string, timestamp: float}`) |
| `context` | `object` | Namespaced context dictionary. Each brick writes only to `context[CTX_SELF]` but may read any namespace. Namespace constants defined in `common/contexts.py` |
| `media_refs` | `array` | List of `MediaRef` objects (see below) |

### MediaRef schema

```json
{
  "media_id": "media-uuid",
  "path": "/tmp/relais/media/file.png",
  "mime_type": "image/png",
  "size_bytes": 204800,
  "expires_in_hours": 24
}
```

---

## Redis Streams

### `relais:messages:incoming`

**Direction**: Aiguilleur → Portail
**Consumer groups**: `portail_group`

Carries raw inbound messages from external channel adapters before any validation.

```
XADD relais:messages:incoming * payload <Envelope JSON>
```

**Context keys set by Aiguilleur (in `context.aiguilleur`)**:

| Key | Type | Description |
|-----|------|-------------|
| `channel_profile` | `string` (optional) | LLM profile name, resolved in order: `aiguilleur.yaml:profile` → `config.yaml:llm.default_profile` → `"default"`. Stamped by the Aiguilleur adapter at envelope creation time. Read by Portail to derive `llm_profile`. |
| `channel_prompt_path` | `string` \| `null` | Relative path (relative to `prompts/`) to the channel formatting overlay, taken from `aiguilleur.yaml:channels[*].prompt_path`. `null` when not configured — no channel overlay is loaded by Atelier. Stamped by the adapter so Atelier can load the prompt layer explicitly. |
| `content_type` | `string` | Always `"text"` for plain messages (Discord only) |
| `reply_to` | `string` | Discord channel ID as string (target for response routing) (Discord only) |

---

### `relais:security`

**Direction**: Portail → Sentinelle
**Consumer group**: `sentinelle_group`

Carries validated and enriched envelopes pending ACL and content-security checks. Portail resolves user information via UserRegistry and stamps `context["portail"]`.

```
XADD relais:security * payload <Envelope JSON>
```

**Context keys added by Portail (in `context.portail`)**:

| Key | Type | Description |
|-----|------|-------------|
| `user_id` | `string` | Stable cross-channel user identifier, equal to the YAML key in portail.yaml (e.g. `"usr_admin"`). `"guest"` for unknown users under guest policy. Use this to resume conversations across channels. |
| `user_record` | `dict` | Serialized `UserRecord` with fields: `user_id`, `display_name`, `role`, `blocked`, `actions`, `skills_dirs`, `allowed_mcp_tools`, `prompt_path`, `role_prompt_path`. `prompt_path` = per-user overlay path (user entry only, no role fallback). `role_prompt_path` = role-level overlay path (role entry only). Both are independent and both can be `null`. Does **not** contain `llm_profile`. |
| `llm_profile` | `string` | Resolved LLM profile name: `channel_profile` (Aiguilleur) → `"default"`. Used by Atelier to load the appropriate `ProfileConfig` from `profiles.yaml`. |
| `session_start` | `float` (optional) | Epoch timestamp if this is a new session |

---

### `relais:tasks`

**Direction**: Sentinelle → Atelier
**Consumer group**: `atelier_group`

Carries security-cleared envelopes ready for LLM processing.

```
XADD relais:tasks * payload <Envelope JSON>
```

No additional context keys are added by Sentinelle beyond what Portail set.

---

### `relais:commands`

**Direction**: Sentinelle → Commandant
**Consumer group**: `commandant_group`

Carries slash-command envelopes that have passed both identity ACL (allowlist check) and
command-level ACL (`action=<command_name>` check).  Only known commands (present in
`KNOWN_COMMANDS`) arrive here — unknown commands are rejected inline by Sentinelle before
reaching this stream.

```
XADD relais:commands * payload <Envelope JSON>
```

The envelope content is the raw slash command (e.g. `/clear`).  All context
stamped by Portail (`context["portail"]`: user_record, llm_profile, …) is preserved.

**XACK contract**: Commandant ACKs every message it dequeues, regardless of whether a
handler exists for the command name.  Post-ACL unknown-command filtering at this layer
is intentionally absent — Sentinelle is the sole gatekeeper for command validity.

---

### `relais:tasks:failed` (DLQ)

**Direction**: Atelier → (monitoring / manual review)
**Consumer group**: none (manual consumption)

Dead-Letter Queue for messages that caused non-recoverable errors in Atelier.

```
XADD relais:tasks:failed * payload <original Envelope JSON>
                            reason  <error string>
                            failed_at <Unix epoch float as string>
```

| Field | Type | Description |
|-------|------|-------------|
| `payload` | `string` | Original Envelope JSON that failed |
| `reason` | `string` | Human-readable error message |
| `failed_at` | `string` | Unix epoch float as string |

---

### `relais:messages:outgoing:failed` (DLQ)

**Direction**: Aiguilleur channel adapters → (monitoring / manual review)
**Consumer group**: none (manual consumption)
**Stream name constant**: `common.streams.STREAM_OUTGOING_FAILED`

Dead-Letter Queue for outgoing messages that a channel adapter failed to deliver to the
external platform (e.g. Discord message send exceptions, WhatsApp gateway HTTP errors).
Written by the adapter that owns `relais:messages:outgoing:{channel}`.

```
XADD relais:messages:outgoing:failed * source      "relais:messages:outgoing:discord"
                                       message_id  "<stream entry id>"
                                       payload     "<original Envelope JSON>"
                                       reason      "<error string>"
                                       failed_at   "<Unix epoch float as string>"
```

| Field | Type | Description |
|-------|------|-------------|
| `source` | `string` | Origin outgoing stream (e.g. `relais:messages:outgoing:discord`) |
| `message_id` | `string` | Entry ID of the failed message in its source stream |
| `payload` | `string` | Original Envelope JSON that failed to deliver |
| `reason` | `string` | Human-readable error message |
| `failed_at` | `string` | Unix epoch float as string |

---

### `relais:messages:outgoing_pending`

**Direction**: Atelier → Sentinelle
**Consumer groups**: `sentinelle_outgoing_group`

Carries completed LLM response envelopes waiting for outgoing validation by Sentinelle before delivery.
The destination channel is determined by the `channel` field inside the Envelope payload.

```
XADD relais:messages:outgoing_pending * payload <Envelope JSON>
```

**Context keys added by Atelier (in `context.atelier`)**:

| Key | Type | Description |
|-----|------|-------------|
| `user_message` | `string` | Original user message content (copied from incoming envelope) |
| `messages_raw` | `array` | Full serialized LangChain message list for the turn (via `atelier.message_serializer.serialize_messages()`). Read by Souvenir to archive the complete turn history. |

**Traces updated by Atelier**:
- The `traces` array is appended with entry: `{"brick": "atelier", "action": "Generated via {model}", "timestamp": ...}`

---

### `relais:messages:outgoing:{channel}`

**Direction**: Sentinelle → Aiguilleur (relay)
**Consumer groups**: `{channel}_relay_group`, `souvenir_outgoing_group`

Carries outgoing-validated response envelopes for delivery to the user. Sentinelle reads from
`outgoing_pending`, applies the outgoing rule (currently a pass-through), and republishes here.

```
XADD relais:messages:outgoing:discord * payload <Envelope JSON>
```

**Traces updated by Sentinelle**:
- The `traces` array is appended with entry: `{"brick": "sentinelle", "action": "outgoing pass-through", "timestamp": ...}`

---

### `relais:messages:streaming:{channel}:{correlation_id}`

**Direction**: Atelier (StreamPublisher) → Aiguilleur (streaming relay)
**Consumer group**: none (direct XREAD by Aiguilleur)
**TTL**: 300 seconds after `finalize()` call
**Max entries**: ~500 (APPROX trimming)

Carries incremental LLM text chunks for real-time progressive rendering.
Only produced for channels in `STREAMING_CAPABLE_CHANNELS`: `telegram`, `tui`.

```
XADD relais:messages:streaming:telegram:550e8400-... * chunk "Hello, "
                                                        seq   "0"
                                                        is_final "0"
```

| Field | Type | Description |
|-------|------|-------------|
| `chunk` | `string` | Text fragment (empty string `""` for the final sentinel) |
| `seq` | `string` | Monotonically increasing integer (as string) |
| `is_final` | `string` | `"1"` for the terminal sentinel entry, `"0"` otherwise |

**Reading pattern** (Aiguilleur):
```python
while True:
    results = await redis.xread({stream_key: last_id}, count=10, block=5000)
    for entry_id, fields in results[0][1]:
        last_id = entry_id
        if fields["is_final"] == "1":
            break
```

---

### `relais:memory:request`

**Direction**: Atelier, Commandant → Souvenir, Forgeron
**Consumer groups**: `souvenir_group` (Souvenir), `forgeron_archive_group` (Forgeron)

Carries memory action requests. Two independent consumer groups read this stream:

- **`souvenir_group`** — Souvenir brick persists turns and handles memory actions (archive, clear, file_*)
- **`forgeron_archive_group`** — Forgeron brick reads `archive` actions only, extracts intent labels via IntentLabeler (Haiku LLM), and triggers auto-creation of SKILL.md files when a pattern recurs (auto-creation pipeline)

Three producers:
- **Atelier** (`atelier/main.py`) — `archive` after each completed agent turn (fire-and-forget, no response expected)
- **SouvenirBackend** (`atelier/souvenir_backend.py`) — `file_write`, `file_read`, `file_list` called by the agent's file tools (synchronous request-response via `relais:memory:response`)
- **Commandant** — `clear` to erase a session (synchronous request-response)

> **Fan-out**: Both consumer groups are fully independent. Forgeron's ACK on `forgeron_archive_group` does not affect Souvenir's PEL in `souvenir_group`, and vice versa.

```
XADD relais:memory:request * payload <Envelope JSON>
```

The payload is a serialized `Envelope` (`Envelope.to_json()`). The `action` field and action-specific data are carried in the envelope's `metadata`.

**`archive` action** (published by Atelier):

```json
{
  "action": "archive",
  "envelope_json": "<Envelope.to_json() of the outgoing response>",
  "messages_raw": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `action` | `string` | `"archive"` |
| `envelope_json` | `string` | Serialized outgoing response `Envelope` |
| `messages_raw` | `array` | Full serialized LangChain message list for the completed turn |

**`clear` action** (published by Commandant) / **`file_*` actions** (published by SouvenirBackend):

```json
{
  "action": "clear",
  "session_id": "sess-abc",
  "user_id": "usr_admin",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "envelope_json": "..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `action` | `string` | One of `"clear"`, `"file_write"`, `"file_read"`, `"file_list"` |
| `session_id` | `string` | Session identifier (used by `clear`) |
| `user_id` | `string` | Stable user identifier — used by `clear` to erase the LangGraph checkpointer thread |
| `correlation_id` | `string` | UUID for tracing |
| `envelope_json` | `string` (optional) | Serialized original envelope; used by `clear` to send a confirmation reply |

---

### `relais:skill:trace`

**Direction**: Atelier → Forgeron
**Consumer group**: `forgeron_group`
**XACK contract**: `ack_mode="always"` — traces are advisory; losing one is acceptable.

Published by Atelier after each completed agent turn where skills were used **and** `tool_call_count > 0`.  Turns where skills are loaded but no tool was invoked are not published (avoids polluting the trace stream with skill-less turns).

```
XADD relais:skill:trace * payload <Envelope JSON>
```

**Context keys set by Atelier (in `context.skill_trace` / `CTX_SKILL_TRACE`)**:

| Key | Type | Description |
|-----|------|-------------|
| `skill_names` | `list[str]` | Directory names of skills used in the turn (e.g. `["mail-agent"]`) |
| `tool_call_count` | `int` | Total tool invocations in the agent turn |
| `tool_error_count` | `int` | Tool invocations that returned an error |
| `messages_raw` | `list[dict]` | Serialized LangChain message list for the turn (for LLM analysis by Forgeron) |

---

### `relais:events:system`

**Direction**: Forgeron → Archiviste (and any other listeners)
**Consumer group**: `archiviste_group`

System lifecycle events published by Forgeron when a skill patch is applied, rolled back, or a new skill is created.

```
XADD relais:events:system * payload <Envelope JSON>
```

**Actions published by Forgeron**:

| `action` | Description |
|----------|-------------|
| `skill_patch_applied` | A new SKILL.md has been atomically applied for a skill (trace analysis pipeline) |
| `skill_patch_rolled_back` | A previously applied patch has been reverted due to regression (trace analysis pipeline) |
| `skill.created` | A new SKILL.md has been auto-generated from recurring session patterns (auto-creation pipeline) |

**Context keys set by Forgeron (in `context.forgeron` / `CTX_FORGERON`)**:

For `skill_patch_applied`:

| Key | Type | Description |
|-----|------|-------------|
| `skill_name` | `string` | Directory name of the patched skill |
| `patch_id` | `string` | UUID of the `SkillPatch` record in SQLite |
| `pre_error_rate` | `float` | Error rate that triggered the analysis (0.0–1.0) |
| `diff_preview` | `string` | First 500 chars of the unified diff |

For `skill_patch_rolled_back`:

| Key | Type | Description |
|-----|------|-------------|
| `skill_name` | `string` | Directory name of the rolled-back skill |
| `patch_id` | `string` | UUID of the `SkillPatch` record that was reverted |

For `skill.created`:

| Key | Type | Description |
|-----|------|-------------|
| `skill_created` | `bool` | Always `true` |
| `skill_path` | `string` | Absolute path to the written SKILL.md file |
| `intent_label` | `string` | Normalized intent label that triggered creation (e.g. `"send_email"`) |
| `contributing_sessions` | `int` | Number of sessions that matched this intent label |

> **Note**: Forgeron constructs a minimal `Envelope` (only `sender_id`, `channel`, `session_id`, `correlation_id` from the triggering trace) — it does NOT use `Envelope.from_parent()` in order to avoid leaking upstream context (portail user records, atelier state) into system events.

---

### `relais:logs`

**Direction**: All bricks → Archiviste
**Consumer group**: `archiviste_group`

Structured log stream for cross-brick observability.

```
XADD relais:logs * level          "INFO"
                   brick          "atelier"
                   correlation_id "550e8400-..."
                   sender_id      "discord:123"
                   message        "Answered corr-id via relais:messages:outgoing_pending:discord"
                   content_preview "Hello, here is ..."
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `level` | `string` | Yes | `INFO`, `WARNING`, `ERROR` |
| `brick` | `string` | Yes | Name of the emitting brick |
| `correlation_id` | `string` | No | Request correlation ID if available |
| `sender_id` | `string` | No | Originating user ID |
| `message` | `string` | Yes | Human-readable log message |
| `content_preview` | `string` | No | First 60 chars of content (atelier only) |
| `error` | `string` | No | Error details (ERROR level only) |

---

### `relais:admin:pending_users`

**Direction**: Sentinelle (ACL) → Admin tools
**Consumer group**: none (manual consumption)

Published when an unknown user sends a message and the ACL policy is `pending`.

```
XADD relais:admin:pending_users * payload <PendingUserNotification JSON>
```

**PendingUserNotification schema**:

```json
{
  "user_id": "discord:123456789",
  "channel": "discord",
  "timestamp": "1711234567.890",
  "policy": "pending"
}
```

---

### `relais:active_sessions:{sender_id}` (Hash, not Stream)

**Type**: Redis Hash
**TTL**: 3600 seconds (1 hour)
**Direction**: Portail writes, Le Crieur reads

Tracks active user sessions per channel for push-notification routing.

```
HSET relais:active_sessions:discord:123456789 discord 1711234567.890
EXPIRE relais:active_sessions:discord:123456789 3600
```

Hash fields: `{channel_name}` → `{Unix epoch float as string}`

---

### `relais:whatsapp:pairing` (String, not Stream)

**Type**: Redis String (JSON-encoded)
**TTL**: 300 seconds
**Direction**: `whatsapp_configure` tool / `python -m aiguilleur.channels.whatsapp configure --action pair` writes, WhatsApp adapter / operator reads
**Key name constant**: `common.streams.KEY_WHATSAPP_PAIRING`

Holds the context of an active WhatsApp QR pairing flow so the operator running
`python -m aiguilleur.channels.whatsapp configure --action pair` (or the `whatsapp_configure` LangChain tool)
can correlate QR display with gateway state. Cleared
by `python -m aiguilleur.channels.whatsapp configure --action unpair` (or `whatsapp_configure(action="unpair")`) or when the TTL expires.

```
SET relais:whatsapp:pairing '{"phone":"+33612345678","started_at":1711234567.890,"correlation_id":"..."}' EX 300
```

The exact JSON schema is owned by `aiguilleur/channels/whatsapp/core.py` — see the module
for the current field set.

---

## Pub/Sub Channels

### `relais:streaming:start:{channel}`

**Direction**: Atelier → Aiguilleur (streaming relay)
**Primitive**: Redis Pub/Sub (`PUBLISH` / `SUBSCRIBE`)

Signals the start of a streaming session. The subscriber spawns a task to read from the corresponding `relais:messages:streaming:{channel}:{correlation_id}` stream.

```
PUBLISH relais:streaming:start:telegram <Envelope JSON>
```

**Payload**: Full **Envelope JSON** (same schema as all pipeline streams).

> **Important**: The payload MUST be a complete JSON-serialized Envelope, not a bare UUID or correlation_id string.
> The subscriber performs `json.loads(payload)` and reconstructs the Envelope to extract `correlation_id` and `context.aiguilleur.reply_to`.

**Why the full Envelope?** The subscriber needs both `correlation_id` (to identify the stream key) and `context.aiguilleur.reply_to` (to find the target channel).

---

### `relais:events:{event_type}`

**Direction**: Any brick → Monitoring tools (Le Scrutateur, dashboards)
**Primitive**: Redis Pub/Sub

Fire-and-forget monitoring events. Published via `EventPublisher.emit()`.

```
PUBLISH relais:events:task_received <EventPayload JSON>
```

**EventPayload schema**:

```json
{
  "event_type": "task_received",
  "timestamp": 1711234567.890,
  "session_id": "sess-abc",
  "brick": "atelier"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event_type` | `string` | Event category (e.g. `task_received`, `llm_error`, `session_started`) |
| `timestamp` | `float` | Automatically injected Unix epoch |
| `*` | any | Arbitrary JSON-serializable fields passed by the emitter |

---

## Stream Consumer Groups Summary

| Stream | Consumer group(s) | Brick |
|--------|------------------|-------|
| `relais:messages:incoming` | `portail_group` | Portail |
| `relais:security` | `sentinelle_group` | Sentinelle |
| `relais:tasks` | `atelier_group` | Atelier |
| `relais:commands` | `commandant_group` | Commandant |
| `relais:messages:outgoing_pending` | `sentinelle_outgoing_group` | Sentinelle |
| `relais:messages:outgoing:{channel}` | `{channel}_relay_group` | Aiguilleur |
| `relais:messages:outgoing:failed` | none (manual) | — (DLQ) |
| `relais:memory:request` | `souvenir_group` | Souvenir |
| `relais:memory:request` | `forgeron_archive_group` | Forgeron (archive action only — intent labeling + skill creation) |
| `relais:skill:trace` | `forgeron_group` | Forgeron |
| `relais:logs` | `archiviste_group` | Archiviste |
| `relais:events:system` | `archiviste_group` | Archiviste |
| `relais:events:messages` | `archiviste_group` | Archiviste |

---

## XACK Contract

All consumer bricks follow this acknowledgement pattern:

| Return value | Meaning | XACK? |
|-------------|---------|-------|
| `True` | Success OR non-recoverable error (routed to DLQ) | **Yes** |
| `False` | Transient error — message stays in PEL for re-delivery | **No** |

---

## Adding a New Message Type

1. Choose the right primitive: **Stream** for durable pipeline messages, **Pub/Sub** for ephemeral signals.
2. Pick a key following the naming convention: `relais:{category}:{subcategory}`.
3. Define the JSON schema in this document **before** writing any brick code.
4. For Streams: define the consumer group name and ACL permissions in `config/redis.conf`.
5. Write a test that verifies the exact payload structure published by the producer.
