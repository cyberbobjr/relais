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
* ``McpSessionManager`` (atelier.mcp_session_manager) — **singleton** managing
  stdio/SSE MCP server lifecycle; started once at brick startup via ``start()``,
  shared across all requests; per-server ``asyncio.Lock`` serializes stdio pipe
  calls; dead sessions (``BrokenPipeError``, ``ConnectionError``, ``EOFError``)
  evicted and re-established on next call; closed on shutdown via ``close()``.
* ``ToolPolicy`` (atelier.tool_policy) — resolves skill directories per role
  and filters MCP tool definitions (enforces ``mcp_max_tools``).
* ``SoulAssembler`` (atelier.soul_assembler) — assembles the multi-layer
  system prompt from soul / role / user / channel / policy prompt files.
* ``ProfileConfig`` — loaded from ``common/profile_loader.py`` (config file
  ``atelier/profiles.yaml``); selects model, temperature,
  max_tokens, mcp_timeout, mcp_max_tools per request.  Optional field
  ``parallel_tool_calls: bool | None`` forwards the OpenAI-compatible
  ``parallel_tool_calls`` parameter to the model (useful to disable it for
  providers like Mistral that emit broken parallel calls).  When ``None``
  (default) the parameter is not forwarded and the provider default applies.
* ``StreamPublisher`` — publishes streaming entries to
  ``relais:messages:streaming:{channel}:{corr_id}`` (type ``token`` for text
  chunks) and progress events (type ``progress``) to both the streaming stream
  and ``relais:messages:outgoing:{channel}`` for real-time rendering.
* ``SouvenirBackend`` (atelier.souvenir_backend) — ``BackendProtocol`` impl
  that routes ``/memories/`` paths to the Souvenir brick via Redis.
* ``ErrorSynthesizer`` (atelier.error_synthesizer) — on ``AgentExecutionError``,
  performs a lightweight LLM call with the partial conversation history
  (captured in ``AgentExecutionError.messages_raw``) to produce an empathetic
  user-visible error reply.  Falls back to a static message on LLM failure.
* ``ToolErrorGuard`` (atelier.agent_executor) — tracks consecutive and total
  tool errors during the agentic loop; raises ``AgentExecutionError`` when
  limits are exceeded to prevent runaway loops.
* ``StreamBuffer`` (atelier.agent_executor) — accumulates text tokens and
  flushes to a callback when a character threshold is reached.

Redis channels
--------------
Consumed:
  - relais:tasks               (consumer group: atelier_group)
  - relais:config:reload:atelier  (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:messages:outgoing_pending   — full reply envelope → Sentinelle (no messages_raw).
                                  Also used for synthesized error replies: on AgentExecutionError,
                                  ``ErrorSynthesizer`` (atelier/error_synthesizer.py) performs a
                                  lightweight LLM call with the partial conversation history
                                  (``AgentExecutionError.messages_raw``) and publishes an
                                  empathetic error message so the user is not left in silence.
  - relais:messages:streaming:{channel}:{corr_id}  — streaming token chunks
  - relais:memory:request      — archive action with full messages_raw for Souvenir
  - relais:skill:trace         — per-turn skill execution trace → Forgeron (fire-and-forget,
                                  only when skills were used).  Published in two cases:
                                  (a) after a successful turn when tool_call_count > 0;
                                  (b) on the DLQ path (AgentExecutionError) with
                                  tool_error_count=-1 (sentinel: aborted turn) and
                                  messages_raw=exc.messages_raw (partial conversation
                                  captured from the graph state).
                                  context[CTX_SKILL_TRACE] carries skill_names,
                                  tool_call_count, tool_error_count, messages_raw
  - relais:tasks:failed        — DLQ for exhausted retry attempts
  - relais:logs                — operational log entries

Configuration hot-reload
------------------------
Atelier watches configuration files for changes and reloads them without
restarting the service:

* Watched files: atelier/profiles.yaml, atelier/mcp_servers.yaml, atelier.yaml
* Reload trigger: File system change detected via watchfiles library
  (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows)
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous config is preserved (no corruption)
* Redis Pub/Sub channel: relais:config:reload:atelier (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

Message flow (one task at a time):

  relais:tasks
    │  (1) deserialise Envelope, resolve ProfileConfig from CTX_PORTAIL
    │  (2) assemble soul system prompt (SoulAssembler)
    │  (3) resolve per-user skills dirs and MCP patterns via ToolPolicy
    │  (4) read pre-built MCP tools from singleton McpSessionManager (under
    │      _mcp_lock); apply ToolPolicy.filter_mcp_tools() per allowed_mcp_tools
    │  (5) resolve subagents + delegation prompt from SubagentRegistry,
    │      filtered by user's allowed_subagents patterns (fnmatch)
    │  (6) AgentExecutor.execute(profile, soul_prompt, mcp_tools, skills,
    │      backend=SouvenirBackend, checkpointer, subagents, delegation_prompt,
    │      progress_callback=…)
    │      ├── token chunks    ──► relais:messages:streaming:{channel}:{corr_id}
    │      ├── progress events ──► relais:messages:streaming + relais:messages:outgoing:{channel}
    │      └── AgentResult(reply_text, messages_raw, tool_call_count, tool_error_count)
    │          ← full turn captured via aget_state()
    │  (7) if skills_used and tool_call_count > 0 → publish ACTION_SKILL_TRACE envelope
    │      to relais:skill:trace (fire-and-forget → Forgeron stores the trace)
    │      └── Forgeron handles changelog + consolidation autonomously
    │  (8) build response Envelope; stamp context["atelier"]["skills_used"] if any;
    │      publish archive to relais:memory:request (Souvenir persists full LangChain history)
    └──► relais:messages:outgoing_pending

Loop guard (AgentExecutor):
  ``ToolErrorGuard`` tracks tool errors during the agentic loop.  If the same
  named tool returns ``status="error"`` 5 consecutive times, or if 8 total
  errors accumulate across all tools, ``AgentExecutor.execute()`` raises
  ``AgentExecutionError`` to abort the request and prevent infinite tool-error
  loops (e.g. Mistral parallel-tool-call bug with ``write_todos``).  The total
  limit (8) is intentionally higher than the consecutive limit (5): the system
  prompt includes a ``SELF_DIAGNOSIS_PROMPT`` that instructs the agent to stop
  and re-read the SKILL.md troubleshooting sections after repeated errors
  instead of blindly retrying — the extra headroom lets the self-diagnosis
  loop actually exercise the fix.  Unnamed tools (name == "?") are excluded
  from consecutive grouping to avoid false positives.  On
  ``AgentExecutionError``, the partial conversation state is captured into
  ``exc.messages_raw`` and forwarded to both ``ErrorSynthesizer`` (user-
  visible error reply) and Forgeron (skill improvement trace with full
  conversation context).

XACK contract:
  - Return True  → ACK (success, or AgentExecutionError/ExhaustedRetriesError
    routed to relais:tasks:failed)
  - Return False → no ACK (unexpected infrastructure errors — e.g. Redis
    ConnectError outside the executor; message stays in PEL for re-delivery)

Note: LLM provider transient errors (RateLimitError, InternalServerError, etc.)
are now retried internally by ``AgentExecutor.execute()`` using the profile's
``resilience`` config (retry_attempts + retry_delays).  After all attempts are
exhausted, ``ExhaustedRetriesError`` (a subclass of ``AgentExecutionError``) is
raised — which routes the message to the DLQ and ACKs it, preventing indefinite
PEL poisoning.

Streaming mode is determined by ``context["aiguilleur"]["streaming"]`` (stamped
by the channel adapter from ``ChannelConfig.streaming`` in aiguilleur.yaml) —
Atelier no longer loads aiguilleur.yaml per-request.  For streaming-capable
channels each text chunk is also published via StreamPublisher for real-time
rendering before the full reply is ready.  Discord receives a final reply only,
with live progress events (tool_call, tool_result, subagent_start) sent to
``relais:messages:outgoing:{channel}`` as ``message_type=progress`` envelopes.
Publishing is governed by ``ProgressConfig`` (atelier.yaml ``progress:`` section).
"""

import asyncio
import contextlib
import json
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.streams import (
    STREAM_LOGS,
    STREAM_MEMORY_REQUEST,
    STREAM_OUTGOING_PENDING,
    STREAM_SKILL_TRACE,
    STREAM_TASKS,
    STREAM_TASKS_FAILED,
)
from common.contexts import (
    CTX_AIGUILLEUR,
    CTX_ATELIER,
    CTX_PORTAIL,
    CTX_SKILL_TRACE,
    CTX_SOUVENIR_REQUEST,
    AiguilleurCtx,
    PortailCtx,
    ensure_ctx,
)
from common.envelope_actions import ACTION_MEMORY_ARCHIVE, ACTION_MESSAGE_OUTGOING_PENDING, ACTION_SKILL_TRACE
from common.redis_client import RedisClient  # noqa: F401 — kept for test namespace patching
from common.envelope import Envelope
from common.config_reload import watch_and_reload
from common.profile_loader import load_profiles, resolve_profile
from atelier.mcp_loader import load_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.agent_executor import AgentExecutor, AgentExecutionError, AgentResult
from atelier.error_synthesizer import ErrorSynthesizer
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.souvenir_backend import SouvenirBackend
from atelier.stream_publisher import StreamPublisher
from atelier.progress_config import load_progress_config, ProgressConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from common.config_loader import resolve_config_path, resolve_prompts_dir, resolve_skills_dir, resolve_storage_dir
from atelier.tool_policy import ToolPolicy
from atelier.subagents import SubagentRegistry
from atelier.tools._registry import ToolRegistry

logger = logging.getLogger("atelier")

# Ensure the atelier logger has at least one handler writing to stdout,
# even if configure_logging_once() was skipped or root-level handlers were
# removed.  This guarantees that logger.info() calls always appear in the
# supervisord stdout log file.
import sys as _sys
if not logger.handlers:
    _handler = logging.StreamHandler(_sys.stdout)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

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

        self.stream_in: str = STREAM_TASKS
        self.group_name: str = "atelier_group"
        self.consumer_name: str = "atelier_1"

        # Config-dependent state — initialised via _load() so __init__ and
        # subsequent safe_reload() checkpoints share the same code path.
        self._profiles: dict = {}
        self._mcp_servers_default: dict = {}
        self._progress_config: ProgressConfig | None = None
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

        # Option A — Singleton McpSessionManager started once at brick startup.
        # All requests share the same server connections and tool list.
        self._mcp_manager: McpSessionManager | None = None
        self._mcp_tools: list = []
        self._mcp_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # BrickBase abstract interface implementation
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load Atelier's runtime configuration from disk.

        Refreshes ``_profiles``, ``_mcp_servers_default``, and
        ``_progress_config``.  Called once by ``__init__`` for the initial
        load.  Hot-reload goes through ``_build_config_candidate`` →
        ``_apply_config`` (which holds ``_config_lock``) instead.

        Does not touch Redis or any async resource.

        Raises:
            Any exception raised by the underlying loader functions (e.g.
            ``FileNotFoundError``, ``yaml.YAMLError``) propagates so that the
            caller (``__init__`` or ``safe_reload``) can handle it.
        """
        self._profiles = load_profiles()
        self._mcp_servers_default = load_for_sdk()
        self._progress_config = load_progress_config()
        logger.info("Atelier: configuration loaded from disk")

    def stream_specs(self) -> list[StreamSpec]:
        """Return the single StreamSpec for consuming ``relais:tasks``.

        Uses ``ack_mode="on_success"`` so messages stay in the PEL on
        unexpected infrastructure errors (e.g. Redis ConnectError) and are
        re-delivered automatically.  LLM provider transient errors are retried
        internally by ``AgentExecutor.execute()`` and never leave the PEL.

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
        """Enter the AsyncSqliteSaver context manager and start the MCP singleton.

        Called by ``BrickBase.start()`` before stream loops are launched.
        Sets ``self._checkpointer`` so that ``_handle_message`` can pass it
        to ``AgentExecutor``.  Also starts the singleton ``McpSessionManager``
        and registers its ``close()`` coroutine as a cleanup callback on
        ``stack`` so it is torn down automatically when the brick shuts down.

        Args:
            stack: The ``AsyncExitStack`` used for the brick's lifespan.
        """
        self._checkpointer = await stack.enter_async_context(self._checkpointer_cm)

        # Start the singleton McpSessionManager.
        # Use the "default" profile for mcp_timeout (best-effort default).
        # Guard against test objects created via __new__ that skip __init__.
        profiles = getattr(self, "_profiles", {})
        mcp_servers = getattr(self, "_mcp_servers_default", {})
        if not hasattr(self, "_mcp_manager"):
            self._mcp_manager = None
        if not hasattr(self, "_mcp_tools"):
            self._mcp_tools = []
        if not hasattr(self, "_mcp_lock"):
            self._mcp_lock = asyncio.Lock()

        from common.profile_loader import resolve_profile as _resolve_profile
        if not profiles:
            # No profiles loaded (e.g. test objects created via __new__); skip MCP.
            self._mcp_manager = None
            self._mcp_tools = []
            return
        default_profile = _resolve_profile(profiles, "default")
        self._mcp_manager = McpSessionManager(default_profile, mcp_servers)
        await self._mcp_manager.start()
        self._mcp_tools = await make_mcp_tools(self._mcp_manager)
        stack.push_async_callback(self._mcp_manager.close)  # type: ignore[union-attr]

        # Cancel any pending MCP restart task on shutdown so it doesn't outlive
        # the McpSessionManager that was just registered for cleanup above.
        async def _cancel_mcp_restart_task() -> None:
            task = getattr(self, "_mcp_restart_task", None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        stack.push_async_callback(_cancel_mcp_restart_task)
        await self.log.info(
            f"Atelier: MCP singleton started — {len(self._mcp_tools)} tools available",
        )

    def _build_config_candidate(self) -> dict:
        """Build a new configuration snapshot from disk without mutating self.

        NOTE: each loader (load_profiles, load_for_sdk, load_progress_config,
        SubagentRegistry.load) re-reads its file from disk independently.  On
        a hot-reload triggered by a single file change this means up to four
        separate disk reads.  Acceptable for the current reload frequency;
        revisit if reload latency becomes a concern.

        Returns:
            A dict with keys ``profiles``, ``mcp_servers``, ``progress``,
            ``subagent_registry``.

        Raises:
            Any exception from the underlying loaders.
        """
        new_subagent_registry = SubagentRegistry.load(self._tool_registry)
        return {
            "profiles": load_profiles(),
            "mcp_servers": load_for_sdk(),
            "progress": load_progress_config(),
            "subagent_registry": new_subagent_registry,
        }

    def _apply_config(self, cfg: dict) -> None:
        """Swap in a freshly loaded configuration snapshot.

        If the MCP server configuration changed, schedules an async task to
        restart the singleton ``McpSessionManager`` atomically.  The task is
        scheduled via ``asyncio.get_running_loop().create_task()`` because
        ``_apply_config`` is called synchronously inside ``safe_reload``'s
        locked applier and cannot ``await`` directly.

        Args:
            cfg: Dict returned by ``_build_config_candidate``.
        """
        old_mcp = self._mcp_servers_default
        self._profiles = cfg["profiles"]
        self._mcp_servers_default = cfg["mcp_servers"]
        self._progress_config = cfg["progress"]
        if "subagent_registry" in cfg:
            self._subagent_registry = cfg["subagent_registry"]
        logger.info("Atelier: configuration applied")

        # Schedule MCP singleton restart if server config changed.
        if old_mcp != cfg["mcp_servers"]:
            logger.info("Atelier: MCP server config changed — scheduling singleton restart")
            try:
                loop = asyncio.get_running_loop()
                # Cancel any in-flight restart task before scheduling a new one
                # to avoid two concurrent McpSessionManager restarts racing.
                existing = getattr(self, "_mcp_restart_task", None)
                if existing is not None and not existing.done():
                    existing.cancel()
                self._mcp_restart_task = loop.create_task(self._restart_mcp_sessions())
            except RuntimeError:
                # No running event loop (e.g., during synchronous unit tests)
                logger.warning("Atelier: could not schedule MCP restart — no running loop")

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
            checkpoint_paths=[p for p in self._config_watch_paths() if p.is_file()],
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
        await self.log.info(f"Atelier: subscribed to {channel}")

        async for message in pubsub.listen():
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                await self.log.info("Atelier: received reload signal — reloading config")
                await self.reload_config()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    async def _restart_mcp_sessions(self) -> None:
        """Atomically replace the singleton McpSessionManager with a fresh one.

        Acquires ``_mcp_lock`` to prevent concurrent tool calls from racing
        with the replacement.  On failure, degrades gracefully by setting
        ``_mcp_manager`` to ``None`` and ``_mcp_tools`` to ``[]`` so that
        subsequent requests receive an empty tool list rather than crashing.
        """
        async with self._mcp_lock:
            # Close old manager if running.
            if self._mcp_manager is not None:
                try:
                    await self._mcp_manager.close()
                except Exception as exc:  # noqa: BLE001
                    await self.log.warning(f"Atelier: error closing old MCP manager: {exc}")

            try:
                from common.profile_loader import resolve_profile as _resolve_profile
                default_profile = _resolve_profile(self._profiles, "default")
                new_mgr = McpSessionManager(default_profile, self._mcp_servers_default)
                await new_mgr.start()
                self._mcp_manager = new_mgr
                self._mcp_tools = await make_mcp_tools(new_mgr)
                await self.log.info(
                    f"Atelier: MCP singleton restarted — {len(self._mcp_tools)} tools available",
                )
            except Exception as exc:  # noqa: BLE001
                await self.log.error(
                    f"Atelier: failed to restart MCP singleton — degrading to no MCP tools: {exc}",
                )
                self._mcp_manager = None
                self._mcp_tools = []

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
        corr = envelope.correlation_id
        sender = envelope.sender_id
        skills_used: list[str] = []  # hoisted so the DLQ path can publish a failure trace
        try:
            logger.info(
                "[TASK] received — corr=%s sender=%s channel=%s content='%s'",
                corr, sender, envelope.channel, envelope.content[:100],
            )

            # 1. Resolve LLM profile — read from CTX_PORTAIL stamped by Portail
            portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore[assignment]
            ur: dict = portail_ctx.get("user_record") or {}
            profile_name = portail_ctx.get("llm_profile") or "default"
            profile = resolve_profile(self._profiles, profile_name)
            user_id: str = portail_ctx.get("user_id") or sender
            logger.info(
                "[TASK] profile resolved — corr=%s profile=%s model=%s user_id=%s",
                corr, profile_name, profile.model, user_id,
            )

            # 2. Assemble soul system prompt
            soul_prompt = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                role_prompt_path=ur.get("role_prompt_path"),
                user_prompt_path=ur.get("prompt_path"),
                channel_prompt_path=portail_ctx.get("channel_prompt_path"),
            )
            logger.info(
                "[TASK] soul prompt assembled — corr=%s len=%d role=%s",
                corr, len(soul_prompt), ur.get("role_prompt_path", "none"),
            )

            # 3. Resolve per-user skills and MCP tool policy
            skills = self._tool_policy.resolve_skills(ur.get("skills_dirs"))
            skills_used = [Path(s).name for s in skills]
            mcp_patterns = self._tool_policy.parse_mcp_patterns(ur.get("allowed_mcp_tools"))
            logger.info(
                "[TASK] skills resolved — corr=%s skills=%s mcp_patterns=%s",
                corr, skills_used, mcp_patterns,
            )

            # 4. Read MCP tools from the singleton
            async with self._mcp_lock:
                if mcp_patterns and self._mcp_manager and self._mcp_manager.is_running:
                    mcp_tools = self._tool_policy.filter_mcp_tools(
                        list(self._mcp_tools), ur.get("allowed_mcp_tools"),
                    )
                else:
                    mcp_tools = []
            logger.info(
                "[TASK] MCP tools filtered — corr=%s mcp_tools=%d names=%s",
                corr, len(mcp_tools), [t.name for t in mcp_tools][:10],
            )

            # 5. Resolve subagents and delegation prompt
            stream_pub: StreamPublisher | None = None
            subagents = self._subagent_registry.specs_for_user(ur, request_tools=mcp_tools)
            delegation_prompt = self._subagent_registry.delegation_prompt_for_user(ur)
            logger.info(
                "[TASK] subagents resolved — corr=%s count=%d delegation_len=%d",
                corr, len(subagents), len(delegation_prompt),
            )

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

            # 6. Execute
            aig_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})  # type: ignore[assignment]
            streaming = aig_ctx.get("streaming", False)
            stream_pub = StreamPublisher(
                redis_conn,
                channel=envelope.channel,
                correlation_id=corr,
                source_envelope=envelope,
                progress_config=self._progress_config,
            )
            if streaming:
                await redis_conn.publish(
                    f"relais:streaming:start:{envelope.channel}",
                    envelope.to_json(),
                )
            logger.info(
                "[TASK] executing agent — corr=%s streaming=%s model=%s",
                corr, streaming, profile.model,
            )

            agent_result = await agent_executor.execute(
                envelope=envelope,
                stream_callback=stream_pub.push_chunk if streaming else None,
                progress_callback=stream_pub.push_progress,
            )
            reply_text = agent_result.reply_text
            logger.info(
                "[TASK] agent done — corr=%s reply_len=%d tool_calls=%d "
                "tool_errors=%d messages=%d",
                corr, len(reply_text), agent_result.tool_call_count,
                agent_result.tool_error_count, len(agent_result.messages_raw),
            )

            # 7. Publish skill trace for Forgeron (fire-and-forget)
            if skills_used and agent_result.tool_call_count > 0:
                trace_env = Envelope(
                    content="",
                    sender_id=f"atelier:{sender}",
                    channel="internal",
                    session_id=envelope.session_id,
                    correlation_id=corr,
                    action=ACTION_SKILL_TRACE,
                    context={CTX_SKILL_TRACE: {
                        "skill_names": skills_used,
                        "tool_call_count": agent_result.tool_call_count,
                        "tool_error_count": agent_result.tool_error_count,
                        "messages_raw": agent_result.messages_raw,
                    }},
                )
                await redis_conn.xadd(
                    STREAM_SKILL_TRACE, {"payload": trace_env.to_json()}
                )
                logger.info(
                    "[TASK] skill trace published — corr=%s skills=%s "
                    "calls=%d errors=%d",
                    corr, skills_used, agent_result.tool_call_count,
                    agent_result.tool_error_count,
                )
            else:
                logger.info(
                    "[TASK] skill trace SKIP — corr=%s skills_used=%s "
                    "tool_calls=%d",
                    corr, skills_used, agent_result.tool_call_count,
                )

            # 8. Build and publish response envelope
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.action = ACTION_MESSAGE_OUTGOING_PENDING
            atelier_ctx = ensure_ctx(response_env, CTX_ATELIER)
            atelier_ctx["user_message"] = envelope.content
            if skills_used:
                atelier_ctx["skills_used"] = skills_used
            if streaming:
                await stream_pub.finalize()
                atelier_ctx["streamed"] = True
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = STREAM_OUTGOING_PENDING
            await redis_conn.xadd(out_stream, {"payload": response_env.to_json()})
            logger.info(
                "[TASK] response published — corr=%s stream=%s reply='%s'",
                corr, out_stream, reply_text[:80],
            )

            # 9. Archive turn to Souvenir
            archive_env = Envelope(
                content="",
                sender_id=f"atelier:{sender}",
                channel="internal",
                session_id=envelope.session_id,
                correlation_id=corr,
                action=ACTION_MEMORY_ARCHIVE,
                context={CTX_SOUVENIR_REQUEST: {
                    "envelope_json": response_env.to_json(),
                    "messages_raw": agent_result.messages_raw,
                }},
            )
            await redis_conn.xadd(
                STREAM_MEMORY_REQUEST, {"payload": archive_env.to_json()},
            )
            logger.info(
                "[TASK] archive published — corr=%s messages=%d",
                corr, len(agent_result.messages_raw),
            )

            logger.info(
                "[TASK] DONE — corr=%s sender=%s model=%s reply_len=%d",
                corr, sender, profile.model, len(reply_text),
            )
            return True

        except AgentExecutionError as exc:
            # Non-recoverable agent failure — route to DLQ and ACK
            logger.error(
                "[TASK] AgentExecutionError — corr=%s error='%s' "
                "tool_calls=%d tool_errors=%d messages_raw=%d skills=%s",
                corr, exc, exc.tool_call_count, exc.tool_error_count,
                len(exc.messages_raw), skills_used,
            )
            dlq_entry: dict = {
                "payload": envelope.to_json(),
                "reason": str(exc),
                "failed_at": str(time.time()),
            }
            if exc.response_body:
                dlq_entry["error_detail"] = exc.response_body[:4000]
            await redis_conn.xadd(STREAM_TASKS_FAILED, dlq_entry)
            logger.info(
                "[TASK] DLQ published — corr=%s stream=%s",
                corr, STREAM_TASKS_FAILED,
            )

            # Synthesize an empathetic error reply
            try:
                synth = ErrorSynthesizer()
                logger.info(
                    "[TASK] synthesizing error reply — corr=%s model=%s "
                    "messages_raw=%d",
                    corr, profile.model, len(exc.messages_raw),
                )
                error_reply = await synth.synthesize(
                    messages_raw=exc.messages_raw,
                    error=str(exc),
                    profile=profile,
                )
                error_env = Envelope.from_parent(parent=envelope, content=error_reply)
                error_env.action = ACTION_MESSAGE_OUTGOING_PENDING
                await redis_conn.xadd(STREAM_OUTGOING_PENDING, {"payload": error_env.to_json()})
                logger.info(
                    "[TASK] error reply published — corr=%s reply='%s'",
                    corr, error_reply[:80],
                )
            except Exception as synth_exc:
                logger.warning(
                    "[TASK] error reply FAILED — corr=%s error=%s",
                    corr, synth_exc,
                )

            # Publish failure trace so Forgeron can analyze aborted turns
            if skills_used:
                try:
                    failure_trace_env = Envelope(
                        content="",
                        sender_id=f"atelier:{sender}",
                        channel="internal",
                        session_id=envelope.session_id,
                        correlation_id=corr,
                        action=ACTION_SKILL_TRACE,
                        context={CTX_SKILL_TRACE: {
                            "skill_names": skills_used,
                            "tool_call_count": exc.tool_call_count,
                            "tool_error_count": -1,  # sentinel: aborted turn
                            "messages_raw": exc.messages_raw,
                        }},
                    )
                    await redis_conn.xadd(
                        STREAM_SKILL_TRACE, {"payload": failure_trace_env.to_json()}
                    )
                    logger.info(
                        "[TASK] failure trace published — corr=%s skills=%s "
                        "messages_raw=%d",
                        corr, skills_used, len(exc.messages_raw),
                    )
                except Exception as trace_exc:
                    logger.warning(
                        "[TASK] failure trace FAILED — corr=%s error=%s",
                        corr, trace_exc,
                    )
            else:
                logger.info(
                    "[TASK] failure trace SKIP — no skills_used corr=%s",
                    corr,
                )
            return True

        except Exception as exc:
            # Transient or unknown error — leave in PEL for re-delivery
            logger.error(
                "[TASK] UNHANDLED exception — corr=%s error='%s' "
                "leaving in PEL for re-delivery",
                corr, exc, exc_info=True,
            )
            return False


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
