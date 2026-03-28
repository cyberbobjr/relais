import asyncio
import json
import logging
import sys
from datetime import datetime
from common.redis_client import RedisClient
from common.config_loader import get_relais_home

# Configure local simple logging for the archivist itself
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("archiviste")

class Archiviste:
    def __init__(self):
        self.client = RedisClient("archiviste")
        self.base_dir = get_relais_home() / "logs"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.events_log = self.base_dir / "events.jsonl"
        self.system_log = self.base_dir / "system.log"

    def _write_event(self, timestamp: str, stream: bytes, message: dict):
        """Append event to the JSONL ledger."""
        try:
            record = {
                "ts": timestamp,
                "stream": stream,
                "data": {k: v for k, v in message.items()}
            }
            with open(self.events_log, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Failed to write event: {e}")

    async def _process_stream(self, redis_conn):
        """Consume streams using a static consumer group."""
        group_name = "archiviste_group"
        consumer_name = "archiviste_1"
        streams = {
            "relais:logs": ">",
            "relais:events:system": ">",
            "relais:events:messages": ">"
        }

        # Create consumer group if it doesn't exist
        for stream in streams.keys():
            try:
                await redis_conn.xgroup_create(stream, group_name, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.warning(f"Consumer group error for {stream}: {e}")

        logger.info("Archiviste listening to streams...")
        
        while True:
            try:
                # Block for 2 seconds waiting for new events
                results = await redis_conn.xreadgroup(
                    group_name,
                    consumer_name,
                    streams,
                    count=50,
                    block=2000
                )
                
                for stream, messages in results:
                    for message_id, data in messages:
                        self._write_event(message_id, stream, data)
                        # Acknowledge the message so it's removed from PEL
                        await redis_conn.xack(stream, group_name, message_id)
                        
                        # Print specifically system logs to stdout
                        if stream == "relais:logs":
                            msg = data.get("message", "")
                            level = data.get("level", "INFO")
                            brick = data.get("brick", "UNKNOWN")
                            print(f"[{level}] {brick}: {msg}")

            except Exception as e:
                logger.error(f"Error reading from stream: {e}")
                await asyncio.sleep(1)

    async def start(self):
        redis_conn = await self.client.get_connection()
        try:
            await self._process_stream(redis_conn)
        except asyncio.CancelledError:
            logger.info("Archiviste shutting down...")
        finally:
            await self.client.close()

if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent)
    archiviste = Archiviste()
    try:
        asyncio.run(archiviste.start())
    except KeyboardInterrupt:
        pass
