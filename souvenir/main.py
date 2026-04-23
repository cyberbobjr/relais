"""Souvenir brick — long-term memory service.

Functional role
---------------
Archives conversational turns and handles agent memory file operations.
Archival is triggered by Atelier via an ``archive`` action on
``relais:memory:request`` — the full message history no longer transits
through the outgoing envelope stream.

Technical overview
------------------
``Souvenir`` extends :class:`~common.brick_base.BrickBase` and runs a single
asyncio consumer loop on ``relais:memory:request``.

* ``archive`` — persists the completed agent turn to SQLite (sent by Atelier
  after each successful LLM call).
* ``clear`` / ``file_write`` / ``file_read`` / ``file_list`` — memory file
  operations triggered by agents via ``SouvenirBackend``.

Key classes:

* ``LongTermStore`` — SQLite ``~/.relais/storage/memory.db``; stores full
  message history as one row per turn (upsert on ``correlation_id``).
* ``FileStore`` — SQLite ``~/.relais/storage/memory.db`` (table
  ``memory_files``); stores persistent agent memory files routed by
  ``SouvenirBackend`` in Atelier.
* ``HandlerContext`` + action registry — extensible dispatch pattern for
  request actions.

Redis channels
--------------
Consumed:
  - relais:memory:request   (consumer group: souvenir_group)
  - relais:config:reload:souvenir  (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:memory:response  — responses to file_read / file_list / clear
  - relais:logs             — operational log entries

Configuration hot-reload
------------------------
Souvenir watches souvenir configuration for changes and reloads without
restarting:

* Watched files: souvenir/profiles.yaml (memory extractor model config)
* Reload trigger: File system change detected via watchfiles library
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous config is preserved
* Redis Pub/Sub channel: relais:config:reload:souvenir (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

Processing flow
---------------
  (1) Consume from relais:memory:request (souvenir_group).
  (2) Parse Envelope from payload field.
  (3) Extract action from envelope.action and parameters from envelope.context[CTX_SOUVENIR_REQUEST].
  (4) Dispatch to registered handler (archive / clear / file_write / file_read / file_list).
  (5) XACK (ack_mode="always").

Envelope.action contract
------------------------
``Envelope.to_json()`` now raises when ``action`` is unset.  Handlers that
publish downstream envelopes (notably ``ClearHandler``, which emits a
"✓ Conversation history cleared." confirmation on
``relais:messages:outgoing:{channel}``) explicitly stamp
``confirmation.action = ACTION_MESSAGE_OUTGOING`` before calling
``xadd``.
"""

import asyncio
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.contexts import CTX_SOUVENIR_REQUEST, SouvenirRequest
from common.envelope import Envelope
from common.streams import STREAM_MEMORY_REQUEST, STREAM_MEMORY_RESPONSE
from souvenir.file_store import FileStore
from souvenir.handlers import HandlerContext, build_registry
from souvenir.long_term_store import LongTermStore


class Souvenir(BrickBase):
    """Memory brick: long-term archival (SQLite/SQLModel) and agent memory files.

    Consumes ``relais:memory:request`` for the ``archive`` / ``clear`` /
    ``file_write`` / ``file_read`` / ``file_list`` actions.  SQLite archival is
    triggered by Atelier via the ``archive`` action — the history no longer
    transits through outgoing envelopes.

    Inherits the full lifecycle plumbing (connection, shutdown, hot-reload,
    logging) from :class:`~common.brick_base.BrickBase`.
    """

    def __init__(self) -> None:
        """Initialise Redis streams, memory stores, and the action registry."""
        super().__init__("souvenir")
        # Preserve legacy attribute names accessed by tests and other bricks.
        self.stream_req = STREAM_MEMORY_REQUEST
        self.stream_res = STREAM_MEMORY_RESPONSE
        self.group_name = "souvenir_group"
        self.consumer_name = "souvenir_1"
        self._long_term = LongTermStore()
        self._file_store = FileStore()
        self._action_registry = build_registry()

    # ------------------------------------------------------------------
    # BrickBase abstract interface
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load or reload Souvenir configuration from disk.

        No-op: Souvenir has no YAML configuration to reload today.  The method
        exists so that the hot-reload contract is consistent across all bricks
        and can be extended in the future without interface changes.
        """

    def stream_specs(self) -> list[StreamSpec]:
        """Return the single StreamSpec for ``relais:memory:request``.

        Returns:
            A list containing one :class:`~common.brick_base.StreamSpec` that
            describes the ``souvenir_group`` consumer on
            ``relais:memory:request``.  ``ack_mode="always"`` because every
            memory operation is idempotent (re-running an archive or file_write
            at worst creates a harmless duplicate row).
        """
        return [
            StreamSpec(
                stream=self.stream_req,
                group=self.group_name,
                consumer=self.consumer_name,
                handler=self._handle,
                ack_mode="always",
            )
        ]

    # ------------------------------------------------------------------
    # BrickBase lifecycle hooks
    # ------------------------------------------------------------------

    async def on_startup(self, redis: Any) -> None:
        """Initialise the SQLite schema before the stream loop starts.

        Args:
            redis: Live async Redis connection (unused by Souvenir's startup).
        """
        await self._long_term._create_tables()
        await self._file_store._create_tables()

    async def on_shutdown(self) -> None:
        """Release SQLite connections after the stream loop stops."""
        await self._long_term.close()
        await self._file_store.close()

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    async def _handle(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Dispatch one memory request to the appropriate action handler.

        Extracts action parameters from ``envelope.context["souvenir_request"]``
        and builds a flat :class:`~souvenir.handlers.HandlerContext` dict
        (``ctx.req``) for backward-compatible handler access.

        Args:
            envelope: Incoming memory request; action is in ``envelope.action``
                and parameters are in ``envelope.context["souvenir_request"]``.
            redis_conn: Active async Redis connection passed to handlers for
                publishing responses.

        Returns:
            ``True`` always — every memory action is idempotent and the
            message is unconditionally ACKed (``ack_mode="always"``).
        """
        req: SouvenirRequest = dict(envelope.context.get(CTX_SOUVENIR_REQUEST, {}))
        req.setdefault("correlation_id", envelope.correlation_id)
        action = envelope.action or req.get("action")

        ctx = HandlerContext(
            redis_conn=redis_conn,
            long_term_store=self._long_term,
            file_store=self._file_store,
            req=req,
            stream_res=self.stream_res,
        )
        handler = self._action_registry.get(action)
        if handler:
            await handler.handle(ctx)
        else:
            await self.log.error(
                f"Souvenir: rejected unknown action {action!r} from {envelope.correlation_id}",
                correlation_id=envelope.correlation_id,
            )
        return True


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    souvenir = Souvenir()
    try:
        asyncio.run(souvenir.start())
    except KeyboardInterrupt:
        pass
