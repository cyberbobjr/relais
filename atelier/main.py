"""Atelier brick — DeepAgents-based LLM execution pipeline.

Message flow (one task at a time):

  relais:tasks
    │  (1) deserialise Envelope, resolve profile
    │  (2) request context from Souvenir  ──► relais:memory:request
    │                                     ◄── relais:memory:response
    │  (3) assemble soul system prompt
    │  (4) start MCP sessions + build LangChain tools
    │  (5) AgentExecutor.execute() — DeepAgents agentic loop
    │      ├── streaming chunks ──► relais:messages:streaming:{channel}:{corr_id}
    │      └── full reply
    │  (6) publish response Envelope
    └──► relais:messages:outgoing_pending:{channel}

XACK contract:
  - Return True  → ACK (success or final error routed to DLQ)
  - Return False → no ACK (transient error; message stays in PEL for re-delivery)

For streaming-capable channels (discord, telegram, tui) each text chunk is
also published via StreamPublisher for real-time rendering before the full
reply is ready.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Configure logging to standard output.
# LOG_LEVEL env var controls verbosity (default: INFO).
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.shutdown import GracefulShutdown
from atelier.profile_loader import load_profiles, resolve_profile
from atelier.mcp_loader import load_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.agent_executor import AgentExecutor, AgentExecutionError
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.stream_publisher import StreamPublisher
from atelier.tools import make_skills_tools
from common.config_loader import resolve_prompts_dir, resolve_skills_dir
from aiguilleur.channel_config import load_channels_config

logger = logging.getLogger("atelier")

# Timeout (seconds) waiting for Souvenir memory response.
# Kept deliberately short (3 s) — a slow or unavailable Souvenir should
# degrade gracefully (empty context) rather than delay every message.
_MEMORY_TIMEOUT_SECONDS: float = 3.0

# Directory containing soul/channels/roles/policies prompts.
# Resolved via the config cascade so users can override in ~/.relais/prompts/.
_PROMPTS_DIR: Path = resolve_prompts_dir()


class Atelier:
    """The Atelier brick — orchestrates DeepAgents-based LLM generation.

    Consumes tasks from ``relais:tasks``, fetches context from Souvenir,
    calls the LLM via DeepAgents/LangChain (AgentExecutor), and publishes
    response envelopes.
    """

    def __init__(self) -> None:
        """Initialise Atelier with Redis stream and consumer group config.

        Loads LLM profiles, MCP server configs, and skills tools once at
        startup to avoid repeated filesystem I/O on every message.
        """
        self.client: RedisClient = RedisClient("atelier")
        self.stream_in: str = "relais:tasks"
        self.group_name: str = "atelier_group"
        self.consumer_name: str = "atelier_1"

        # Load static configuration once at startup — avoids 3+ disk reads
        # per message for resources that do not change during the process lifetime.
        self._profiles = load_profiles()
        self._mcp_servers_default = load_for_sdk()

        # Channels that support incremental chunk streaming — loaded here
        # (not at module level) so tests can import atelier.main without
        # triggering filesystem I/O before any fixture is in place.
        self._streaming_capable_channels: frozenset[str] = frozenset(
            name for name, cfg in load_channels_config().items() if cfg.streaming
        )

        # Build skills tools once — no need to re-scan the skills directory
        # on every message.  MCP tools are built per-message (session-bound).
        self._skills_tools = make_skills_tools(resolve_skills_dir())


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
        logger.debug("_handle_message payload: %s", payload)
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
                user_role=envelope.metadata.get("user_role"),
            )

            # 4. Select MCP servers for this profile.
            # Re-use the pre-loaded default set unless a non-default profile is
            # requested — profile-specific contextual servers require a fresh load.
            if profile_name == "default":
                mcp_servers = self._mcp_servers_default
            else:
                mcp_servers = load_for_sdk(profile=profile_name)

            # 5. Start MCP sessions and assemble tools for this request.
            # McpSessionManager is bound to the AsyncExitStack so all MCP
            # sub-processes are terminated when the stack exits.
            stream_pub: StreamPublisher | None = None
            stream_callback = None

            session_manager = McpSessionManager(profile, mcp_servers)
            async with contextlib.AsyncExitStack() as stack:
                await session_manager.start_all(stack)
                mcp_tools = await make_mcp_tools(session_manager)
                tools = [*self._skills_tools, *mcp_tools]

                # 6. Create AgentExecutor with the assembled tools.
                agent_executor = AgentExecutor(
                    profile=profile,
                    soul_prompt=soul_prompt,
                    tools=tools,
                )

                # 7. Execute — with optional streaming for capable channels.
                if envelope.channel in self._streaming_capable_channels:
                    stream_pub = StreamPublisher(
                        redis_conn,
                        channel=envelope.channel,
                        correlation_id=envelope.correlation_id,
                    )
                    await redis_conn.publish(
                        f"relais:streaming:start:{envelope.channel}",
                        envelope.to_json(),
                    )
                    stream_callback = stream_pub.push_chunk

                reply_text = await agent_executor.execute(
                    envelope=envelope,
                    context=context,
                    stream_callback=stream_callback,
                )
            # MCP sessions closed; finalize stream and publish response.

            # 8. Build and publish response envelope.
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.metadata["user_message"] = envelope.content
            if stream_pub is not None:
                await stream_pub.finalize()
                response_env.metadata["streamed"] = True
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = "relais:messages:outgoing_pending"
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

        except AgentExecutionError as exc:
            # Non-recoverable agent failure — route to DLQ and ACK
            cid = envelope.correlation_id if envelope else message_id
            sid = envelope.sender_id if envelope else ""
            logger.error("[%s] Agent execution error, routing to DLQ: %s", cid, exc)
            dlq_entry: dict = {
                "payload": payload,
                "reason": str(exc),
                "failed_at": str(time.time()),
            }
            if exc.response_body:
                dlq_entry["error_detail"] = exc.response_body[:4000]
            await redis_conn.xadd("relais:tasks:failed", dlq_entry)
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "correlation_id": cid,
                "sender_id": sid,
                "message": f"Agent execution error for {cid}: {exc}",
                "error": str(exc),
            })
            return True

        except Exception as exc:
            # Transient or unknown error — leave in PEL for re-delivery
            cid = envelope.correlation_id if envelope else message_id
            sid = envelope.sender_id if envelope else ""
            logger.error(
                "[%s] Unhandled exception, leaving in PEL: %s", cid, exc, exc_info=True
            )
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

        # Derive the XREAD start ID from the XADD return value so that any
        # Souvenir response arriving between XADD and the first XREAD is
        # never missed.  Using "$" would skip messages published in that gap.
        if isinstance(xadd_id, bytes):
            xadd_id = xadd_id.decode()
        last_id = _xadd_id_to_xread_start(xadd_id)

        # Poll loop: read entries from the response stream, filter by our
        # correlation_id, and stop as soon as the matching reply arrives or
        # the deadline is reached.  block_ms shrinks on each iteration so the
        # total wait never exceeds _MEMORY_TIMEOUT_SECONDS.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MEMORY_TIMEOUT_SECONDS

        while loop.time() < deadline:
            remaining = deadline - loop.time()
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
                                return res.get("messages", [])
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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _xadd_id_to_xread_start(xadd_id: str) -> str:
    """Convert an XADD return ID to a safe XREAD start ID.

    Returns a stream ID that is 1 millisecond before ``xadd_id``.  This
    ensures that a message published to the response stream *between* our
    XADD call and the first XREAD is never skipped — using the XADD timestamp
    verbatim (or "$") would miss it.

    Args:
        xadd_id: The ID string returned by redis XADD, e.g. "1711234567890-0".

    Returns:
        Start ID for XREAD, e.g. "1711234567889-0", or "0" on parse failure.
    """
    try:
        ts_ms, _ = xadd_id.split("-")
        return f"{int(ts_ms) - 1}-0"
    except (ValueError, AttributeError):
        return "0"


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
