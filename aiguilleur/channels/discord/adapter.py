"""Discord channel adapter — NativeAiguilleur implementation.

Bridges the Discord API and the RELAIS Redis bus:
- Produces:   relais:messages:incoming          (new user messages)
- Consumes:   relais:messages:outgoing:discord  (bot replies)
- Subscribes: relais:streaming:start:discord    (streaming sessions)
"""

from __future__ import annotations

import asyncio
import json
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

# ---------------------------------------------------------------------------
# Streaming constants
# ---------------------------------------------------------------------------

STREAM_EDIT_THROTTLE_CHARS = 80   # Edit Discord message every N chars
STREAM_READ_BLOCK_MS = 150        # XREAD block timeout in ms


class DiscordAiguilleur(NativeAiguilleur):
    """Discord channel adapter.

    Wraps RelaisDiscordClient in a NativeAiguilleur lifecycle.
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
    """Internal Discord client — not exposed outside this module."""

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
        self._redis_conn = await self._redis_client.get_connection()
        self._redis = self._redis_conn
        await self._redis_conn.xadd(
            "relais:logs",
            {
                "level": "INFO",
                "brick": "aiguilleur-discord",
                "message": "Starting Discord API connection",
            },
        )
        self.loop.create_task(self._consume_outgoing_stream())
        self.loop.create_task(self._subscribe_streaming_start())

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == self.user.id:
            return

        bot_mentioned = self.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)

        if not (bot_mentioned or is_dm):
            return

        content = message.content.replace(f"<@{self.user.id}>", "").strip()
        if not content:
            content = "Coucou!"

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

    async def _consume_outgoing_stream(self) -> None:
        """Background task reading answers from Atelier."""
        try:
            await self._redis_conn.xgroup_create(
                self.stream_out, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

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
                            envelope = Envelope.from_json(data.get("payload", "{}"))
                            channel_id = int(envelope.metadata.get("reply_to"))
                            channel = self.get_channel(channel_id)
                            if not channel:
                                user_id = int(envelope.sender_id.split(":")[1])
                                user = await self.fetch_user(user_id)
                                channel = await user.create_dm()

                            if channel:
                                if envelope.metadata.get("streamed"):
                                    redis_key = f"relais:streamed_msg:{envelope.correlation_id}"
                                    discord_msg_id = await self._redis_conn.get(redis_key)
                                    if discord_msg_id:
                                        partial = channel.get_partial_message(int(discord_msg_id))
                                        await partial.edit(content=envelope.content)
                                        await self._redis_conn.delete(redis_key)
                                    else:
                                        await channel.send(envelope.content)
                                else:
                                    await channel.send(envelope.content)
                        except Exception as exc:
                            logger.error("Failed to send Discord message: %s", exc)
                        finally:
                            await self._redis_conn.xack(
                                self.stream_out, self.group_name, message_id
                            )
            except Exception as exc:
                logger.error("Background stream error: %s", exc)
                await asyncio.sleep(1)

    async def _subscribe_streaming_start(self) -> None:
        """Listen on relais:streaming:start:discord and dispatch streaming tasks."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe("relais:streaming:start:discord")
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                envelope = Envelope.from_json(json.dumps(data))
                asyncio.create_task(self._handle_streaming_message(envelope))
            except Exception as exc:
                logger.warning("Streaming start parse error: %s", exc)

    async def _handle_streaming_message(self, envelope: Envelope) -> None:
        """Stream LLM chunks into a Discord message via live edits."""
        channel_id = int(
            envelope.metadata.get("discord_channel_id")
            or envelope.metadata.get("reply_to", 0)
        )

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as exc:
                logger.error("Cannot find Discord channel %s: %s", channel_id, exc)
                return

        stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

        try:
            msg = await channel.send("▌")
        except Exception as exc:
            logger.error("Failed to send streaming placeholder: %s", exc)
            return

        await self._redis.setex(
            f"relais:streamed_msg:{envelope.correlation_id}", 300, str(msg.id)
        )

        accumulated = ""
        buffer = ""
        last_id = "0"

        while True:
            try:
                results = await self._redis.xread(
                    {stream_key: last_id},
                    block=STREAM_READ_BLOCK_MS,
                    count=10,
                )
            except Exception as exc:
                logger.warning("xread streaming error: %s", exc)
                break

            if not results:
                continue

            for _, entries in results:
                for entry_id, fields in entries:
                    last_id = entry_id

                    if isinstance(fields, dict):
                        chunk = fields.get("chunk", "")
                        is_final = fields.get("is_final", "0") == "1"
                    else:
                        chunk = fields.get(b"chunk", b"").decode()
                        is_final = fields.get(b"is_final", b"0").decode() == "1"

                    buffer += chunk
                    accumulated += chunk

                    should_edit = len(buffer) >= STREAM_EDIT_THROTTLE_CHARS or is_final
                    if should_edit:
                        display = accumulated if is_final else accumulated + "▌"
                        try:
                            await msg.edit(content=display)
                        except Exception as edit_exc:
                            logger.debug("Discord edit error (non-fatal): %s", edit_exc)
                        buffer = ""

                    if is_final:
                        return
