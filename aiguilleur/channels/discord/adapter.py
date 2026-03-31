"""Discord channel adapter — NativeAiguilleur implementation.

Bridges the Discord API and the RELAIS Redis bus:
- Produces:   relais:messages:incoming         (new user messages)
- Consumes:   relais:messages:outgoing:discord (bot replies)

Note: streaming progressif désactivé sur ce canal — Atelier publie la réponse
complète sur relais:messages:outgoing:discord et le bot l'envoie en un seul
message.
"""

from __future__ import annotations

import asyncio
import logging
import os

import certifi

# Fix for macOS SSL certificate verify failed
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import discord

from common.redis_client import RedisClient
from common.envelope import Envelope
from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.native import NativeAiguilleur

logger = logging.getLogger("aiguilleur.discord")


class DiscordAiguilleur(NativeAiguilleur):
    """Discord channel adapter.

    Wraps ``_RelaisDiscordClient`` in a NativeAiguilleur lifecycle.
    The Discord client runs inside the adapter thread's event loop.
    """

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)

    async def run(self) -> None:
        """Start the Discord client and block until stop_event is set."""
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token or token == "dummy":
            logger.error(
                "DISCORD_BOT_TOKEN is not set or is 'dummy' — Discord adapter will not start."
            )
            return

        client = _RelaisDiscordClient(stop_event=self.stop_event)
        try:
            await client.start(token)
        except asyncio.CancelledError:
            pass
        finally:
            if not client.is_closed():
                await client.close()


# ---------------------------------------------------------------------------
# Internal Discord client
# ---------------------------------------------------------------------------


class _RelaisDiscordClient(discord.Client):
    """Internal Discord client — not exposed outside this module.

    Manages two concerns:
    - Receiving Discord messages and publishing them to ``relais:messages:incoming``.
    - Consuming ``relais:messages:outgoing:discord`` and sending the final reply.

    Streaming is intentionally disabled on this adapter: responses are sent as
    a single message once Atelier finishes. Set ``streaming: false`` in
    ``channels.yaml`` to prevent Atelier from publishing partial chunks.
    """

    def __init__(self, stop_event: asyncio.Event | None = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._redis_client = RedisClient("aiguilleur")
        self.stream_in = "relais:messages:incoming"
        self.stream_out = "relais:messages:outgoing:discord"
        self.group_name = "discord_relay_group"
        self.consumer_name = f"discord_{os.getpid()}"
        self._redis_conn = None
        # threading.Event or asyncio.Event — we only call is_set()
        self._stop_event = stop_event

    async def setup_hook(self) -> None:
        """Initialise the Redis connection and launch background tasks.

        Called by discord.py once the client is ready to connect.
        Creates the Redis connection, logs the startup event, and launches
        the outgoing-stream consumer task.
        """
        self._redis_conn = await self._redis_client.get_connection()
        await self._redis_conn.xadd(
            "relais:logs",
            {
                "level": "INFO",
                "brick": "aiguilleur-discord",
                "message": "Starting Discord API connection",
            },
        )
        self.loop.create_task(self._consume_outgoing_stream())

    async def on_ready(self) -> None:
        """Log successful Discord login."""
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming Discord messages and publish them to the Redis bus.

        Only processes messages that mention the bot or are sent in a DM.
        Publishes an ``Envelope`` to ``relais:messages:incoming``.

        Args:
            message: The incoming Discord message event.
        """
        if message.author.id == self.user.id:
            return

        bot_mentioned = self.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)

        if not (bot_mentioned or is_dm):
            return

        content = message.content.replace(f"<@{self.user.id}>", "").strip()
        if not content:
            content = "Coucou!"

        preview = content[:80] + "…" if len(content) > 80 else content
        logger.debug(
            "RECV discord | author=%s | channel=%s | content=%r",
            message.author.name,
            message.channel,
            preview,
        )

        envelope = Envelope(
            channel="discord",
            sender_id=f"discord:{message.author.id}",
            content=content,
            session_id=str(message.channel.id),
            metadata={
                "content_type": "text",
                "reply_to": str(message.channel.id),
            },
        )

        try:
            await self._redis_conn.xadd(self.stream_in, {"payload": envelope.to_json()})
            logger.info("Queued message from %s", message.author.name)
        except Exception as exc:
            logger.error("Failed to queue message: %s", exc)

    # ------------------------------------------------------------------
    # Outgoing stream helpers
    # ------------------------------------------------------------------

    async def _ensure_consumer_group(self, stream: str, group: str) -> None:
        """Create a Redis consumer group idempotently.

        Silently ignores the ``BUSYGROUP`` error raised when the group already
        exists. Other errors are logged as warnings.

        Args:
            stream: Redis stream key (e.g. ``relais:messages:outgoing:discord``).
            group: Consumer group name to create.
        """
        try:
            await self._redis_conn.xgroup_create(stream, group, mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

    async def _resolve_discord_channel(
        self, envelope: Envelope
    ) -> discord.abc.Messageable | None:
        """Resolve the Discord channel or DM to send a reply to.

        Tries ``get_channel()`` first (in-process cache), then falls back to
        fetching the user and opening a DM. This fallback is needed when the
        target is a DM channel that isn't cached (e.g. after a bot restart).

        Args:
            envelope: The outgoing message envelope. Must contain ``reply_to``
                (channel ID) in ``metadata`` and a ``sender_id`` of the form
                ``discord:{user_id}``.

        Returns:
            A Discord messageable (``TextChannel``, ``DMChannel``) or ``None``
            if resolution fails.
        """
        try:
            channel_id = int(envelope.metadata.get("reply_to", 0))
            channel = self.get_channel(channel_id)
            if channel:
                return channel
            user_id = int(envelope.sender_id.split(":")[1])
            user = await self.fetch_user(user_id)
            return await user.create_dm()
        except Exception as exc:
            logger.error(
                "Cannot resolve Discord channel for envelope %s: %s",
                envelope.correlation_id,
                exc,
            )
            return None

    async def _deliver_outgoing_message(self, data: dict) -> None:
        """Parse and deliver a single outgoing envelope to Discord.

        Deserialises the ``payload`` field, resolves the target channel, then
        sends the message content. Deserialization errors (malformed JSON,
        missing fields) are logged separately from Discord API errors.

        Args:
            data: Raw Redis stream entry fields. Must contain a ``"payload"``
                key with a JSON-serialised ``Envelope``.
        """
        try:
            envelope = Envelope.from_json(data.get("payload", "{}"))
        except (ValueError, KeyError) as exc:
            logger.error("Malformed envelope payload, skipping: %s", exc)
            return

        channel = await self._resolve_discord_channel(envelope)
        if not channel:
            return

        preview = envelope.content[:80] + "…" if len(envelope.content) > 80 else envelope.content
        logger.debug(
            "SEND discord | corr=%s | channel=%s | content=%r",
            envelope.correlation_id[:8],
            channel,
            preview,
        )

        await channel.send(envelope.content)

    async def _consume_outgoing_stream(self) -> None:
        """Background task: consume final answers from Atelier and send to Discord.

        Reads from ``relais:messages:outgoing:discord`` via a Redis consumer
        group (at-least-once delivery). Each message is ACKed in a ``finally``
        block after ``_deliver_outgoing_message`` runs, whether delivery
        succeeded or not. This prevents undeliverable messages (e.g. deleted
        Discord channels) from poisoning the PEL indefinitely.

        On outer Redis errors (connection loss, stream errors) the loop sleeps
        1 second before retrying.
        """
        await self._ensure_consumer_group(self.stream_out, self.group_name)
        logger.info("Listening for outgoing messages targeted to Discord...")

        while not self.is_closed():
            try:
                results = await self._redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_out: ">"},
                    count=10,
                    block=2000,
                )
                for _, messages in results:
                    for message_id, data in messages:
                        try:
                            await self._deliver_outgoing_message(data)
                        except Exception as exc:
                            logger.error(
                                "Undeliverable Discord message %s, routing to DLQ: %s",
                                message_id,
                                exc,
                            )
                            await self._redis_conn.xadd(
                                "relais:messages:outgoing:failed",
                                {
                                    "source": self.stream_out,
                                    "message_id": message_id,
                                    "payload": data.get("payload", ""),
                                    "reason": str(exc),
                                },
                            )
                        finally:
                            await self._redis_conn.xack(
                                self.stream_out, self.group_name, message_id
                            )
            except Exception as exc:
                logger.error("Background stream error: %s", exc)
                await asyncio.sleep(1)
