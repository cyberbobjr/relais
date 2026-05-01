"""Atelier brick — DeepAgents-based LLM execution pipeline.

Functional role
---------------
Executes the agentic LLM loop for each authorized task.  Conversation history
is managed natively by the persistent LangGraph checkpointer (AsyncSqliteSaver,
``checkpoints.db``), keyed by ``user_id``.  Resolves memory paths for the
multi-layer system prompt (soul + role + user + channel layers), runs the
DeepAgents loop with MCP and internal tools, streams token-by-token output to
the channel, and publishes the final reply for Sentinelle to deliver.

Technical overview
------------------
Key classes:

* ``AgentExecutor`` (atelier.agent_executor) — orchestrates a single
  ``deepagents.create_deep_agent()`` call; handles streaming via
  ``agent.astream(stream_mode=["updates", "messages"], subgraphs=True,
  version="v2")``; accepts an optional ``backend: BackendProtocol`` (for
  DeepAgents persistent memory) and ``progress_callback`` forwarded from
  the caller.  Before dispatching to the model, the executor prepends a
  ``<relais_execution_context>`` block to the first user message containing
  ``sender_id``, ``channel``, ``session_id``, ``correlation_id`` and
  ``reply_to``.  This block is technical metadata used by skills that need
  routing information (notably ``channel-setup`` for WhatsApp pairing); the
  system prompt explicitly instructs the model not to echo it back to the
  user.
* ``McpSessionManager`` (atelier.mcp_session_manager) — **singleton** managing
  stdio/SSE MCP server lifecycle; started once at brick startup via ``start()``,
  shared across all requests; per-server ``asyncio.Lock`` serializes stdio pipe
  calls; dead sessions (``BrokenPipeError``, ``ConnectionError``, ``EOFError``)
  evicted and re-established on next call; closed on shutdown via ``close()``.
* ``ToolPolicy`` (atelier.tool_policy) — resolves skill directories per role
  and filters MCP tool definitions by role-level ``allowed_mcp_tools`` patterns.
* ``SoulAssembler`` (atelier.soul_assembler) — resolves and validates
  multi-layer prompt file paths (soul / role / user / channel), returning
  them as a ``memory_paths: list[str]`` for ``create_deep_agent(memory=)``.
  File reading is delegated to DeepAgents; this module only validates paths.
* ``ProfileConfig`` — loaded from ``common/profile_loader.py`` (config file
  ``atelier/profiles.yaml``); selects model, temperature, max_tokens per
  request.  ``shell_timeout_seconds`` (default 30) caps individual shell tool
  calls; ``max_turn_seconds`` (default 300, 0 = disabled) caps the total turn.
  Optional field ``parallel_tool_calls: bool | None`` forwards the
  OpenAI-compatible
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
* ``StreamBuffer`` (atelier.streaming) — accumulates text tokens and
  flushes to a callback when a character threshold is reached.
* ``SubagentMessageCapture`` (atelier.subagent_capture) — LangChain
  ``BaseCallbackHandler`` injected into the parent ``RunnableConfig`` so
  LangGraph propagates it to all child invocations including subagents.
  Captures ``on_chat_model_start`` / ``on_llm_end`` / ``on_tool_start`` /
  ``on_tool_end`` events keyed by ``langgraph_namespace`` so Atelier can
  build per-subagent ``SubagentTrace`` objects after the turn completes.
* ``SubagentTrace`` (atelier.agent_executor) — frozen dataclass carrying
  ``subagent_name``, ``skill_names``, ``tool_call_count``,
  ``tool_error_count``, and ``messages_raw`` for a single subagent
  invocation.  Collected into ``AgentResult.subagent_traces`` (a tuple,
  empty when no subagents ran).
* ``DiagnosticTrace`` (atelier.errors) — structured dataclass produced by
  ``format_diagnostic_trace()`` capturing ``messages_count``,
  ``tool_count``, ``tool_errors``, ``last_tool``, ``last_error``, and
  ``tool_error_details``; rendered to plain text by
  ``_render_diagnostic_trace()`` before injection into conversation history.

Helper modules extracted from agent_executor.py (re-exported for compat):
* ``atelier.profile_model`` — ``_resolve_profile_model()`` builds
  ``BaseChatModel | str`` from a ``ProfileConfig`` by dispatching to the
  first matching ``ModelHandler`` in ``_HANDLER_REGISTRY``
  (``DeepSeekModelHandler`` → ``DefaultModelHandler``).  Provider-specific
  handlers raise ``ImportError`` when their required library is absent
  (e.g. ``langchain_deepseek`` for ``deepseek:`` models) instead of
  silently falling back.
* ``atelier.prompts`` — system-prompt constants and builders
  (``LONG_TERM_MEMORY_PROMPT``, ``SELF_DIAGNOSIS_PROMPT``,
  ``build_project_context_prompt``, ``_build_execution_context``,
  ``_enrich_system_prompt``, …).
* ``atelier.transient_errors`` — provider-agnostic transient-error detection
  (``_is_transient_provider_error``, ``_TRANSIENT_ERROR_NAMES``, …).
* ``atelier.diagnostic_trace`` — diagnostic trace formatting helpers
  (``format_diagnostic_trace``, ``_render_diagnostic_trace``,
  ``_DIAGNOSTIC_MAX_CHARS``); re-exported from ``agent_executor`` for compat.
* ``atelier.stream_loop`` — stream-loop state and pure helpers:
  ``StreamLoopState`` (mutable accumulator for a ``_stream()`` call),
  ``compute_reply_text`` (final reply selection with nemotron-mini fallback
  to ``last_tool_result`` and ``REPLY_PLACEHOLDER``),
  ``build_subagent_traces`` (builds ``SubagentTrace`` objects from
  LangChain callback data); re-exported from ``agent_executor`` for compat.

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
                                  only when skills were actually invoked).  ``skills_used``
                                  is derived by scanning ``messages_raw`` for AIMessage
                                  tool_calls where name == "read_skill"
                                  (``extract_read_skill_names`` from ``atelier.message_serializer``); only skills that were
                                  genuinely read appear here.  Published in two cases:
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
    │  (6) Execute — always streaming; adapters decide whether to buffer or forward
    │      AgentExecutor.execute(profile, soul_prompt, mcp_tools, skills,
    │      backend=SouvenirBackend, checkpointer, subagents, delegation_prompt,
    │      progress_callback=…)
    │      ├── token chunks    ──► relais:messages:streaming:{channel}:{corr_id}
    │      ├── progress events ──► relais:messages:streaming + relais:messages:outgoing:{channel}
    │      └── AgentResult(reply_text, messages_raw, tool_call_count, tool_error_count,
    │                       subagent_traces)
    │          ← full turn captured via aget_state(); subagent_traces built from
    │            SubagentMessageCapture callbacks (empty tuple when no subagents ran)
    │  (7) if skills_used and tool_call_count > 0 → publish ACTION_SKILL_TRACE envelope
    │      to relais:skill:trace (fire-and-forget → Forgeron stores the trace)
    │      └── Forgeron handles changelog + consolidation autonomously
    │  (7b) for each SubagentTrace in AgentResult.subagent_traces where
    │       tool_call_count > 0 AND skill_names non-empty → publish a separate
    │       ACTION_SKILL_TRACE envelope to relais:skill:trace so Forgeron tracks
    │       skill performance at the subagent level independently
    │  (8) build response Envelope, stamp response_env.action = ACTION_MESSAGE_OUTGOING_PENDING
    │      (required: Envelope.to_json() raises if action is unset), stamp
    │      context["atelier"]["skills_used"] if any; publish archive to relais:memory:request
    │      (Souvenir persists full LangChain history)
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

Atelier always streams token-by-token.  Each adapter buffers or forwards tokens
as appropriate for its channel.  Each text chunk is published via StreamPublisher
for real-time rendering before the full reply is ready.  Discord receives a
final reply only, with live progress events (tool_call, tool_result,
subagent_start) sent to
``relais:messages:outgoing:{channel}`` as ``message_type=progress`` envelopes.
Publishing is governed by ``DisplayConfig`` (atelier.yaml ``display:`` section).
"""

import asyncio
import contextlib
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.streams import (
    STREAM_ATELIER_CONTROL,
    STREAM_MEMORY_REQUEST,
    STREAM_OUTGOING_PENDING,
    STREAM_SKILL_TRACE,
    STREAM_TASKS,
    STREAM_TASKS_FAILED,
    pubsub_streaming_start,
)
from common.contexts import (
    CTX_AIGUILLEUR,
    CTX_ATELIER,
    CTX_ATELIER_CONTROL,
    CTX_FORGERON,
    CTX_PORTAIL,
    CTX_SKILL_TRACE,
    CTX_SOUVENIR_REQUEST,
    AiguilleurCtx,
    PortailCtx,
    ensure_ctx,
)
from common.envelope_actions import ACTION_ATELIER_COMPACT, ACTION_MEMORY_ARCHIVE, ACTION_MESSAGE_OUTGOING_PENDING, ACTION_SKILL_TRACE
from common.redis_client import RedisClient  # noqa: F401 — kept for test namespace patching
from common.envelope import Envelope
from common.config_reload import watch_and_reload
from common.profile_loader import load_profiles, resolve_profile
from atelier.message_serializer import extract_read_skill_names
from atelier.mcp_loader import load_for_sdk
from atelier.soul_assembler import assemble_system_prompt
from atelier.agent_executor import AgentExecutor, AgentExecutionError, AgentResult, CompactResult, build_project_context_prompt, format_diagnostic_trace, _render_diagnostic_trace
from atelier.error_synthesizer import ErrorSynthesizer
from atelier.mcp_session_manager import McpSessionManager
from atelier.mcp_adapter import make_mcp_tools
from atelier.souvenir_backend import SouvenirBackend
from atelier.stream_publisher import StreamPublisher
from atelier.display_config import load_display_config, DisplayConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from common.config_loader import get_relais_home, get_relais_project_dir, resolve_bundles_dir, resolve_config_path, resolve_prompts_dir, resolve_skills_dir, resolve_storage_dir
from atelier.tool_policy import ToolPolicy
from atelier.subagents import SubagentRegistry
from atelier.tools._registry import ToolRegistry

logger = logging.getLogger("atelier")

# Ensure the atelier logger has at least one handler writing to stdout,
# even if configure_logging_once() was skipped or root-level handlers were
# removed.  This guarantees that logger.info() calls always appear in the
# supervisord stdout log file.

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
        self._display_config: DisplayConfig | None = None
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

        # Ensure bundles dir exists so the file watcher can target it directly
        # instead of falling back to its parent (~/.relais/), which would cause
        # an infinite reload loop (log writes under ~/.relais/ would re-trigger).
        resolve_bundles_dir().mkdir(parents=True, exist_ok=True)

        # Option A — Singleton McpSessionManager started once at brick startup.
        # All requests share the same server connections and tool list.
        self._mcp_manager: McpSessionManager | None = None
        self._mcp_tools: list = []
        self._mcp_lock = asyncio.Lock()

        # Per-profile cache of minimal executors for compact operations — avoids
        # recompiling the LangGraph graph on every /compact call. Cleared on config
        # reload so the next compact picks up updated profiles (model changes, etc.).
        self._compact_executors: dict[str, AgentExecutor] = {}

    # ------------------------------------------------------------------
    # BrickBase abstract interface implementation
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load Atelier's runtime configuration from disk.

        Refreshes ``_profiles``, ``_mcp_servers_default``, and
        ``_display_config``.  Called once by ``__init__`` for the initial
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
        self._display_config = load_display_config()
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
            ),
            StreamSpec(
                stream=STREAM_ATELIER_CONTROL,
                group="atelier_control_group",
                consumer=self.consumer_name,
                handler=self._handle_control,
                ack_mode="on_success",
                block_ms=2000,
                count=5,
            ),
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

        # Start the singleton McpSessionManager using the "default" profile.
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

        NOTE: each loader (load_profiles, load_for_sdk, load_display_config,
        SubagentRegistry.load) re-reads its file from disk independently.  On
        a hot-reload triggered by a single file change this means up to four
        separate disk reads.  Acceptable for the current reload frequency;
        revisit if reload latency becomes a concern.

        Returns:
            A dict with keys ``profiles``, ``mcp_servers``, ``display``,
            ``subagent_registry``.

        Raises:
            Any exception from the underlying loaders.
        """
        new_subagent_registry = SubagentRegistry.load(self._tool_registry)
        return {
            "profiles": load_profiles(),
            "mcp_servers": load_for_sdk(),
            "display": load_display_config(),
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
        self._display_config = cfg["display"]
        if "subagent_registry" in cfg:
            self._subagent_registry = cfg["subagent_registry"]
        self._compact_executors = {}  # force rebuild on next compact (profiles may have changed)
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
        from atelier.subagents import NATIVE_SUBAGENTS_PATH

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
        # Also watch native subagents bundled in the source tree
        if NATIVE_SUBAGENTS_PATH.is_dir():
            paths.append(NATIVE_SUBAGENTS_PATH)
        # Watch bundles parent so a first-ever install (dir creation) is also detected
        bundles_dir = resolve_bundles_dir()
        watch_target = bundles_dir if bundles_dir.is_dir() else bundles_dir.parent
        if watch_target.is_dir():
            paths.append(watch_target)
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
    # Control stream handler
    # ------------------------------------------------------------------

    async def _handle_control(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Handle a control envelope from ``relais:atelier:control``.

        Currently supports a single operation: ``op="compact"`` — triggers a
        conversation history compaction for the given session.

        Profile resolution: reads ``llm_profile`` from ``context["portail"]``
        of the original user envelope (carried in ``ctrl_ctx["envelope_json"]``)
        so that ``compact_keep`` and the summarization model match the requesting
        user's assigned profile.  A per-profile ``AgentExecutor`` cache
        (``_compact_executors``) avoids recompiling the LangGraph graph on every
        call; the cache is cleared on config reload.

        On ``compact_session()`` failure the exception is caught and an error
        reply is published to the user — control messages are always ACKed.

        Args:
            envelope: Control envelope published by Commandant.
            redis_conn: Active Redis connection.

        Returns:
            Always ``True`` to ACK the control message (failures are caught,
            logged, and reported to the user; never left in the PEL).
        """
        ctrl_ctx: dict = envelope.context.get(CTX_ATELIER_CONTROL, {})
        op: str = ctrl_ctx.get("op", "")
        user_id: str = ctrl_ctx.get("user_id", envelope.sender_id)

        if op != "compact":
            logger.warning(
                "[CONTROL] unknown op=%r — corr=%s", op, envelope.correlation_id
            )
            return True

        logger.info(
            "[CONTROL] compact requested — session=%s user=%s corr=%s",
            envelope.session_id, user_id, envelope.correlation_id,
        )

        # Resolve the originating envelope early to read the user's llm_profile.
        try:
            envelope_json = ctrl_ctx.get("envelope_json", "")
            source_env = Envelope.from_json(envelope_json) if envelope_json else envelope
        except Exception:
            source_env = envelope

        # Honour the requesting user's LLM profile (compact_keep, model).
        portail_ctx = source_env.context.get(CTX_PORTAIL, {})
        llm_profile: str = portail_ctx.get("llm_profile", "default")
        profile = (
            self._profiles.get(llm_profile)
            or self._profiles.get("default")
            or next(iter(self._profiles.values()), None)
        )
        compact_keep: int = getattr(profile, "compact_keep", 6)

        reply_text: str
        try:
            if llm_profile not in self._compact_executors:
                self._compact_executors[llm_profile] = AgentExecutor(
                    profile=profile,
                    memory_paths=[],
                    tools=[],
                    checkpointer=self._checkpointer,
                )
            result: CompactResult | None = await self._compact_executors[llm_profile].compact_session(
                session_id=envelope.session_id,
                user_id=user_id,
                compact_keep=compact_keep,
            )
            if result is not None:
                reply_text = (
                    f"Conversation compacted: {result.messages_before} → "
                    f"{result.messages_after} messages "
                    f"(kept {compact_keep} recent + 1 summary)."
                )
            else:
                reply_text = "Nothing to compact (session is empty or already within limits)."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "[CONTROL] compact_session failed — session=%s corr=%s",
                envelope.session_id, envelope.correlation_id,
            )
            reply_text = f"Compaction failed: {exc}"

        # Publish reply to the original channel so the user receives feedback.
        try:
            reply = Envelope.create_response_to(source_env, reply_text)
            reply.action = ACTION_MESSAGE_OUTGOING_PENDING
            await redis_conn.xadd(STREAM_OUTGOING_PENDING, {"payload": reply.to_json()})
            logger.info(
                "[CONTROL] compact reply published — corr=%s", envelope.correlation_id
            )
        except Exception as exc:
            logger.warning(
                "[CONTROL] failed to publish compact reply — corr=%s error=%s",
                envelope.correlation_id, exc,
            )

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish_skill_trace(
        self,
        redis_conn: Any,
        envelope: Envelope,
        sender: str,
        skill_names: list[str],
        tool_call_count: int,
        tool_error_count: int,
        messages_raw: list[dict],
        skill_paths: dict[str, str],
        *,
        subagent_name: str | None = None,
    ) -> None:
        """Build and publish one skill-trace envelope to STREAM_SKILL_TRACE.

        Args:
            redis_conn: Active Redis connection.
            envelope: Parent envelope (provides session_id, correlation_id).
            sender: Originating sender_id string.
            skill_names: Skill directory names involved in this trace.
            tool_call_count: Number of tool calls made.
            tool_error_count: Number of tool errors (-1 = aborted/timeout sentinel).
            messages_raw: Serialised LangChain message list for this turn.
            skill_paths: Mapping of skill name → bundle-relative path.
            subagent_name: When set, marks this as a subagent-level trace.
        """
        ctx: dict = {
            "skill_names": skill_names,
            "tool_call_count": tool_call_count,
            "tool_error_count": tool_error_count,
            "messages_raw": messages_raw,
            "skill_paths": skill_paths,
        }
        if subagent_name is not None:
            ctx["subagent_name"] = subagent_name
        trace_env = Envelope(
            content="",
            sender_id=f"atelier:{sender}",
            channel="internal",
            session_id=envelope.session_id,
            correlation_id=envelope.correlation_id,
            action=ACTION_SKILL_TRACE,
            context={CTX_SKILL_TRACE: ctx},
        )
        await redis_conn.xadd(STREAM_SKILL_TRACE, {"payload": trace_env.to_json()})

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
        skill_paths: dict[str, str] = {}  # hoisted alongside skills_used (may be unset on early timeout)
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
            assembly = assemble_system_prompt(
                prompts_dir=_PROMPTS_DIR,
                role_prompt_path=ur.get("role_prompt_path"),
                user_prompt_path=ur.get("prompt_path"),
                channel_prompt_path=portail_ctx.get("channel_prompt_path"),
            )
            memory_paths = assembly.memory_paths
            if assembly.is_degraded:
                logger.warning(
                    "[TASK] soul prompt degraded — corr=%s issues=%s",
                    corr, assembly.issues,
                )
            logger.info(
                "[TASK] memory paths assembled — corr=%s count=%d role=%s",
                corr, len(memory_paths), ur.get("role_prompt_path", "none"),
            )

            # 3. Resolve per-user skills and MCP tool policy
            skills = self._tool_policy.resolve_skills(ur.get("skills_dirs"))
            skills_used = [Path(s).name for s in skills]
            _bundles_dir = resolve_bundles_dir().resolve()
            skill_paths = {
                Path(s).name: s
                for s in skills
                if Path(s).resolve().is_relative_to(_bundles_dir)
            }
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
            _project_context = build_project_context_prompt(
                str(get_relais_home()), str(get_relais_project_dir())
            )
            subagents = self._subagent_registry.specs_for_user(
                ur, request_tools=mcp_tools, project_context=_project_context
            )
            delegation_prompt = self._subagent_registry.delegation_prompt_for_user(ur)
            logger.info(
                "[TASK] subagents resolved — corr=%s count=%d delegation_len=%d",
                corr, len(subagents), len(delegation_prompt),
            )

            # 5b. Override: force_subagent from Forgeron correction pipeline
            forgeron_ctx: dict = envelope.context.get(CTX_FORGERON, {})
            force_subagent_name: str | None = forgeron_ctx.get("force_subagent")
            if force_subagent_name:
                forced = next(
                    (s for s in subagents if s["name"] == force_subagent_name), None
                )
                if forced is not None:
                    subagents = [forced]
                    corrected_behavior = forgeron_ctx.get("corrected_behavior", "")
                    delegation_prompt = (
                        f"You MUST delegate immediately to the subagent '{force_subagent_name}'. "
                        f"Do not use any other subagent. "
                        f"Context: {corrected_behavior}"
                    )
                    logger.info(
                        "[TASK] force_subagent override — corr=%s subagent=%s",
                        corr, force_subagent_name,
                    )
                else:
                    logger.warning(
                        "[TASK] force_subagent '%s' not found in role-filtered list — "
                        "falling back to normal agent — corr=%s",
                        force_subagent_name, corr,
                    )

            # 6. Execute — always streaming; adapters decide whether to buffer or forward
            display_config = replace(self._display_config, final_only=False)
            agent_executor = AgentExecutor(
                profile=profile,
                memory_paths=memory_paths,
                tools=mcp_tools,
                skills=skills,
                backend=SouvenirBackend(user_id=user_id),
                checkpointer=self._checkpointer,
                subagents=subagents,
                delegation_prompt=delegation_prompt,
                display_config=display_config,
            )
            stream_pub = StreamPublisher(
                redis_conn,
                channel=envelope.channel,
                correlation_id=corr,
                source_envelope=envelope,
                display_config=display_config,
            )
            await redis_conn.publish(
                pubsub_streaming_start(envelope.channel),
                envelope.to_json(),
            )
            logger.info(
                "[TASK] executing agent — corr=%s model=%s",
                corr, profile.model,
            )

            _turn_timeout: float | None = profile.max_turn_seconds if profile.max_turn_seconds > 0 else None
            agent_result = await asyncio.wait_for(
                agent_executor.execute(
                    envelope=envelope,
                    stream_callback=stream_pub.push_chunk,
                    progress_callback=stream_pub.push_progress,
                ),
                timeout=_turn_timeout,
            )
            skills_used = extract_read_skill_names(agent_result.messages_raw)
            reply_text = agent_result.reply_text
            logger.info(
                "[TASK] agent done — corr=%s reply_len=%d tool_calls=%d "
                "tool_errors=%d messages=%d",
                corr, len(reply_text), agent_result.tool_call_count,
                agent_result.tool_error_count, len(agent_result.messages_raw),
            )

            # 7. Publish skill trace for Forgeron (fire-and-forget)
            if skills_used and agent_result.tool_call_count > 0:
                await self._publish_skill_trace(
                    redis_conn, envelope, sender,
                    skill_names=skills_used,
                    tool_call_count=agent_result.tool_call_count,
                    tool_error_count=agent_result.tool_error_count,
                    messages_raw=agent_result.messages_raw,
                    skill_paths=skill_paths,
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

            # 7b. Publish per-subagent skill traces for Forgeron
            subagent_by_name = {s.get("name"): s for s in subagents if s.get("name")}
            for sa_trace in agent_result.subagent_traces:
                if sa_trace.tool_call_count == 0 or not sa_trace.skill_names:
                    continue
                sa_spec = subagent_by_name.get(sa_trace.subagent_name)
                sa_skill_paths: dict[str, str] = {}
                if sa_spec is not None:
                    for skill_path in sa_spec.get("skills", []):
                        p = Path(skill_path).resolve()
                        if p.is_relative_to(_bundles_dir):
                            sa_skill_paths[p.name] = skill_path
                await self._publish_skill_trace(
                    redis_conn, envelope, sender,
                    skill_names=sa_trace.skill_names,
                    tool_call_count=sa_trace.tool_call_count,
                    tool_error_count=sa_trace.tool_error_count,
                    messages_raw=sa_trace.messages_raw,
                    skill_paths=sa_skill_paths,
                    subagent_name=sa_trace.subagent_name,
                )
                logger.info(
                    "[TASK] subagent skill trace published — corr=%s subagent=%s "
                    "skills=%s calls=%d errors=%d",
                    corr, sa_trace.subagent_name, sa_trace.skill_names,
                    sa_trace.tool_call_count, sa_trace.tool_error_count,
                )

            # 8. Build and publish response envelope
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.action = ACTION_MESSAGE_OUTGOING_PENDING
            atelier_ctx = ensure_ctx(response_env, CTX_ATELIER)
            atelier_ctx["user_message"] = envelope.content
            if skills_used:
                atelier_ctx["skills_used"] = skills_used
            await stream_pub.finalize()
            atelier_ctx["streamed"] = True
            response_env.add_trace("atelier", f"Generated via {profile.model}")

            out_stream = STREAM_OUTGOING_PENDING
            response_json = response_env.to_json()
            await redis_conn.xadd(out_stream, {"payload": response_json})
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
                    "envelope_json": response_json,
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

        except asyncio.TimeoutError:
            # Turn exceeded max_turn_seconds — treat like AgentExecutionError
            logger.error(
                "[TASK] turn TIMEOUT — corr=%s max_turn_seconds=%s skills=%s",
                corr, profile.max_turn_seconds, skills_used,
            )
            dlq_entry_timeout: dict = {
                "payload": envelope.to_json(),
                "reason": f"turn timeout after {profile.max_turn_seconds}s",
                "failed_at": str(time.time()),
            }
            await redis_conn.xadd(STREAM_TASKS_FAILED, dlq_entry_timeout)

            # Publish a plain error reply — no ErrorSynthesizer (no messages_raw available)
            timeout_msg = (
                "La requête a pris trop de temps et a été interrompue. "
                "Merci de réessayer ou de simplifier votre demande."
            )
            timeout_env = Envelope.from_parent(parent=envelope, content=timeout_msg)
            timeout_env.action = ACTION_MESSAGE_OUTGOING_PENDING
            await redis_conn.xadd(STREAM_OUTGOING_PENDING, {"payload": timeout_env.to_json()})

            # Publish failure trace so Forgeron can detect timeout patterns
            if skills_used:
                try:
                    await self._publish_skill_trace(
                        redis_conn, envelope, sender,
                        skill_names=skills_used,
                        tool_call_count=0,
                        tool_error_count=-1,  # sentinel: timeout/aborted turn
                        messages_raw=[],
                        skill_paths=skill_paths,
                    )
                    logger.info(
                        "[TASK] timeout trace published — corr=%s skills=%s",
                        corr, skills_used,
                    )
                except Exception as trace_exc:
                    logger.warning(
                        "[TASK] timeout trace FAILED — corr=%s error=%s",
                        corr, trace_exc,
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

            diag_trace = format_diagnostic_trace(
                error=str(exc),
                messages_raw=exc.messages_raw,
                tool_call_count=exc.tool_call_count,
                tool_error_count=exc.tool_error_count,
            )
            diag_text = _render_diagnostic_trace(
                diag_trace,
                error=str(exc),
            )
            await agent_executor.inject_diagnostic_message(envelope, diag_text)

            # Publish failure trace so Forgeron can analyze aborted turns
            if skills_used:
                try:
                    await self._publish_skill_trace(
                        redis_conn, envelope, sender,
                        skill_names=skills_used,
                        tool_call_count=exc.tool_call_count,
                        tool_error_count=-1,  # sentinel: aborted turn
                        messages_raw=exc.messages_raw,
                        skill_paths=skill_paths,
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

            for sa_trace in exc.subagent_traces:
                if not sa_trace.skill_names:
                    continue
                try:
                    await self._publish_skill_trace(
                        redis_conn, envelope, sender,
                        skill_names=sa_trace.skill_names,
                        tool_call_count=sa_trace.tool_call_count,
                        tool_error_count=-1,  # sentinel: aborted subagent turn
                        messages_raw=sa_trace.messages_raw,
                        skill_paths={},
                        subagent_name=sa_trace.subagent_name,
                    )
                    logger.info(
                        "[TASK] subagent failure trace published — corr=%s subagent=%s skills=%s",
                        corr, sa_trace.subagent_name, sa_trace.skill_names,
                    )
                except Exception as sa_exc:
                    logger.warning(
                        "[TASK] subagent failure trace FAILED — corr=%s subagent=%s error=%s",
                        corr, sa_trace.subagent_name, sa_exc,
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
