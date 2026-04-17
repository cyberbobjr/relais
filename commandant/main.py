"""Commandant brick — interprets global out-of-LLM commands.

Consumes ``relais:commands`` (routed by Sentinelle) via its own consumer group.
Dispatches known commands to their handlers and ACKs all messages.

Technical overview
------------------
* ``Commandant`` — ``BrickBase`` subclass; single consumer loop on
  ``relais:commands``.
* Command dispatch via ``COMMAND_REGISTRY`` (see ``commandant/commands.py``).

Redis channels
--------------
Consumed:
  - relais:commands  (consumer group: commandant_group)

Produced (by handlers):
  - relais:memory:request      — /clear delegates to Souvenir
  - relais:messages:outgoing:* — /help replies directly
    (the response envelope is stamped with
    ``ACTION_MESSAGE_OUTGOING`` before publication; ``Envelope.to_json()``
    now raises if ``action`` is unset)

XACK contract
-------------
  - ``ack_mode="always"`` — all messages are ACKed unconditionally.

Channel install/pair commands
-----------------------------
Channel installation, configuration and pairing (including the WhatsApp QR
flow previously driven by ``/settings whatsapp``) are no longer Commandant
responsibilities.  They are handled end-to-end by the ``relais-config``
subagent via the ``channel-setup`` and ``whatsapp`` skills in Atelier —
users ask the agent in natural language.
"""

import asyncio
from typing import Any

from commandant.commands import COMMAND_REGISTRY, parse_command
from common.brick_base import BrickBase, StreamSpec
from common.envelope import Envelope
from common.shutdown import GracefulShutdown  # noqa: F401 — test patch target
from common.streams import STREAM_COMMANDS


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
        """Return the single stream spec for command consumption.

        Returns:
            A list containing one StreamSpec for relais:commands.
        """
        return [
            StreamSpec(
                stream=STREAM_COMMANDS,
                group="commandant_group",
                consumer="commandant_1",
                handler=self._handle,
                ack_mode="always",
            ),
        ]

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
