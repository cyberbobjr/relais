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
from common.config_loader import get_default_llm_profile
from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.native import NativeAiguilleur

logger = logging.getLogger("aiguilleur.discord")

_TYPING_MAX_SECONDS: float = 120.0


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

        client = _RelaisDiscordClient(stop_event=self.stop_event, channel_config=self.config)
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

    def __init__(
        self,
        stop_event: asyncio.Event | None = None,
        channel_config: ChannelConfig | None = None,
    ) -> None:
        """Initialise the Discord client.

        Args:
            stop_event: Optional event to signal the adapter should stop.
            channel_config: Optional channel configuration for this adapter.
                Used to resolve the LLM profile stamped on every incoming
                envelope. When None, falls back to the system default profile.
        """
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
        self._channel_config = channel_config
        # Resolve LLM profile once at init: channel config > system default
        self._llm_profile: str = (
            channel_config.profile
            if channel_config is not None and channel_config.profile is not None
            else get_default_llm_profile()
        )
        # Active typing indicator tasks keyed by correlation_id
        self._typing_tasks: dict[str, asyncio.Task] = {}

    async def _typing_loop(
        self, channel: discord.abc.Messageable, correlation_id: str
    ) -> None:
        """Maintain a typing indicator until cancelled or the timeout expires.

        Uses ``channel.typing()`` — discord.py's built-in context manager —
        which sends ``trigger_typing`` every 5 seconds automatically. The task
        sleeps inside the context for up to ``_TYPING_MAX_SECONDS`` as a safety
        net against pipeline failures that would never deliver a reply.
        Cancelling the task (via ``_cancel_typing``) raises ``CancelledError``
        in ``asyncio.sleep``, which exits the context manager cleanly.

        Args:
            channel: The Discord channel or DM to show the indicator in.
            correlation_id: Key used to register this task in ``_typing_tasks``.
        """
        try:
            async with channel.typing():
                await asyncio.sleep(_TYPING_MAX_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("typing indicator error (ignored): %s", exc)
        finally:
            self._typing_tasks.pop(correlation_id, None)

    def _cancel_typing(self, correlation_id: str) -> None:
        """Cancel the typing indicator task for the given correlation ID.

        Safe to call even if no task is registered for that ID.

        Args:
            correlation_id: The correlation ID whose typing task to cancel.
        """
        task = self._typing_tasks.pop(correlation_id, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        """Shut down the client and cancel any pending typing indicator tasks.

        Cancels all active typing tasks before delegating to the parent
        ``discord.Client.close()`` to avoid "task was destroyed but pending"
        warnings on shutdown.
        """
        for task in list(self._typing_tasks.values()):
            task.cancel()
        self._typing_tasks.clear()
        await super().close()

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
                "access_context": "dm" if is_dm else "server",
                "channel_profile": self._llm_profile,
            },
        )

        typing_task = self.loop.create_task(
            self._typing_loop(message.channel, envelope.correlation_id)
        )
        self._typing_tasks[envelope.correlation_id] = typing_task

        try:
            await self._redis_conn.xadd(self.stream_in, {"payload": envelope.to_json()})
            logger.info("Queued message from %s", message.author.name)
        except Exception as exc:
            logger.error("Failed to queue message: %s", exc)
            self._cancel_typing(envelope.correlation_id)

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

        self._cancel_typing(envelope.correlation_id)
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
