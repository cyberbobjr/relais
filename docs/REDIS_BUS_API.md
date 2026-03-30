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
  "metadata": {
    "reply_to": "999888777",
    "traces": [
      {"brick": "portail", "action": "validated", "timestamp": 1711234567.900}
    ]
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
| `metadata` | `object` | Extensible map; channel-specific keys documented per-stream below |
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
**Consumer group**: `portail_group`

Carries raw inbound messages from external channel adapters before any validation.

```
XADD relais:messages:incoming * payload <Envelope JSON>
```

**Metadata keys set by Aiguilleur (Discord)**:

| Key | Type | Description |
|-----|------|-------------|
| `content_type` | `string` | Always `"text"` for plain messages |
| `reply_to` | `string` | Discord channel ID as string (target for response routing) |

---

### `relais:security`

**Direction**: Portail → Sentinelle
**Consumer group**: `sentinelle_group`

Carries validated envelopes pending ACL and content-security checks.

```
XADD relais:security * payload <Envelope JSON>
```

**Additional metadata added by Portail**:

| Key | Type | Description |
|-----|------|-------------|
| `session_start` | `float` (optional) | Epoch timestamp if this is a new session |

---

### `relais:tasks`

**Direction**: Sentinelle → Atelier
**Consumer group**: `atelier_group`

Carries security-cleared envelopes ready for LLM processing.

```
XADD relais:tasks * payload <Envelope JSON>
```

No additional metadata is added by Sentinelle beyond what Portail set.

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

### `relais:messages:outgoing:{channel}`

**Direction**: Atelier → Aiguilleur (relay)
**Consumer groups**: `{channel}_relay_group`, `souvenir_outgoing_group`

Carries completed LLM response envelopes for delivery to the user.

```
XADD relais:messages:outgoing:discord * payload <Envelope JSON>
```

**Metadata added by Atelier**:

| Key | Type | Description |
|-----|------|-------------|
| `user_message` | `string` | Original user message content (copied from incoming envelope) |
| `traces` | `array` | Pipeline trace list appended with `{"brick": "atelier", "action": "Generated via {model}"}` |
| `streamed` | `boolean` (optional) | `True` when the response was already progressively rendered via streaming. Signals Aiguilleur to edit the existing Discord message instead of sending a new one. Only present for channels in `STREAMING_CAPABLE_CHANNELS`. |

**Streaming deduplication flow (Discord)**:

When `metadata["streamed"]` is `True`, Aiguilleur Discord performs an edit instead of a new send:

1. At streaming start, `_handle_streaming_message` stores the Discord placeholder message ID in Redis:
   `SETEX relais:streamed_msg:{correlation_id} 300 {discord_message_id}`
2. `consume_outgoing_stream` reads `metadata["streamed"]`, fetches the key from Redis.
3. If found: edits the existing message via `channel.get_partial_message(id).edit(content=...)` and deletes the key.
4. If key is missing (TTL expired): falls back to `channel.send()`.

---

### `relais:streamed_msg:{correlation_id}` (String, not Stream)

**Type**: Redis String
**TTL**: 300 seconds
**Direction**: Aiguilleur Discord writes, Aiguilleur Discord reads
**Set at**: Placeholder `▌` send time (start of streaming session)
**Consumed at**: Final envelope processing in `consume_outgoing_stream`

Stores the Discord message ID of the streaming placeholder so `consume_outgoing_stream` can edit it instead of sending a duplicate message.

```
SETEX relais:streamed_msg:550e8400-e29b-41d4-a716-446655440000 300 "1234567890123456789"
```

| Field | Type | Description |
|-------|------|-------------|
| key | `string` | `relais:streamed_msg:{correlation_id}` |
| value | `string` | Discord message ID (integer as string) |
| TTL | `int` | 300 seconds — covers the full streaming + final publish round-trip |

---

### `relais:messages:streaming:{channel}:{correlation_id}`

**Direction**: Atelier (StreamPublisher) → Aiguilleur (streaming relay)
**Consumer group**: none (direct XREAD by Aiguilleur)
**TTL**: 300 seconds after `finalize()` call
**Max entries**: ~500 (APPROX trimming)

Carries incremental LLM text chunks for real-time progressive rendering.
Only produced for channels in `STREAMING_CAPABLE_CHANNELS`: `discord`, `telegram`, `tui`.

```
XADD relais:messages:streaming:discord:550e8400-... * chunk "Hello, "
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

**Direction**: Atelier → Souvenir
**Consumer group**: `souvenir_group`

Carries memory retrieval requests from Atelier before each LLM call.

```
XADD relais:memory:request * payload <MemoryRequest JSON>
```

**MemoryRequest schema**:

```json
{
  "action": "get",
  "session_id": "sess-abc",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `action` | `string` | Always `"get"` (future: `"set"`, `"delete"`) |
| `session_id` | `string` | Session identifier to retrieve context for |
| `correlation_id` | `string` | UUID for matching the response |

---

### `relais:memory:response`

**Direction**: Souvenir → Atelier
**Consumer group**: none (direct XREAD by Atelier with correlation_id matching)

Carries the conversation history returned by Souvenir.

```
XADD relais:memory:response * payload <MemoryResponse JSON>
```

**MemoryResponse schema**:

```json
{
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "history": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `correlation_id` | `string` | Must match the `correlation_id` from the corresponding request |
| `history` | `array` | Ordered list of `{role, content}` dicts (role: `"user"` or `"assistant"`) |

**Timeout**: Atelier waits at most **3 seconds** for the matching response before falling back to an empty context.

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
                   message        "Answered corr-id via relais:messages:outgoing:discord"
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

### `relais:context:{session_id}` (List, not Stream)

**Type**: Redis List
**TTL**: 86400 seconds (24 hours)
**Max entries**: 20 (LPUSH + LTRIM)
**Direction**: Souvenir writes, Souvenir reads

Short-term conversation context cache (role/content pairs as JSON strings).

```
LPUSH relais:context:sess-abc '{"role": "user", "content": "Hello"}'
```

Each list element is a JSON string:
```json
{"role": "user", "content": "Hello"}
```

---

## Pub/Sub Channels

### `relais:streaming:start:{channel}`

**Direction**: Atelier → Aiguilleur (streaming relay)
**Primitive**: Redis Pub/Sub (`PUBLISH` / `SUBSCRIBE`)

Signals the start of a streaming session. The subscriber spawns a task to read from the corresponding `relais:messages:streaming:{channel}:{correlation_id}` stream.

```
PUBLISH relais:streaming:start:discord <Envelope JSON>
```

**Payload**: Full **Envelope JSON** (same schema as all pipeline streams).

> **Important**: The payload MUST be a complete JSON-serialized Envelope, not a bare UUID or correlation_id string.
> The subscriber performs `json.loads(payload)` and reconstructs the Envelope to extract `correlation_id` and `metadata.reply_to`.

**Why the full Envelope?** The subscriber needs both `correlation_id` (to identify the stream key) and `metadata.reply_to` or `metadata.discord_channel_id` (to find the Discord channel).

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
| `relais:messages:outgoing:{channel}` | `{channel}_relay_group`, `souvenir_outgoing_group` | Aiguilleur, Souvenir |
| `relais:memory:request` | `souvenir_group` | Souvenir |
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
