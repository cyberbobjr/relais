import logging
import os
import redis.asyncio as redis
from typing import Optional
from .config_loader import get_relais_home

logger = logging.getLogger(__name__)


class RedisClient:
    """Shared Redis client factory for RELAIS bricks.

    Supports Unix socket connection and ACL authentication.
    Validates connection liveness on every call to ``get_connection``.
    """

    def __init__(self, brick_name: str) -> None:
        """Initialise the client for a named brick.

        Args:
            brick_name: Identifier of the brick (e.g. ``"atelier"``).
                Used to resolve the brick-specific Redis password from the
                environment variable ``REDIS_PASS_<BRICK_NAME>``.
        """
        self.brick_name = brick_name
        self._connection: Optional[redis.Redis] = None

    async def get_connection(self) -> redis.Redis:
        """Return a live Redis connection, creating one if necessary.

        On first call, connects to the Unix socket and authenticates via PING.
        On subsequent calls, issues a PING to verify the cached connection is
        still alive; reconnects transparently if it is not (e.g. after a Redis
        restart).

        Returns:
            An authenticated, ready-to-use async Redis connection.

        Raises:
            redis.exceptions.ConnectionError: If the socket cannot be reached.
        """
        if self._connection is not None:
            try:
                await self._connection.ping()
                return self._connection
            except Exception:
                logger.warning(
                    "RedisClient[%s]: cached connection stale — reconnecting",
                    self.brick_name,
                )
                self._connection = None

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
            decode_responses=True,
        )

        # Test connection on first open
        await self._connection.ping()
        return self._connection

    async def close(self) -> None:
        """Close the Redis connection and reset the internal handle.

        Safe to call even if no connection has been opened yet.
        """
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
