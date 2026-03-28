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

# Make sure we can import common
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from common.redis_client import RedisClient
from common.envelope import Envelope
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
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
        await self.redis_conn.xadd("relais:logs", {"level": "INFO", "brick": "aiguilleur-discord", "message": "Starting Discord API connection"})
        self.loop.create_task(self.consume_outgoing_stream())

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

def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or token == "dummy":
        logger.error("Please set DISCORD_BOT_TOKEN in .env")
        sys.exit(1)
        
    client = RelaisDiscordClient()
    client.run(token)

if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent.parent)
    main()
