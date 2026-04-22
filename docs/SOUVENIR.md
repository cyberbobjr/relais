# Souvenir — Memory Persistence Brick

## Functional Overview

Souvenir is the memory service of the RELAIS pipeline. It manages all forms of persistent user data: conversation history (short-term via Redis and long-term via SQLite), arbitrary memory files (key-value blobs scoped to a user), and session lifecycle operations such as listing, resuming, and clearing sessions.

Souvenir never calls an LLM. It is a pure storage backend: it receives structured action envelopes, executes the corresponding database operation, and (when the action requires a response) publishes the result to a dedicated ephemeral Redis List from which the requesting brick can BRPOP.

---

## Architecture Position in the Pipeline

```
Atelier      ──► relais:memory:request ──► Souvenir (souvenir_group)
Forgeron     ──► relais:memory:request ──► Souvenir (forgeron_archive_group)
Commandant   ──► relais:memory:request ──► Souvenir (souvenir_group)
                                               │
                                               ├── SQLite: archived_messages, memory_files
                                               ├── Redis List: relais:context:{session_id}  (short-term)
                                               └── Redis List: relais:memory:response:{corr_id}  (BRPOP replies)
```

All producers publish to the same `relais:memory:request` stream. Souvenir has a single consumer group (`souvenir_group`) for most actions. Forgeron additionally subscribes with its own group (`forgeron_archive_group`) to receive the same archive envelopes — the two groups are independent and do not interfere.

Souvenir uses `ack_mode="always"`: a storage failure must never block the pipeline. Missing one archive turn is acceptable; poisoning the PEL is not.

---

## Action Dispatcher

Every envelope carries an `action` field. Souvenir maintains an `_action_registry` dict mapping action constants to `BaseActionHandler` subclasses. The dispatch loop calls `handler.handle(envelope, redis_conn)` for each incoming message.

Registered actions:

| Action constant | Handler class | Description |
|---|---|---|
| `ACTION_MEMORY_ARCHIVE` | `ArchiveHandler` | Persist a completed Atelier turn to SQLite + Redis short-term list |
| `ACTION_MEMORY_CLEAR` | `ClearHandler` | Drop short-term Redis context key and SQLite rows for a session |
| `ACTION_MEMORY_FILE_WRITE` | `FileWriteHandler` | Upsert a named memory file in SQLite |
| `ACTION_MEMORY_FILE_READ` | `FileReadHandler` | Fetch a named memory file and publish to response List |
| `ACTION_MEMORY_FILE_LIST` | `FileListHandler` | List all memory files for a user, publish to response List |
| `ACTION_MEMORY_HISTORY_READ` | `HistoryReadHandler` | Read full session history, truncate to token budget, publish to response List |
| `ACTION_MEMORY_SESSIONS` | `SessionsHandler` | List recent sessions for a user, publish to response List |
| `ACTION_MEMORY_RESUME` | `ResumeHandler` | Restore a session's context into the active Redis short-term key |

---

## Pipeline: Archiving a Turn (ArchiveHandler)

### Trigger

Atelier publishes to `relais:memory:request` with `action=ACTION_MEMORY_ARCHIVE` after every completed agent turn. The envelope carries:

- `messages_raw` — full serialised LangChain message list for the turn (JSON array of dicts)
- `user_content` — text of the user's message (first HumanMessage)
- `assistant_content` — text of the final assistant reply
- `session_id`, `correlation_id`, `sender_id`, `channel`

### Short-Term Storage (Redis)

`ArchiveHandler` prepends the serialised `messages_raw` blob to the Redis List `relais:context:{session_id}`:

```
LPUSH relais:context:{session_id}  <messages_raw JSON>
LTRIM relais:context:{session_id}  0  19   # keep 20 most-recent blobs
EXPIRE relais:context:{session_id} 86400   # 24h TTL
```

Each list element is the full serialised LangChain message list for one turn (not individual messages). Atelier reads this list at the start of each request to reconstruct conversation history.

### Long-Term Storage (SQLite)

`ArchiveHandler` also calls `LongTermStore.archive()`, which upserts one row into `archived_messages` keyed on `correlation_id`. This ensures idempotency on re-delivery.

---

## Pipeline: History Read (HistoryReadHandler)

### Trigger

Forgeron's correction pipeline publishes to `relais:memory:request` with `action=ACTION_MEMORY_HISTORY_READ`. The envelope carries `session_id`, `user_id`, and the `correlation_id` to use as the response channel.

### Token Budget Truncation

`HistoryReadHandler` fetches all archived turns for the session from SQLite (ordered by creation time) and applies `_truncate_to_token_budget()`:

- Approximation: 4 characters ≈ 1 token.
- Budget: configurable, default 16 000 tokens (64 000 characters).
- Strategy: drop the **oldest** turns first until the total character count is within budget.

This prevents context-overflow when a session has accumulated many turns.

### Response Publication

The truncated turn list is serialised as a JSON array and published to the ephemeral Redis List:

```
LPUSH relais:memory:response:{correlation_id}  <json_array>
EXPIRE relais:memory:response:{correlation_id} 60   # 60s TTL
```

The requesting brick (Forgeron) uses `BRPOP relais:memory:response:{correlation_id} <timeout>` to receive the payload. If no response arrives within the timeout, the requesting brick aborts gracefully.

---

## Pipeline: Session Resume (ResumeHandler)

`ResumeHandler` receives the target `session_id` from the envelope, fetches its 20 most-recent turn blobs from SQLite via `LongTermStore.get_session_history()`, and writes them back into the active Redis List `relais:context:{current_session_id}`:

```
DEL  relais:context:{current_session_id}
RPUSH relais:context:{current_session_id}  <turn_0> ... <turn_N>
EXPIRE relais:context:{current_session_id} 86400
```

The current session ID (where Atelier will read the context next) comes from the envelope's `session_id` field — not the target session being resumed.

---

## Pipeline: File Operations (FileWriteHandler, FileReadHandler, FileListHandler)

Memory files are arbitrary text blobs named by the agent (e.g. `todo_list`, `project_notes`). They are stored in SQLite's `memory_files` table, scoped to a `user_id`.

### Write

`FileWriteHandler` calls `FileStore.write_file(user_id, name, content, overwrite=True)`. The upsert is keyed on `(user_id, name)`. If `overwrite=False` and the file exists, the write is silently skipped.

### Read

`FileReadHandler` calls `FileStore.read_file(user_id, name)` and publishes the content to `relais:memory:response:{correlation_id}` (same BRPOP pattern as history reads). Missing files return `None`.

### List

`FileListHandler` calls `FileStore.list_files(user_id)` and publishes the list of `(name, updated_at)` tuples as JSON to the response List.

---

## Data Model (SQLite — `~/.relais/storage/memory.db`)

### `archived_messages` table

One row per completed Atelier turn.

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `correlation_id` | `str` | Correlation ID of the Atelier turn (unique — upsert key) |
| `session_id` | `str` | Session ID grouping turns into a conversation |
| `sender_id` | `str` | Origin sender_id (e.g. `discord:123`) |
| `channel` | `str` | Origin channel (e.g. `discord`, `telegram`) |
| `user_content` | `str` | Text of the user's message |
| `assistant_content` | `str` | Text of the final assistant reply |
| `messages_raw` | `str` | JSON blob — full LangChain message list for the turn |
| `created_at` | `float` | Unix timestamp |

### `memory_files` table

One row per named memory file per user.

| Column | Type | Description |
|---|---|---|
| `id` | `str` (UUID) | Primary key |
| `user_id` | `str` | User identifier (from portail.yaml, e.g. `usr_admin`) |
| `name` | `str` | File name (agent-chosen, e.g. `todo_list`) |
| `content` | `str` | File content (arbitrary text) |
| `created_at` | `float` | Creation Unix timestamp |
| `updated_at` | `float` | Last-modified Unix timestamp |

---

## Redis Keys

| Key | TTL | Purpose |
|---|---|---|
| `relais:context:{session_id}` | 86400 s (24 h) | Short-term conversation context — Redis List of turn blobs (max 20) |
| `relais:memory:response:{corr_id}` | 60 s | Ephemeral BRPOP channel for HistoryReadHandler, FileReadHandler, FileListHandler, SessionsHandler replies |

---

## Configuration Reference

Souvenir has no dedicated YAML configuration file. Storage paths are resolved via the common config cascade:

| Parameter | Default | Description |
|---|---|---|
| `db_path` | `~/.relais/storage/memory.db` | SQLite database file path |
| `max_short_term_turns` | `20` | Maximum number of turn blobs retained in the Redis List |
| `short_term_ttl_seconds` | `86400` | TTL for the short-term Redis context key (24 h) |
| `history_token_budget` | `16000` | Approximate token budget for `HistoryReadHandler` truncation |
| `response_list_ttl_seconds` | `60` | TTL for ephemeral `relais:memory:response:*` keys |

---

## Key Classes

| Class | File | Responsibility |
|---|---|---|
| `Souvenir` | `souvenir/main.py` | `BrickBase` subclass; owns the consumer loop and dispatches to `_action_registry` |
| `BaseActionHandler` | `souvenir/handlers/base.py` | Abstract base class with `handle(envelope, redis_conn)` interface |
| `ArchiveHandler` | `souvenir/handlers/archive.py` | Persists a completed turn to SQLite + Redis short-term list |
| `ClearHandler` | `souvenir/handlers/clear.py` | Drops short-term Redis key and SQLite rows for a session |
| `HistoryReadHandler` | `souvenir/handlers/history_read.py` | Fetches session history, applies token-budget truncation, publishes to response List |
| `ResumeHandler` | `souvenir/handlers/resume.py` | Restores a past session's turns into the current session's Redis context key |
| `FileWriteHandler` | `souvenir/handlers/file_write.py` | Upserts a named memory file in SQLite |
| `FileReadHandler` | `souvenir/handlers/file_read.py` | Reads a named memory file and publishes to response List |
| `FileListHandler` | `souvenir/handlers/file_list.py` | Lists all memory files for a user, publishes to response List |
| `SessionsHandler` | `souvenir/handlers/sessions.py` | Lists recent sessions for a user, publishes to response List |
| `LongTermStore` | `souvenir/long_term_store.py` | Async SQLite wrapper for `archived_messages` (archive, clear, list_sessions, get_session_history, get_full_session_messages_raw) |
| `FileStore` | `souvenir/file_store.py` | Async SQLite wrapper for `memory_files` (write_file, read_file, list_files) |
| `ArchivedMessage` | `souvenir/models.py` | SQLModel ORM class for the `archived_messages` table |
| `MemoryFile` | `souvenir/models.py` | SQLModel ORM class for the `memory_files` table |

---

## Important Design Decisions

### Why `ack_mode="always"`?

Souvenir is a write-through store. The most critical write (short-term Redis context) happens first, so even if the SQLite long-term write fails later, Atelier can still reconstruct history for the current session. Leaving failed archive envelopes in the PEL would re-trigger the SQLite write endlessly with no benefit — the duplicate check on `correlation_id` means re-delivery is safe, but re-queuing on failure would stall other actions.

### Why two storage tiers?

- **Redis short-term (LPUSH List, 20 blobs, 24h TTL)**: Atelier reads this on every turn to build the LangGraph checkpointer state. Access must be sub-millisecond. Redis Lists provide O(1) prepend and O(N) range retrieval.
- **SQLite long-term**: Provides durable history for session resume, Forgeron correction pipeline, and audit. SQLite is appropriate here: Souvenir processes one message at a time (single consumer) and does not require concurrent writers.

### Why store entire `messages_raw` blobs, not individual messages?

LangChain serialises message lists with type metadata (`AIMessage`, `HumanMessage`, `ToolMessage`, `ToolCall`, etc.) that is not flat-representable in a simple schema. Storing the entire JSON blob per turn preserves round-trip fidelity without requiring Souvenir to understand LangChain internals.

### Why BRPOP for responses instead of a callback stream?

The requesting brick already holds the `correlation_id` and knows where to BRPOP. Using a dedicated Redis List per `correlation_id` avoids fan-out routing logic: Souvenir simply publishes to a well-known key, and the requester blocks until the payload arrives (or its timeout fires). The 60-second TTL prevents orphaned keys from accumulating if the requester crashes before reading.

### Why is token truncation oldest-first?

The most recent turns are most relevant to the current correction or context request. Dropping the oldest turns first preserves the agent's most recent reasoning while fitting within the LLM's context window. An alternative (e.g. summarise old turns) would require an LLM call inside Souvenir, violating its no-LLM constraint.
