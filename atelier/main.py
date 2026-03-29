"""Atelier brick — SDK-based LLM execution pipeline.

Consumes validated tasks from ``relais:tasks``, fetches conversation context
from Souvenir, runs the request through the claude-agent-sdk, and publishes
the response envelope to the channel's outgoing stream.

For streaming-capable channels (discord, telegram, tui) each chunk is also
published to a Redis Stream via StreamPublisher so clients can render
progressive responses.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.shutdown import GracefulShutdown
from atelier.profile_loader import load_profiles, resolve_profile
from atelier.mcp_loader import load_for_sdk, load_subagents_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.sdk_executor import SDKExecutor, SDKExecutionError
from atelier.stream_publisher import StreamPublisher

# Configure logging to standard output
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("atelier")

# Channels that support incremental chunk streaming
STREAMING_CAPABLE_CHANNELS: frozenset[str] = frozenset({"discord", "telegram", "tui"})

# Timeout (seconds) waiting for Souvenir memory response
_MEMORY_TIMEOUT_SECONDS: float = 3.0

# Directory containing soul/channels/roles/policies prompts
_PROMPTS_DIR: Path = Path(__file__).parent.parent / "prompts"


class Atelier:
    """The Atelier brick — orchestrates SDK-based LLM generation.

    Consumes tasks from ``relais:tasks``, fetches context from Souvenir,
    calls the claude-agent-sdk, and publishes response envelopes.
    """

    def __init__(self) -> None:
        """Initialise Atelier with Redis stream and consumer group config.

        Loads LLM profiles, MCP server configs, and subagent definitions once
        at startup to avoid repeated filesystem I/O on every message.

        Raises:
            RuntimeError: If the ``claude`` CLI binary is not found on PATH.
        """
        if shutil.which("claude") is None:
            raise RuntimeError(
                "claude CLI not found on PATH. "
                "Install it with: npm install -g @anthropic-ai/claude-code"
            )
        self.client: RedisClient = RedisClient("atelier")
        self.stream_in: str = "relais:tasks"
        self.group_name: str = "atelier_group"
        self.consumer_name: str = "atelier_1"

        # Load static configuration once at startup — avoids 3+ disk reads
        # per message for resources that do not change during the process lifetime.
        self._profiles = load_profiles()
        self._mcp_servers_default = load_for_sdk()
        # max_agent_depth from the 'default' profile; per-profile overrides are
        # applied lazily in _handle_message when a non-default profile is needed.
        _default_profile = resolve_profile(self._profiles, "default")
        self._subagents = load_subagents_for_sdk(
            max_turns=_default_profile.max_agent_depth
        )

    async def _handle_message(
        self,
        redis_conn: Any,
        message_id: str,
        payload: str,
    ) -> bool:
        """Process a single task message from the Redis stream.

        Fetches memory context from Souvenir, calls the SDK executor, and
        publishes the response.  Routes SDKExecutionError payloads to the DLQ.

        Args:
            redis_conn: Active Redis connection.
            message_id: Redis stream message ID (used for DLQ logging).
            payload: Raw JSON string of the task envelope.

        Returns:
            True when the message should be ACKed (success or DLQ routing).
            False when a transient error occurred and the message should
            remain in the PEL for re-delivery.
        """
        envelope: Envelope | None = None
        try:
            envelope = Envelope.from_json(payload)
            logger.info(
                "[%s] Processing task for %s",
                envelope.correlation_id,
                envelope.sender_id,
            )

            # 1. Resolve LLM profile
            profile_name = envelope.metadata.get("llm_profile", "default")
            profile = resolve_profile(self._profiles, profile_name)

            # 2. Request context from Souvenir
            context = await self._fetch_context(redis_conn, envelope)

            # 3. Assemble soul system prompt
            soul_prompt = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                channel=envelope.channel,
                sender_id=envelope.sender_id,
            )

            # 4. Select MCP servers for this profile.
            # Re-use the pre-loaded default set unless a non-default profile is
            # requested — profile-specific contextual servers require a fresh load.
            if profile_name == "default":
                mcp_servers = self._mcp_servers_default
            else:
                mcp_servers = load_for_sdk(profile=profile_name)

            # 4b. Subagents already loaded at startup; the master switch and
            # max_agent_depth were applied then.
            subagents = self._subagents

            # 5. Create SDK executor
            sdk_executor = SDKExecutor(
                profile=profile,
                soul_prompt=soul_prompt,
                mcp_servers=mcp_servers,
                subagents=subagents,
            )

            # 6. Execute — with optional streaming for capable channels
            stream_pub: StreamPublisher | None = None
            stream_callback = None

            if envelope.channel in STREAMING_CAPABLE_CHANNELS:
                stream_pub = StreamPublisher(
                    redis_conn,
                    channel=envelope.channel,
                    correlation_id=envelope.correlation_id,
                )
                await redis_conn.publish(
                    f"relais:streaming:start:{envelope.channel}",
                    envelope.correlation_id,
                )
                stream_callback = stream_pub.push_chunk

            reply_text = await sdk_executor.execute(
                envelope=envelope,
                context=context,
                stream_callback=stream_callback,
            )

            if stream_pub is not None:
                await stream_pub.finalize()

            # 7. Build and publish response envelope
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.metadata["user_message"] = envelope.content
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = f"relais:messages:outgoing:{envelope.channel}"
            await redis_conn.xadd(out_stream, {"payload": response_env.to_json()})

            await redis_conn.xadd("relais:logs", {
                "level": "INFO",
                "brick": "atelier",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": f"Answered {envelope.correlation_id} via {out_stream}",
                "content_preview": (reply_text or "")[:60],
            })

            logger.info(
                "[%s] Answered via %s", envelope.correlation_id, out_stream
            )
            return True

        except SDKExecutionError as exc:
            # Non-recoverable SDK failure — route to DLQ and ACK
            cid = envelope.correlation_id if envelope else message_id
            sid = envelope.sender_id if envelope else ""
            logger.error("[%s] SDK execution error, routing to DLQ: %s", cid, exc)
            await redis_conn.xadd(
                "relais:tasks:failed",
                {
                    "payload": payload,
                    "reason": str(exc),
                    "failed_at": str(time.time()),
                },
            )
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "correlation_id": cid,
                "sender_id": sid,
                "message": f"SDK execution error for {cid}: {exc}",
                "error": str(exc),
            })
            return True

        except Exception as exc:
            # Unknown transient or permanent error — leave in PEL for re-delivery
            cid = envelope.correlation_id if envelope else message_id
            sid = envelope.sender_id if envelope else ""
            logger.error("[%s] Unhandled exception, leaving in PEL: %s", cid, exc, exc_info=True)
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "correlation_id": cid,
                "sender_id": sid,
                "message": f"Unhandled exception for {cid}: {exc}",
                "error": str(exc),
            })
            return False

    async def _fetch_context(
        self, redis_conn: Any, envelope: Envelope
    ) -> list[dict]:
        """Request the conversation context from Souvenir via Redis.

        Sends a 'get' request to ``relais:memory:request`` and waits up to
        _MEMORY_TIMEOUT_SECONDS for the matching response on
        ``relais:memory:response``.  Falls back to an empty list on timeout.

        Args:
            redis_conn: Active Redis connection.
            envelope: The current task envelope.

        Returns:
            List of role/content message dicts from memory, or [] on timeout.
        """
        correlation_id = str(uuid.uuid4())
        get_req = {
            "action": "get",
            "session_id": envelope.session_id,
            "correlation_id": correlation_id,
        }
        xadd_id: bytes | str = await redis_conn.xadd(
            "relais:memory:request", {"payload": json.dumps(get_req)}
        )

        # Derive the XREAD starting point from the XADD return value so that
        # a Souvenir response arriving between XADD and XREAD is never missed.
        # Using "$" would skip messages published before the first XREAD call.
        if isinstance(xadd_id, bytes):
            xadd_id = xadd_id.decode()
        try:
            ts_ms, seq = xadd_id.split("-")
            last_id = f"{int(ts_ms) - 1}-0"
        except (ValueError, AttributeError):
            last_id = "0"

        deadline = asyncio.get_event_loop().time() + _MEMORY_TIMEOUT_SECONDS

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            block_ms = max(1, int(remaining * 1000))
            try:
                results = await redis_conn.xread(
                    {"relais:memory:response": last_id},
                    count=10,
                    block=block_ms,
                )
                if results:
                    for _, messages in results:
                        for msg_id, data in messages:
                            last_id = msg_id
                            res = json.loads(data.get("payload", "{}"))
                            if res.get("correlation_id") == correlation_id:
                                return res.get("history", [])
            except Exception as exc:
                logger.warning("Error reading memory response: %s", exc)
                break

        logger.warning(
            "[%s] Memory context timeout for session %s",
            envelope.correlation_id,
            envelope.session_id,
        )
        return []

    async def _process_stream(self, redis_conn: Any, shutdown: GracefulShutdown | None = None) -> None:
        """Main loop: reads from the Redis stream and dispatches messages.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Active Redis connection.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        try:
            await redis_conn.xgroup_create(
                self.stream_in, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

        logger.info("Atelier listening for tasks...")

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_in: ">"},
                    count=5,
                    block=2000,
                )

                if not results:
                    continue

                for _, messages in results:
                    for message_id, data in messages:
                        payload = data.get("payload", "{}")
                        should_ack = await self._handle_message(
                            redis_conn, message_id, payload
                        )
                        if should_ack:
                            await redis_conn.xack(
                                self.stream_in, self.group_name, message_id
                            )

            except Exception as exc:
                logger.error("Stream error: %s", exc)
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Start the Atelier service and its main processing loop.

        Registers SIGTERM/SIGINT handlers via GracefulShutdown so the process
        exits cleanly when sent a termination signal.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd(
            "relais:logs",
            {"level": "INFO", "brick": "atelier", "message": "Atelier started"},
        )
        try:
            await self._process_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Atelier shutting down...")
        finally:
            await self.client.close()
            logger.info("Atelier stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir(Path(__file__).parent.parent)
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
