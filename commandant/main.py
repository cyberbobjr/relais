"""Commandant brick — interprets global out-of-LLM commands.

Consumes ``relais:commands`` (routed by Sentinelle) and ``relais:commandant:query``
(CQRS catalog endpoint used by the REST adapter and TUI auto-completion).
Dispatches known commands to their handlers and ACKs all messages.

Technical overview
------------------
* ``Commandant`` — ``BrickBase`` subclass; two consumer loops:
  - ``relais:commands`` → command dispatch via ``COMMAND_REGISTRY``
  - ``relais:commandant:query`` → catalog query handler (CQRS read side)
* Command dispatch via ``COMMAND_REGISTRY`` (see ``commandant/commands.py``).

Redis channels
--------------
Consumed:
  - relais:commands          (consumer group: commandant_group)
  - relais:commandant:query  (consumer group: commandant_catalog_group)

Produced (by handlers):
  - relais:memory:request           — /clear delegates to Souvenir
  - relais:messages:outgoing:*      — /help replies directly
  - relais:commandant:catalog:{id}  — catalog query response (LPUSH, TTL 7 s)

XACK contract
-------------
  - ``ack_mode="always"`` — all messages on both streams are ACKed unconditionally.

Catalog query (CQRS)
--------------------
The REST adapter issues a query envelope (``action=catalog.query``) to
``relais:commandant:query``.  Commandant responds by LPUSH-ing a JSON payload
to ``relais:commandant:catalog:{correlation_id}`` (TTL 7 s so the REST handler
can BRPOP with a 5 s timeout).  Only ``name`` and ``description`` are exposed;
internal handler callables are omitted.

Channel install/pair commands
-----------------------------
Channel installation, configuration and pairing (including the WhatsApp QR
flow previously driven by ``/settings whatsapp``) are no longer Commandant
responsibilities.  They are handled end-to-end by the ``relais-config``
subagent via the ``channel-setup`` and ``whatsapp`` skills in Atelier —
users ask the agent in natural language.
"""

import asyncio
import json
from typing import Any

from commandant.commands import COMMAND_REGISTRY, parse_command
from common.brick_base import BrickBase, StreamSpec
from common.envelope import Envelope
from common.shutdown import GracefulShutdown  # noqa: F401 — test patch target
from common.streams import STREAM_COMMANDS, STREAM_COMMANDANT_QUERY, key_commandant_catalog


class Commandant(BrickBase):
    """Commandant brick — interprets global out-of-LLM commands.

    Consumes relais:commands in parallel with other bricks via its own
    consumer group (commandant_group). Processes known commands and ACKs
    all messages (commands or otherwise).
    """

    def __init__(self) -> None:
        super().__init__("commandant")
        self._load()

    def _create_shutdown(self) -> GracefulShutdown:
        """Create a GracefulShutdown instance.

        Isolated for test patching.

        Returns:
            A new GracefulShutdown instance.
        """
        return GracefulShutdown()

    def _load(self) -> None:
        """No configuration to load for Commandant."""

    def stream_specs(self) -> list[StreamSpec]:
        """Return stream specs for command consumption and catalog queries.

        Returns:
            A list of two StreamSpecs: relais:commands and relais:commandant:query.
        """
        return [
            StreamSpec(
                stream=STREAM_COMMANDS,
                group="commandant_group",
                consumer="commandant_1",
                handler=self._handle,
                ack_mode="always",
            ),
            StreamSpec(
                stream=STREAM_COMMANDANT_QUERY,
                group="commandant_catalog_group",
                consumer="commandant_catalog_1",
                handler=self._handle_catalog_query,
                ack_mode="always",
            ),
        ]

    async def _handle_catalog_query(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Respond to a catalog query by publishing the command list to a per-request key.

        Uses LPUSH + EXPIRE (TTL 7 s = BRPOP timeout 5 s + 2 s grace) so the caller can retrieve via BRPOP.
        Only ``name`` and ``description`` are exposed — the handler callable is omitted.

        Args:
            envelope: Incoming query envelope; only correlation_id is used.
            redis_conn: Active async Redis connection.

        Returns:
            Always True (ACK unconditionally).
        """
        catalog = [
            {"name": spec.name, "description": spec.description}
            for spec in sorted(COMMAND_REGISTRY.values(), key=lambda s: s.name)
        ]
        response_key = key_commandant_catalog(envelope.correlation_id)
        await redis_conn.lpush(response_key, json.dumps({"commands": catalog}))
        await redis_conn.expire(response_key, 7)
        await self.log.info(
            f"Catalog query answered corr={envelope.correlation_id[:8]}, "
            f"{len(catalog)} commands",
            envelope.correlation_id,
        )
        return True

    async def _handle(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Dispatch a command from the envelope if recognised.

        Parses the envelope content for a known command and executes the
        matching handler. Non-command messages are silently ACKed.

        Args:
            envelope: The deserialized envelope from Redis.
            redis_conn: Active async Redis connection.

        Returns:
            Always True (all messages are ACKed).
        """
        result = parse_command(envelope.content)
        if result is not None:
            spec = COMMAND_REGISTRY.get(result.command)
            if spec is not None:
                await spec.handler(envelope, redis_conn)
                await self.log.info(
                    f"Executed command /{result.command} for sender={envelope.sender_id}",
                    envelope.correlation_id,
                )
        return True


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    commandant = Commandant()
    try:
        asyncio.run(commandant.start())
    except KeyboardInterrupt:
        pass
