"""Unit tests for common/redis_client.py — stale connection handling."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.unit
class TestRedisClientReconnect:
    @pytest.fixture
    def client(self):
        from common.redis_client import RedisClient
        return RedisClient("atelier")

    @pytest.mark.asyncio
    async def test_first_call_connects_and_pings(self, client):
        mock_conn = AsyncMock()
        with patch("common.redis_client.redis.Redis", return_value=mock_conn):
            conn = await client.get_connection()
        assert conn is mock_conn
        mock_conn.ping.assert_called()

    @pytest.mark.asyncio
    async def test_second_call_reuses_live_connection(self, client):
        mock_conn = AsyncMock()
        with patch("common.redis_client.redis.Redis", return_value=mock_conn):
            first = await client.get_connection()
            second = await client.get_connection()
        assert first is second
        # Redis() constructor called only once
        assert mock_conn.ping.call_count == 2  # once on create, once on re-use check

    @pytest.mark.asyncio
    async def test_stale_connection_triggers_reconnect(self, client):
        """A PING failure on the cached connection must trigger a transparent reconnect."""
        stale_conn = AsyncMock()
        fresh_conn = AsyncMock()

        # First call succeeds → stale_conn stored
        stale_conn.ping = AsyncMock(return_value=True)
        with patch("common.redis_client.redis.Redis", return_value=stale_conn):
            await client.get_connection()

        # Now the cached connection is stale: PING raises
        stale_conn.ping = AsyncMock(side_effect=Exception("connection lost"))
        with patch("common.redis_client.redis.Redis", return_value=fresh_conn):
            conn = await client.get_connection()

        assert conn is fresh_conn, "should have reconnected to fresh_conn after stale PING"

    @pytest.mark.asyncio
    async def test_close_clears_connection(self, client):
        mock_conn = AsyncMock()
        with patch("common.redis_client.redis.Redis", return_value=mock_conn):
            await client.get_connection()

        await client.close()
        mock_conn.close.assert_called_once()
        assert client._connection is None

    @pytest.mark.asyncio
    async def test_close_is_idempotent_when_not_connected(self, client):
        """close() on a never-connected client must not raise."""
        await client.close()  # should not raise
