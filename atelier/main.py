"""Atelier brick — DeepAgents-based LLM execution pipeline.

Functional role
---------------
Executes the agentic LLM loop for each authorized task.  Fetches short-term
context from Souvenir, assembles the system prompt (soul + role + user layers),
runs the DeepAgents loop with MCP and internal tools, streams token-by-token
output to the channel, and publishes the final reply for Sentinelle to deliver.

Technical overview
------------------
Key classes:

* ``AgentExecutor`` (atelier.agent_executor) — orchestrates a single
  ``deepagents.create_deep_agent()`` call; handles streaming via
  ``agent.astream(stream_mode=["updates", "messages"], subgraphs=True,
  version="v2")``; accepts an optional ``backend: BackendProtocol`` (for
  DeepAgents persistent memory) and ``progress_callback`` forwarded from
  the caller.
* ``McpSessionManager`` (atelier.mcp_session_manager) — manages stdio/SSE MCP
  server lifecycle across requests.
* ``ToolPolicy`` (atelier.tool_policy) — resolves skill directories per role
  and filters MCP tool definitions (enforces ``mcp_max_tools``).
* ``SoulAssembler`` (atelier.soul_assembler) — assembles the multi-layer
  system prompt from soul / role / user / channel / policy prompt files.
* ``ProfileConfig`` — loaded from atelier/profiles.yaml; selects model, temperature,
  max_tokens, mcp_timeout, mcp_max_tools per request.
* ``StreamPublisher`` — publishes streaming entries to
  ``relais:messages:streaming:{channel}:{corr_id}`` (type ``token`` for text
  chunks) and progress events (type ``progress``) to both the streaming stream
  and ``relais:messages:outgoing:{channel}`` for real-time rendering.
* ``SouvenirBackend`` (atelier.souvenir_backend) — ``BackendProtocol`` impl
  that routes ``/memories/`` paths to the Souvenir brick via Redis.

Redis channels
--------------
Consumed:
  - relais:tasks               (consumer group: atelier_group)

Produced:
  - relais:messages:outgoing_pending   — full reply envelope → Sentinelle
  - relais:messages:streaming:{channel}:{corr_id}  — streaming token chunks
  - relais:tasks:failed        — DLQ for exhausted retry attempts
  - relais:memory:request      — context fetch request → Souvenir
  - relais:logs                — operational log entries

Read:
  - relais:memory:response     — context history returned by Souvenir

Message flow (one task at a time):

  relais:tasks
    │  (1) deserialise Envelope, resolve ProfileConfig
    │  (2) request context from Souvenir  ──► relais:memory:request
    │                                     ◄── relais:memory:response
    │  (3) assemble soul system prompt (SoulAssembler)
    │  (4) start MCP sessions + build LangChain tools (McpSessionManager +
    │      ToolPolicy)
    │  (5) AgentExecutor.execute(backend=SouvenirBackend, progress_callback=…)
    │      ├── token chunks   ──► relais:messages:streaming:{channel}:{corr_id}
    │      ├── progress events ─► relais:messages:streaming + relais:messages:outgoing:{channel}
    │      └── full reply
    │  (6) publish response Envelope
    └──► relais:messages:outgoing_pending

XACK contract:
  - Return True  → ACK (success or final error routed to relais:tasks:failed)
  - Return False → no ACK (transient ConnectError / TimeoutException; message
    stays in PEL for automatic re-delivery)

For streaming-capable channels each text chunk is also published via
StreamPublisher for real-time rendering before the full reply is ready.
Discord receives a final reply only, with live progress events (tool_call,
tool_result, subagent_start) sent to ``relais:messages:outgoing:{channel}``
as ``message_type=progress`` envelopes.  Publishing is governed by
``ProgressConfig`` (atelier.yaml ``progress:`` section).
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
from atelier.agent_executor import AgentExecutor, AgentExecutionError, AgentResult
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.souvenir_backend import SouvenirBackend
from atelier.stream_publisher import StreamPublisher
from atelier.progress_config import load_progress_config
from common.config_loader import resolve_prompts_dir, resolve_skills_dir
from aiguilleur.channel_config import load_channels_config
from atelier.tool_policy import ToolPolicy

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
        self._progress_config = load_progress_config()

        # Channels that support incremental chunk streaming — loaded here
        # (not at module level) so tests can import atelier.main without
        # triggering filesystem I/O before any fixture is in place.
        self._streaming_capable_channels: frozenset[str] = frozenset(
            name for name, cfg in load_channels_config().items() if cfg.streaming
        )

        # Base directory for role-based skill resolution — resolved once at
        # startup so _handle_message does not hit the filesystem on every message.
        # Tests may override this attribute after construction; _tool_policy is
        # a property that always derives from _skills_base_dir so the override
        # is automatically picked up.
        self._skills_base_dir: Path = resolve_skills_dir()

    @property
    def _tool_policy(self) -> ToolPolicy:
        """Return a ToolPolicy rooted at the current _skills_base_dir.

        Constructed on each access so that test overrides of _skills_base_dir
        are reflected immediately without requiring a separate setter.

        Returns:
            A fresh ToolPolicy instance bound to self._skills_base_dir.
        """
        return ToolPolicy(base_dir=self._skills_base_dir)


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

            # 1. Resolve LLM profile — read from user_record dict stamped by Portail
            ur: dict = envelope.metadata.get("user_record") or {}
            profile_name = ur.get("llm_profile") or "default"
            profile = resolve_profile(self._profiles, profile_name)

            # Resolve unique user_id for SouvenirBackend (shortcut in metadata)
            user_id: str = envelope.metadata.get("user_id") or envelope.sender_id

            # 2. Request context from Souvenir
            context = await self._fetch_context(redis_conn, envelope)

            # 3. Assemble soul system prompt
            soul_prompt = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                channel=envelope.channel,
                user_prompt_path=ur.get("prompt_path"),
                user_role=ur.get("role"),
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

            # 5a. Resolve per-user skills and MCP tool policy from user_record
            # dict stamped by Portail (consolidated identity envelope).
            skills = self._tool_policy.resolve_skills(
                ur.get("skills_dirs")
            )
            mcp_patterns = self._tool_policy.parse_mcp_patterns(
                ur.get("allowed_mcp_tools")
            )

            session_manager = McpSessionManager(profile, mcp_servers)
            async with contextlib.AsyncExitStack() as stack:
                if mcp_patterns:
                    await session_manager.start_all(stack)
                    mcp_tools = self._tool_policy.filter_mcp_tools(
                        await make_mcp_tools(session_manager),
                        ur.get("allowed_mcp_tools"),
                    )
                else:
                    mcp_tools = []

                agent_executor = AgentExecutor(
                    profile=profile,
                    soul_prompt=soul_prompt,
                    tools=mcp_tools,
                    skills=skills,
                    backend=SouvenirBackend(user_id=user_id),
                )

                # 7. Execute — streaming only for capable channels; progress for all.
                streaming = envelope.channel in self._streaming_capable_channels
                stream_pub = StreamPublisher(
                    redis_conn,
                    channel=envelope.channel,
                    correlation_id=envelope.correlation_id,
                    source_envelope=envelope,
                    progress_config=self._progress_config,
                )
                if streaming:
                    await redis_conn.publish(
                        f"relais:streaming:start:{envelope.channel}",
                        envelope.to_json(),
                    )

                agent_result = await agent_executor.execute(
                    envelope=envelope,
                    context=context,
                    stream_callback=stream_pub.push_chunk if streaming else None,
                    progress_callback=stream_pub.push_progress,
                )
            # MCP sessions closed; finalize stream and publish response.
            reply_text = agent_result.reply_text

            # 8. Build and publish response envelope.
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.metadata["user_message"] = envelope.content
            response_env.metadata["messages_raw"] = agent_result.messages_raw
            if streaming:
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
            except Exception as exc:
                logger.warning("Error reading memory response: %s", exc)
                break

            next_last_id, messages = _extract_matching_memory_messages(
                results,
                correlation_id,
            )
            if next_last_id is not None:
                last_id = next_last_id
            if messages is not None:
                return messages

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


def _extract_matching_memory_messages(
    results: Any,
    correlation_id: str,
) -> tuple[str | None, list[dict] | None]:
    """Extract the newest stream ID and matching memory payload from XREAD data.

    Args:
        results: Raw Redis XREAD response.
        correlation_id: Request correlation ID to match.

    Returns:
        Tuple of (last_seen_id, messages). ``messages`` is None when no
        matching response is found in the batch.
    """
    last_seen_id: str | None = None

    for _, stream_messages in results or []:
        for msg_id, data in stream_messages:
            last_seen_id = msg_id
            try:
                response = json.loads(data.get("payload", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            if response.get("correlation_id") == correlation_id:
                return last_seen_id, response.get("messages", [])

    return last_seen_id, None


# ---------------------------------------------------------------------------
# Backward-compat shims (deprecated — use atelier.tool_policy.ToolPolicy)
# ---------------------------------------------------------------------------
# These thin wrappers re-expose the three functions that previously lived
# here as module-level helpers.  External tests that import them directly
# from atelier.main continue to work unchanged.


def _parse_tool_policy(raw: object) -> tuple[str, ...]:
    """Normalise a metadata value into a tuple of strings.

    .. deprecated::
        Import from ``atelier.tool_policy.ToolPolicy`` instead.

    Args:
        raw: The raw value from ``envelope.metadata``.

    Returns:
        A tuple of strings, never None.
    """
    return ToolPolicy._parse_policy(raw)


def _resolve_skills_paths(skills_dirs: tuple[str, ...], base: Path) -> list[str]:
    """Expand skill directory specs into existing absolute paths.

    .. deprecated::
        Use ``ToolPolicy(base_dir=base).resolve_skills(list(skills_dirs))``.

    Args:
        skills_dirs: Tuple of directory names or ``"*"`` wildcard.
        base: The base skills directory.

    Returns:
        List of absolute path strings for directories that exist on disk.
    """
    return ToolPolicy(base_dir=base)._resolve_paths(skills_dirs)


def _filter_mcp_tools(tools: list, patterns: tuple[str, ...]) -> list:
    """Filter tools by fnmatch patterns.

    .. deprecated::
        Use ``ToolPolicy(base_dir=Path()).filter_mcp_tools(tools, list(patterns))``.

    Args:
        tools: List of LangChain ``BaseTool`` instances.
        patterns: Tuple of fnmatch-style glob patterns.

    Returns:
        Filtered list of tools.
    """
    return ToolPolicy._filter_tools(tools, patterns)


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
