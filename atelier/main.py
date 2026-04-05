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
  - relais:config:reload:atelier  (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:messages:outgoing_pending   — full reply envelope → Sentinelle (no messages_raw)
  - relais:messages:streaming:{channel}:{corr_id}  — streaming token chunks
  - relais:memory:request      — archive action with full messages_raw for Souvenir
  - relais:tasks:failed        — DLQ for exhausted retry attempts
  - relais:logs                — operational log entries

Configuration hot-reload
------------------------
Atelier watches configuration files for changes and reloads them without
restarting the service:

* Watched files: atelier/profiles.yaml, atelier/mcp_servers.yaml, atelier.yaml,
  channels.yaml
* Reload trigger: File system change detected via watchfiles library
  (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows)
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous config is preserved (no corruption)
* Redis Pub/Sub channel: relais:config:reload:atelier (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

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
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.contexts import (
    CTX_ATELIER,
    CTX_PORTAIL,
    CTX_SOUVENIR_REQUEST,
    AtelierCtx,
    PortailCtx,
    ensure_ctx,
)
from common.envelope_actions import ACTION_MEMORY_ARCHIVE
from common.redis_client import RedisClient  # noqa: F401 — kept for test namespace patching
from common.envelope import Envelope
from common.config_reload import watch_and_reload
from atelier.profile_loader import load_profiles, resolve_profile
from atelier.mcp_loader import load_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.agent_executor import AgentExecutor, AgentExecutionError, AgentResult
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.souvenir_backend import SouvenirBackend
from atelier.stream_publisher import StreamPublisher
from atelier.progress_config import load_progress_config, ProgressConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from common.config_loader import resolve_config_path, resolve_prompts_dir, resolve_skills_dir, resolve_storage_dir
from aiguilleur.channel_config import load_channels_config
from atelier.tool_policy import ToolPolicy
from atelier.subagents import SubagentRegistry
from atelier.tools._registry import ToolRegistry

logger = logging.getLogger("atelier")

# Directory containing soul/channels/roles/policies prompts.
# Resolved via the config cascade so users can override in ~/.relais/prompts/.
_PROMPTS_DIR: Path = resolve_prompts_dir()


class Atelier(BrickBase):
    """The Atelier brick — orchestrates DeepAgents-based LLM generation.

    Consumes tasks from ``relais:tasks``, calls the LLM via DeepAgents/LangChain
    (AgentExecutor), and publishes response envelopes.  Conversation history is
    managed by the persistent LangGraph checkpointer (AsyncSqliteSaver).

    Inherits from ``BrickBase`` which provides the stream loop, hot-reload,
    graceful shutdown, and structured logging infrastructure.
    """

    def __init__(self) -> None:
        """Initialise Atelier with Redis stream and consumer group config.

        Loads LLM profiles, MCP server configs, and skills tools once at
        startup to avoid repeated filesystem I/O on every message.
        """
        super().__init__("atelier")

        self.stream_in: str = "relais:tasks"
        self.group_name: str = "atelier_group"
        self.consumer_name: str = "atelier_1"

        # Config-dependent state — initialised via _load() so __init__ and
        # subsequent safe_reload() checkpoints share the same code path.
        self._profiles: dict = {}
        self._mcp_servers_default: dict = {}
        self._progress_config: ProgressConfig | None = None
        self._streaming_capable_channels: frozenset[str] = frozenset()
        self._load()

        # Base directory for role-based skill resolution — resolved once at
        # startup so _handle_message does not hit the filesystem on every message.
        # Tests may override this attribute after construction; _tool_policy is
        # a property that always derives from _skills_base_dir so the override
        # is automatically picked up.
        self._skills_base_dir: Path = resolve_skills_dir()

        # Static tool registry — discovers @tool-decorated functions in atelier/tools/.
        self._tool_registry: ToolRegistry = ToolRegistry.discover()

        # Subagent registry — loads specs from config/atelier/subagents/*/ directories in cascade.
        self._subagent_registry: SubagentRegistry = SubagentRegistry.load(self._tool_registry)

        # Persistent LangGraph checkpointer — owned by the Atelier singleton so
        # it survives across per-message AgentExecutor instances.  Uses a
        # dedicated SQLite file (checkpoints.db) separate from Souvenir's
        # memory.db to avoid table-name conflicts between LangGraph and SQLModel.
        # NOTE: from_conn_string() returns an async context manager; the live
        # saver instance is stored in self._checkpointer once start() enters it
        # via _extra_lifespan().
        checkpoints_db = str(resolve_storage_dir() / "checkpoints.db")
        self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(checkpoints_db)
        self._checkpointer: AsyncSqliteSaver | None = None

    # ------------------------------------------------------------------
    # BrickBase abstract interface implementation
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load Atelier's runtime configuration from disk.

        Refreshes ``_profiles``, ``_mcp_servers_default``, ``_progress_config``,
        and ``_streaming_capable_channels``.  Called once by ``__init__`` for the
        initial load.  Hot-reload goes through ``_build_config_candidate`` →
        ``_apply_config`` (which holds ``_config_lock``) instead.

        Does not touch Redis or any async resource.

        Raises:
            Any exception raised by the underlying loader functions (e.g.
            ``FileNotFoundError``, ``yaml.YAMLError``) propagates so that the
            caller (``__init__`` or ``safe_reload``) can handle it.
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
        logger.info("Atelier: configuration loaded from disk")

    def stream_specs(self) -> list[StreamSpec]:
        """Return the single StreamSpec for consuming ``relais:tasks``.

        Uses ``ack_mode="on_success"`` so messages stay in the PEL on
        transient errors (ConnectError, TimeoutException) and are
        re-delivered automatically.

        Returns:
            A list containing one ``StreamSpec`` for ``relais:tasks``.
        """
        return [
            StreamSpec(
                stream=self.stream_in,
                group=self.group_name,
                consumer=self.consumer_name,
                handler=self._handle_envelope,
                ack_mode="on_success",
                block_ms=2000,
                count=5,
            )
        ]

    # ------------------------------------------------------------------
    # BrickBase optional hooks
    # ------------------------------------------------------------------

    async def _extra_lifespan(self, stack: AsyncExitStack) -> None:
        """Enter the AsyncSqliteSaver context manager into the lifespan stack.

        Called by ``BrickBase.start()`` before stream loops are launched.
        Sets ``self._checkpointer`` so that ``_handle_message`` can pass it
        to ``AgentExecutor``.

        Args:
            stack: The ``AsyncExitStack`` used for the brick's lifespan.
        """
        self._checkpointer = await stack.enter_async_context(self._checkpointer_cm)

    def _build_config_candidate(self) -> dict:
        """Build a new configuration snapshot from disk without mutating self.

        Returns:
            A dict with keys ``profiles``, ``mcp_servers``, ``progress``,
            ``streaming_channels``, ``subagent_registry``.

        Raises:
            Any exception from the underlying loaders.
        """
        new_profiles = load_profiles()
        new_mcp_servers = load_for_sdk()
        new_progress = load_progress_config()
        new_streaming = frozenset(
            name for name, cfg in load_channels_config().items() if cfg.streaming
        )
        new_subagent_registry = SubagentRegistry.load(self._tool_registry)
        return {
            "profiles": new_profiles,
            "mcp_servers": new_mcp_servers,
            "progress": new_progress,
            "streaming_channels": new_streaming,
            "subagent_registry": new_subagent_registry,
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
        if "subagent_registry" in cfg:
            self._subagent_registry = cfg["subagent_registry"]
        logger.info("Atelier: configuration applied")

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths and directories to watch.

        Returns all four Atelier config files plus any existing
        ``config/atelier/subagents/`` directories found across the config cascade,
        so changes to subagent YAML files trigger a hot-reload.

        Returns:
            A list of resolved Path instances.
        """
        from common.config_loader import CONFIG_SEARCH_PATH

        paths = [
            resolve_config_path("atelier/profiles.yaml"),
            resolve_config_path("atelier/mcp_servers.yaml"),
            resolve_config_path("atelier.yaml"),
            resolve_config_path("channels.yaml"),
        ]
        # Add existing subagents directories from all cascade roots
        for base in CONFIG_SEARCH_PATH:
            subagents_dir = base / "config" / "atelier" / "subagents"
            if subagents_dir.is_dir():
                paths.append(subagents_dir)
        return paths

    def _start_file_watcher(self, shutdown_event: asyncio.Event | None = None) -> "asyncio.Task | None":
        """Create and return an asyncio.Task that watches config files for changes.

        Overrides ``BrickBase._start_file_watcher`` to accept ``shutdown_event``
        as an optional keyword argument (tests call this method without arguments).
        Returns None when watchfiles is not installed (hot-reload gracefully
        degrades to Redis Pub/Sub only).

        Args:
            shutdown_event: Optional event used by the base class interface;
                ignored here as ``watch_and_reload`` manages its own lifecycle.

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

    async def reload_config(self) -> bool:
        """Hot-reload Atelier's YAML configuration without interrupting message processing.

        Overrides ``BrickBase.reload_config()`` to call ``safe_reload`` directly
        with the builder callable so that loader exceptions are caught by
        ``safe_reload`` and the previous configuration is preserved on failure.

        Returns:
            True when the configuration was reloaded successfully.
            False when the reload failed (previous config preserved).
        """
        from common.config_reload import safe_reload

        return await safe_reload(
            self._config_lock,
            "atelier",
            self._build_config_candidate,
            self._apply_config,
            checkpoint_paths=self._config_watch_paths(),
        )

    async def _config_reload_listener(
        self,
        redis_conn: Any,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        """Subscribe to ``relais:config:reload:atelier`` and trigger hot-reloads.

        Overrides ``BrickBase._config_reload_listener`` to preserve backward
        compatibility with tests that call this method without ``shutdown_event``.
        Only the exact string ``"reload"`` triggers a config reload; all other
        messages are silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
            shutdown_event: Optional event to signal loop termination.  When
                ``None`` (e.g. in unit tests where ``listen()`` returns a
                finite iterator), the loop runs until the iterator is exhausted.
        """
        pubsub = redis_conn.pubsub()
        channel = "relais:config:reload:atelier"
        await pubsub.subscribe(channel)
        logger.info("Atelier: subscribed to %s", channel)

        async for message in pubsub.listen():
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                logger.info("Atelier: received reload signal — reloading config")
                await self.reload_config()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _tool_policy(self) -> ToolPolicy:
        """Return a ToolPolicy rooted at the current _skills_base_dir.

        Constructed on each access so that test overrides of _skills_base_dir
        are reflected immediately without requiring a separate setter.

        Returns:
            A fresh ToolPolicy instance bound to self._skills_base_dir.
        """
        return ToolPolicy(base_dir=self._skills_base_dir)

    # ------------------------------------------------------------------
    # Core message handler
    # ------------------------------------------------------------------

    async def _handle_envelope(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Process a single task envelope from the Redis stream.

        Calls the SDK executor and publishes the response.  Routes
        AgentExecutionError payloads to the DLQ.

        Args:
            envelope: Deserialized ``Envelope`` received from the stream loop.
            redis_conn: Active Redis connection.

        Returns:
            True when the message should be ACKed (success or DLQ routing).
            False when a transient error occurred and the message should
            remain in the PEL for re-delivery.
        """
        logger.debug("_handle_envelope correlation_id: %s", envelope.correlation_id)
        try:
            logger.info(
                "[%s] Processing task for %s",
                envelope.correlation_id,
                envelope.sender_id,
            )

            # 1. Resolve LLM profile — read from CTX_PORTAIL stamped by Portail
            portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore[assignment]
            ur: dict = portail_ctx.get("user_record") or {}
            profile_name = portail_ctx.get("llm_profile") or "default"
            profile = resolve_profile(self._profiles, profile_name)

            # Resolve unique user_id for SouvenirBackend
            user_id: str = portail_ctx.get("user_id") or envelope.sender_id

            # 2. Assemble soul system prompt
            soul_prompt = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                role_prompt_path=ur.get("role_prompt_path"),
                user_prompt_path=ur.get("prompt_path"),
                channel_prompt_path=portail_ctx.get("channel_prompt_path"),
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
                # Pass mcp_tools as request_tools so mcp:<glob> and inherit
                # tokens resolve against the per-request ToolPolicy-filtered pool.
                subagents = self._subagent_registry.specs_for_user(ur, request_tools=mcp_tools)
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
            atelier_ctx = ensure_ctx(response_env, CTX_ATELIER)
            atelier_ctx["user_message"] = envelope.content
            if streaming:
                await stream_pub.finalize()
                atelier_ctx["streamed"] = True
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = "relais:messages:outgoing_pending"
            await redis_conn.xadd(out_stream, {"payload": response_env.to_json()})

            # Archive the turn directly to Souvenir via relais:memory:request.
            # messages_raw stays off the outgoing envelope to avoid serializing
            # the full conversation history through every downstream consumer.
            # The Envelope format is required: Souvenir's BrickBase loop parses
            # each message via Envelope.from_json(), and action + parameters are
            # carried in envelope.action and envelope.context[CTX_SOUVENIR_REQUEST].
            archive_env = Envelope(
                content="",
                sender_id=f"atelier:{envelope.sender_id}",
                channel="internal",
                session_id=envelope.session_id,
                correlation_id=envelope.correlation_id,
                action=ACTION_MEMORY_ARCHIVE,
                context={CTX_SOUVENIR_REQUEST: {
                    "envelope_json": response_env.to_json(),
                    "messages_raw": agent_result.messages_raw,
                }},
            )
            await redis_conn.xadd(
                "relais:memory:request",
                {"payload": archive_env.to_json()},
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
            logger.error("[%s] Agent execution error, routing to DLQ: %s", envelope.correlation_id, exc)
            dlq_entry: dict = {
                "payload": envelope.to_json(),
                "reason": str(exc),
                "failed_at": str(time.time()),
            }
            if exc.response_body:
                dlq_entry["error_detail"] = exc.response_body[:4000]
            await redis_conn.xadd("relais:tasks:failed", dlq_entry)
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": f"Agent execution error for {envelope.correlation_id}: {exc}",
                "error": str(exc),
            })
            return True

        except Exception as exc:
            # Transient or unknown error — leave in PEL for re-delivery
            logger.error(
                "[%s] Unhandled exception, leaving in PEL: %s", envelope.correlation_id, exc, exc_info=True
            )
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": f"Unhandled exception for {envelope.correlation_id}: {exc}",
                "error": str(exc),
            })
            return False


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
