"""Atelier brick — DeepAgents-based LLM execution pipeline.

Functional role
---------------
Executes the agentic LLM loop for each authorized task.  Conversation history
is managed natively by the persistent LangGraph checkpointer (AsyncSqliteSaver,
``checkpoints.db``), keyed by ``user_id``.  Assembles the system prompt (soul +
role + user layers), runs the DeepAgents loop with MCP and internal tools,
streams token-by-token output to the channel, and publishes the final reply for
Sentinelle to deliver.

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
  - relais:messages:outgoing_pending   — full reply envelope → Sentinelle (no messages_raw)
  - relais:messages:streaming:{channel}:{corr_id}  — streaming token chunks
  - relais:memory:request      — archive action with full messages_raw for Souvenir
  - relais:tasks:failed        — DLQ for exhausted retry attempts
  - relais:logs                — operational log entries

Message flow (one task at a time):

  relais:tasks
    │  (1) deserialise Envelope, resolve ProfileConfig
    │  (2) assemble soul system prompt (SoulAssembler)
    │  (3) start MCP sessions + build LangChain tools (McpSessionManager +
    │      ToolPolicy)
    │  (4) AgentExecutor.execute(backend=SouvenirBackend, progress_callback=…)
    │      ├── token chunks   ──► relais:messages:streaming:{channel}:{corr_id}
    │      ├── progress events ─► relais:messages:streaming + relais:messages:outgoing:{channel}
    │      └── AgentResult(reply_text, messages_raw)  ← full turn captured via aget_state()
    │  (5) build response Envelope (without messages_raw to avoid serializing full
    │      conversation history through every downstream consumer)
    │  (6) publish archive action to relais:memory:request with envelope + messages_raw
    │      (Souvenir processes this to persist the full LangChain message history)
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
from common.config_reload import safe_reload, watch_and_reload
from atelier.profile_loader import load_profiles, resolve_profile
from atelier.mcp_loader import load_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.agent_executor import AgentExecutor, AgentExecutionError, AgentResult
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.souvenir_backend import SouvenirBackend
from atelier.stream_publisher import StreamPublisher
from atelier.progress_config import load_progress_config
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from common.config_loader import resolve_config_path, resolve_prompts_dir, resolve_skills_dir, resolve_storage_dir
from aiguilleur.channel_config import load_channels_config
from atelier.tool_policy import ToolPolicy
from atelier.agents import SubagentRegistry

logger = logging.getLogger("atelier")

# Directory containing soul/channels/roles/policies prompts.
# Resolved via the config cascade so users can override in ~/.relais/prompts/.
_PROMPTS_DIR: Path = resolve_prompts_dir()


class Atelier:
    """The Atelier brick — orchestrates DeepAgents-based LLM generation.

    Consumes tasks from ``relais:tasks``, calls the LLM via DeepAgents/LangChain
    (AgentExecutor), and publishes response envelopes.  Conversation history is
    managed by the persistent LangGraph checkpointer (AsyncSqliteSaver).
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

        # Subagent registry — auto-discovers modules in atelier/agents/.
        self._subagent_registry: SubagentRegistry = SubagentRegistry.discover()

        # Config reload lock — guards _profiles, _mcp_servers_default,
        # _progress_config, and _streaming_capable_channels.
        self._config_lock: asyncio.Lock = asyncio.Lock()

        # Persistent LangGraph checkpointer — owned by the Atelier singleton so
        # it survives across per-message AgentExecutor instances.  Uses a
        # dedicated SQLite file (checkpoints.db) separate from Souvenir's
        # memory.db to avoid table-name conflicts between LangGraph and SQLModel.
        # NOTE: from_conn_string() returns an async context manager; the live
        # saver instance is stored in self._checkpointer once start() enters it.
        checkpoints_db = str(resolve_storage_dir() / "checkpoints.db")
        self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(checkpoints_db)
        self._checkpointer: AsyncSqliteSaver | None = None

    def _load(self) -> None:
        """Reload Atelier's runtime configuration from disk.

        Refreshes ``_profiles``, ``_mcp_servers_default``, ``_progress_config``,
        and ``_streaming_capable_channels``.  Called by ``__init__`` is a no-op
        pattern here — ``__init__`` loads these directly; ``_load()`` is
        provided so ``reload_config()`` can call it through ``safe_reload``.

        This method is the single authoritative entry point for loading Atelier's
        mutable configuration.  Does not touch Redis or any async resource.

        Raises:
            Any exception raised by the underlying loader functions (e.g.
            ``FileNotFoundError``, ``yaml.YAMLError``) propagates so that
            ``safe_reload`` can intercept it and preserve the current state.
        """
        new_profiles = load_profiles()
        new_mcp_servers = load_for_sdk()
        new_progress = load_progress_config()
        new_streaming = frozenset(
            name for name, cfg in load_channels_config().items() if cfg.streaming
        )
        self._profiles = new_profiles
        self._mcp_servers_default = new_mcp_servers
        self._progress_config = new_progress
        self._streaming_capable_channels = new_streaming
        logger.info("Atelier: configuration reloaded from disk")

    def _build_config_candidate(self) -> dict:
        """Build a new configuration snapshot from disk without mutating self.

        Returns:
            A dict with keys ``profiles``, ``mcp_servers``, ``progress``,
            ``streaming_channels``.

        Raises:
            Any exception from the underlying loaders.
        """
        new_profiles = load_profiles()
        new_mcp_servers = load_for_sdk()
        new_progress = load_progress_config()
        new_streaming = frozenset(
            name for name, cfg in load_channels_config().items() if cfg.streaming
        )
        return {
            "profiles": new_profiles,
            "mcp_servers": new_mcp_servers,
            "progress": new_progress,
            "streaming_channels": new_streaming,
        }

    def _apply_config(self, cfg: dict) -> None:
        """Swap in a freshly loaded configuration snapshot.

        Args:
            cfg: Dict returned by ``_build_config_candidate``.
        """
        self._profiles = cfg["profiles"]
        self._mcp_servers_default = cfg["mcp_servers"]
        self._progress_config = cfg["progress"]
        self._streaming_capable_channels = cfg["streaming_channels"]
        logger.info("Atelier: configuration applied")

    async def reload_config(self) -> bool:
        """Hot-reload Atelier's YAML configuration without interrupting message processing.

        Uses ``safe_reload`` to guarantee that the previous configuration is
        preserved if any loader raises.

        Returns:
            True when the configuration was reloaded successfully.
            False when the reload failed (previous config preserved).
        """
        return await safe_reload(
            self._config_lock,
            "atelier",
            self._build_config_candidate,
            self._apply_config,
            checkpoint_paths=self._config_watch_paths(),
        )

    async def _config_reload_listener(self, redis_conn: Any) -> None:
        """Subscribe to ``relais:config:reload:atelier`` and trigger hot-reloads.

        Runs as a background asyncio task alongside ``_process_stream``.
        Only the exact string ``"reload"`` triggers a config reload; all other
        messages are silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
        """
        pubsub = redis_conn.pubsub()
        channel = "relais:config:reload:atelier"
        await pubsub.subscribe(channel)
        logger.info("Atelier: subscribed to %s", channel)

        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                logger.info("Atelier: received reload signal — reloading config")
                await self.reload_config()

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths to watch for changes.

        Resolves all four Atelier config files via the config cascade:
        profiles.yaml, mcp_servers.yaml, atelier.yaml, and channels.yaml.

        Returns:
            A list of four resolved Path instances.
        """
        return [
            resolve_config_path("atelier/profiles.yaml"),
            resolve_config_path("atelier/mcp_servers.yaml"),
            resolve_config_path("atelier.yaml"),
            resolve_config_path("channels.yaml"),
        ]

    def _start_file_watcher(self) -> "asyncio.Task | None":
        """Create and return an asyncio.Task that watches config files for changes.

        Returns None when watchfiles is not installed (hot-reload gracefully
        degrades to Redis Pub/Sub only).

        Returns:
            An asyncio.Task running watch_and_reload, or None when watchfiles
            is unavailable.
        """
        from common.config_reload import watchfiles as _wf
        if _wf is None:
            logger.warning(
                "Atelier: watchfiles not installed — file-based hot-reload disabled. "
                "Install with: pip install watchfiles"
            )
            return None
        return asyncio.create_task(
            watch_and_reload(self._config_watch_paths(), self.reload_config, "atelier")
        )

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

        Calls the SDK executor and publishes the response.  Routes
        SDKExecutionError payloads to the DLQ.

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

            # 1. Resolve LLM profile — read from top-level metadata stamped by Portail
            ur: dict = envelope.metadata.get("user_record") or {}
            profile_name = envelope.metadata.get("llm_profile") or "default"
            profile = resolve_profile(self._profiles, profile_name)

            # Resolve unique user_id for SouvenirBackend (shortcut in metadata)
            user_id: str = envelope.metadata.get("user_id") or envelope.sender_id

            # 2. Assemble soul system prompt
            soul_prompt = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                role_prompt_path=ur.get("role_prompt_path"),
                user_prompt_path=ur.get("prompt_path"),
                channel_prompt_path=envelope.metadata.get("channel_prompt_path"),
            )

            # 3. Select MCP servers for this profile.
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

                # Resolve subagents and delegation prompt from the registry,
                # filtered by the user's allowed_subagents patterns.
                subagents = self._subagent_registry.specs_for_user(ur)
                delegation_prompt = self._subagent_registry.delegation_prompt_for_user(ur)

                agent_executor = AgentExecutor(
                    profile=profile,
                    soul_prompt=soul_prompt,
                    tools=mcp_tools,
                    skills=skills,
                    backend=SouvenirBackend(user_id=user_id),
                    checkpointer=self._checkpointer,
                    subagents=subagents,
                    delegation_prompt=delegation_prompt,
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
                    stream_callback=stream_pub.push_chunk if streaming else None,
                    progress_callback=stream_pub.push_progress,
                )
            # MCP sessions closed; finalize stream and publish response.
            reply_text = agent_result.reply_text

            # 8. Build and publish response envelope.
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.metadata["user_message"] = envelope.content
            if streaming:
                await stream_pub.finalize()
                response_env.metadata["streamed"] = True
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = "relais:messages:outgoing_pending"
            await redis_conn.xadd(out_stream, {"payload": response_env.to_json()})

            # Archive the turn directly to Souvenir via relais:memory:request.
            # messages_raw stays off the outgoing envelope to avoid serializing
            # the full conversation history through every downstream consumer.
            await redis_conn.xadd(
                "relais:memory:request",
                {
                    "payload": json.dumps({
                        "action": "archive",
                        "envelope_json": response_env.to_json(),
                        "messages_raw": agent_result.messages_raw,
                    })
                },
            )

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
        async with self._checkpointer_cm as checkpointer:
            self._checkpointer = checkpointer
            reload_listener_task = asyncio.create_task(
                self._config_reload_listener(redis_conn)
            )
            watcher_task = self._start_file_watcher()
            try:
                await self._process_stream(redis_conn, shutdown=shutdown)
            except asyncio.CancelledError:
                logger.info("Atelier shutting down...")
            finally:
                reload_listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reload_listener_task
                if watcher_task is not None:
                    watcher_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await watcher_task
                self._checkpointer = None
                await self.client.close()
                logger.info("Atelier stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
