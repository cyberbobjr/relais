import asyncio
import logging
import os
import certifi

# Fix for macOS SSL certificate verify failed
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

import sys
import ssl
import json
import discord
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Streaming constants
# ---------------------------------------------------------------------------

STREAM_EDIT_THROTTLE_CHARS = 80   # Edit Discord message every N chars (rate limit ~5 edits/s)
STREAM_READ_BLOCK_MS = 150        # XREAD block timeout in ms

# Make sure we can import common
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from common.redis_client import RedisClient
from common.envelope import Envelope
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("aiguilleur_discord")

class RelaisDiscordClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        # Redis setup
        self.redis_client = RedisClient("aiguilleur")
        self.stream_in = "relais:messages:incoming"
        self.stream_out = "relais:messages:outgoing:discord"
        self.group_name = "discord_relay_group"
        self.consumer_name = f"discord_{os.getpid()}"
        self.redis_conn = None

    async def setup_hook(self):
        """Called once the bot is logging in."""
        self.redis_conn = await self.redis_client.get_connection()
        self._redis = self.redis_conn
        await self.redis_conn.xadd("relais:logs", {"level": "INFO", "brick": "aiguilleur-discord", "message": "Starting Discord API connection"})
        self.loop.create_task(self.consume_outgoing_stream())
        self.loop.create_task(self._subscribe_streaming_start())

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("Discord Relay is ready!")

    async def on_message(self, message: discord.Message):
        # Ignore our own messages
        if message.author.id == self.user.id:
            return

        # MVP: Only respond to direct mentions or DM
        bot_mentioned = self.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)
        
        if bot_mentioned or is_dm:
            # Clean content from mention
            content = message.content.replace(f'<@{self.user.id}>', '').strip()
            if not content:
                content = "Coucou!"
            
            sender_id = f"discord:{message.author.id}"
            
            envelope = Envelope(
                channel="discord",
                sender_id=sender_id,
                content=content,
                session_id=str(message.channel.id),
                metadata={
                    "content_type": "text",
                    "reply_to": str(message.channel.id)
                }
            )
            
            try:
                await self.redis_conn.xadd(self.stream_in, {"payload": envelope.to_json()})
                logger.info(f"Sent message from {message.author.name} to core loop.")
            except Exception as e:
                logger.error(f"Failed to queue message: {e}")

    async def consume_outgoing_stream(self):
        """Background task reading answers sent back from Workshop."""
        try:
            await self.redis_conn.xgroup_create(self.stream_out, self.group_name, mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Consumer group error: {e}")
                
        logger.info("Listening for outgoing messages targeted to Discord...")

        while not self.is_closed():
            try:
                results = await self.redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_out: ">"},
                    count=10,
                    block=2000
                )
                
                for stream, messages in results:
                    for message_id, data in messages:
                        try:
                            payload = data.get("payload", "{}")
                            envelope = Envelope.from_json(payload)
                            
                            logger.info(f"Received answer intended for {envelope.sender_id}")
                            channel_id = int(envelope.metadata.get("reply_to"))
                            
                            channel = self.get_channel(channel_id)
                            if not channel:
                                # Fallback: fetch user if it's a DM channel missing from cache
                                user_id = int(envelope.sender_id.split(":")[1])
                                user = await self.fetch_user(user_id)
                                channel = await user.create_dm()

                            if channel:
                                await channel.send(envelope.content)
                            
                        except Exception as inner_e:
                            logger.error(f"Failed to send Discord message: {inner_e}")
                        finally:
                            await self.redis_conn.xack(self.stream_out, self.group_name, message_id)

            except Exception as e:
                logger.error(f"Background stream error: {e}")
                await asyncio.sleep(1)

    async def _subscribe_streaming_start(self) -> None:
        """Listen on Redis Pub/Sub relais:streaming:start:discord.

        When a streaming session starts, spawns _handle_streaming_message()
        as an asyncio task so it does not block the subscriber loop.
        """
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
                logger.warning("streaming start parse error: %s", exc)

    async def _handle_streaming_message(self, envelope: Envelope) -> None:
        """Stream LLM chunks into a Discord message via live edits.

        Flow:
        1. Send a placeholder '▌' message to Discord.
        2. XREAD relais:messages:streaming:discord:{correlation_id} in a loop.
        3. Accumulate chunks; edit when buffer >= STREAM_EDIT_THROTTLE_CHARS or is_final.
        4. Final edit removes the cursor '▌'.

        Args:
            envelope: The envelope whose correlation_id identifies the Redis stream
                      and whose metadata carries the target discord_channel_id.
        """
        channel_id = int(envelope.metadata.get("discord_channel_id", 0))

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as exc:
                logger.error("Cannot find discord channel %s: %s", channel_id, exc)
                return

        stream_key = f"relais:messages:streaming:discord:{envelope.correlation_id}"

        # Send placeholder to reserve the message slot
        try:
            msg = await channel.send("▌")
        except Exception as exc:
            logger.error("Failed to send streaming placeholder: %s", exc)
            return

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
                continue  # timeout — keep waiting

            for _, entries in results:
                for entry_id, fields in entries:
                    last_id = entry_id

                    # Support both str and bytes keys (aioredis version differences)
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


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or token == "dummy":
        logger.error("Please set DISCORD_BOT_TOKEN in .env")
        sys.exit(1)
        
    client = RelaisDiscordClient()
    client.run(token, log_handler=None)

if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent.parent)
    main()
