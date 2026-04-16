"""REST channel adapter — NativeAiguilleur implementation.

Exposes an HTTP/JSON (and optional SSE) API for programmatic message exchange
with RELAIS. Useful for CLI tools, CI pipelines, and other REST clients.

Bridges HTTP requests and the RELAIS Redis bus:
- Receives: POST /v1/messages (Bearer-authenticated)
- Publishes: relais:messages:incoming:rest
- Consumes:  relais:messages:outgoing:rest  (via consumer group)
- Returns:   JSON response or SSE stream to the HTTP caller

Auth: Bearer API keys resolved via UserRegistry (portail.yaml).
Rate limiting: delegated to the upstream reverse proxy.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.native import NativeAiguilleur
from aiguilleur.channels.rest.correlator import ResponseCorrelator
from aiguilleur.channels.rest.server import create_app
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_PROGRESS
from common.redis_client import RedisClient
from common.streams import stream_outgoing

logger = logging.getLogger("aiguilleur.rest")

_CHANNEL = "rest"
_GROUP = "rest_relay_group"
_CONSUMER = "rest_relay_1"


class RestAiguilleur(NativeAiguilleur):
    """HTTP REST channel adapter.

    Reads adapter settings from ``config.extras``:
        bind (str): Host to bind the HTTP server to. Default ``"127.0.0.1"``.
        port (int): TCP port to listen on. Default ``8080``.
        request_timeout (float): Seconds to wait for an LLM reply. Default 30.
        cors_origins (list[str]): Allowed CORS origins. Default ``["*"]``.
        include_traces (bool): Whether to include pipeline traces in JSON
            responses. Default ``False``.

    Args:
        config: Channel configuration loaded from aiguilleur.yaml.
    """

    def __init__(self, config: ChannelConfig) -> None:
        """Initialise the REST adapter.

        Args:
            config: Channel configuration with extras dict for HTTP settings.
        """
        super().__init__(config)
        extras: dict = config.extras if hasattr(config, "extras") else {}
        self._bind: str = str(extras.get("bind", "127.0.0.1"))
        self._port: int = int(extras.get("port", 8080))
        self._request_timeout: float = float(extras.get("request_timeout", 30))
        self._cors_origins: list[str] = list(extras.get("cors_origins", ["*"]))
        self._include_traces: bool = bool(extras.get("include_traces", False))
        self._redis_client = RedisClient("aiguilleur")

    async def run(self) -> None:
        """Async entry point: start the HTTP server and the outgoing consumer.

        Blocks until ``stop_event`` is set (triggered by ``stop()``). Performs
        a clean shutdown: cancels pending futures, stops the consumer task,
        and cleans up the aiohttp runner.
        """
        redis_conn = await self._redis_client.get_connection()
        correlator = ResponseCorrelator()

        # Load user registry for auth middleware
        try:
            from portail.user_registry import UserRegistry
            registry = UserRegistry()
        except Exception as exc:
            logger.error("Failed to load UserRegistry — REST adapter cannot start: %s", exc)
            raise

        await self._setup_consumer_group(redis_conn)

        server_config: dict = {
            "bind": self._bind,
            "port": self._port,
            "request_timeout": self._request_timeout,
            "cors_origins": self._cors_origins,
            "include_traces": self._include_traces,
        }

        app = create_app(
            adapter=self,
            redis_conn=redis_conn,
            correlator=correlator,
            registry=registry,
            config=server_config,
        )

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind, self._port)
        await site.start()
        logger.info(
            "REST adapter listening on http://%s:%d", self._bind, self._port
        )

        # Start outgoing consumer as background task
        consumer_task = asyncio.create_task(
            self._consume_outgoing(redis_conn, correlator),
            name="rest-outgoing-consumer",
        )

        try:
            # Wait for stop signal (bridge threading.Event → asyncio)
            await asyncio.to_thread(self._stop_event.wait)
        finally:
            logger.info("REST adapter shutting down...")
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
            await runner.cleanup()
            logger.info("REST adapter stopped.")

    async def _setup_consumer_group(self, redis_conn) -> None:
        """Create the outgoing consumer group idempotently.

        Args:
            redis_conn: Async Redis connection.

        Raises:
            Exception: Any Redis error other than BUSYGROUP (group already exists).
        """
        stream = stream_outgoing(_CHANNEL)
        try:
            await redis_conn.xgroup_create(stream, _GROUP, id="$", mkstream=True)
            logger.info("Created consumer group %s on %s", _GROUP, stream)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug("Consumer group %s already exists (BUSYGROUP)", _GROUP)
            else:
                raise

    async def _consume_outgoing(self, redis_conn, correlator: ResponseCorrelator) -> None:
        """Background task: consume outgoing replies and resolve pending futures.

        Reads from ``relais:messages:outgoing:rest`` via consumer group
        ``rest_relay_group``. For each message, calls ``correlator.resolve()``
        so the waiting HTTP handler can return the response.

        Orphan messages (no registered Future, e.g. after a timeout) are
        DEBUG-logged and ACKed normally to prevent PEL accumulation.

        Args:
            redis_conn: Async Redis connection.
            correlator: Shared ResponseCorrelator.
        """
        stream = stream_outgoing(_CHANNEL)
        logger.info("REST outgoing consumer started on %s", stream)

        while not self._stop_event.is_set():
            try:
                results = await redis_conn.xreadgroup(
                    _GROUP,
                    _CONSUMER,
                    {stream: ">"},
                    count=10,
                    block=2000,
                )
                for _, messages in results:
                    for message_id, data in messages:
                        await self._handle_outgoing_message(
                            data, message_id, redis_conn, correlator, stream
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("REST outgoing consumer error: %s", exc)
                await asyncio.sleep(1)

    async def _handle_outgoing_message(
        self,
        data: dict,
        message_id: str,
        redis_conn,
        correlator: ResponseCorrelator,
        stream: str,
    ) -> None:
        """Parse and dispatch a single outgoing message.

        Args:
            data: Raw Redis stream entry fields.
            message_id: Redis stream message ID (for XACK).
            redis_conn: Async Redis connection.
            correlator: Shared ResponseCorrelator.
            stream: Redis stream key (for XACK).
        """
        try:
            payload = data.get(b"payload") or data.get("payload") or ""
            if isinstance(payload, bytes):
                payload = payload.decode()
            if not payload:
                logger.debug("Empty payload in outgoing message %s — skipping", message_id)
                return

            envelope = Envelope.from_json(payload)
            if envelope.action == ACTION_MESSAGE_PROGRESS:
                return  # Skip progress events — only resolve on final reply
            await correlator.resolve(envelope.correlation_id, envelope)
        except Exception as exc:
            logger.error("Failed to process outgoing REST message %s: %s", message_id, exc)
        finally:
            try:
                await redis_conn.xack(stream, _GROUP, message_id)
            except Exception as exc:
                logger.error("Failed to XACK message %s: %s", message_id, exc)
