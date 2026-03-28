import os
import redis.asyncio as redis
from typing import Optional
from .config_loader import get_relais_home

class RedisClient:
    """
    Shared Redis client factory for RELAIS bricks.
    Supports Unix socket connection and ACL authentication.
    """
    def __init__(self, brick_name: str):
        self.brick_name = brick_name
        self._connection: Optional[redis.Redis] = None

    async def get_connection(self) -> redis.Redis:
        if self._connection:
            return self._connection

        # Configuration from environment (set in .env)
        # Default to socket in RELAIS_HOME as per architecture
        default_socket = str(get_relais_home() / "redis.sock")
        
        socket_path = os.environ.get("REDIS_SOCKET_PATH", default_socket)
        password = os.environ.get(f"REDIS_PASS_{self.brick_name.upper()}")
        
        # Fallback to general password if brick-specific password is missing
        if not password:
            password = os.environ.get("REDIS_PASSWORD")

        self._connection = redis.Redis(
            unix_socket_path=socket_path,
            username=self.brick_name,
            password=password,
            decode_responses=True
        )
        
        # Test connection
        await self._connection.ping()
        return self._connection

    async def close(self):
        if self._connection:
            await self._connection.close()
            self._connection = None
