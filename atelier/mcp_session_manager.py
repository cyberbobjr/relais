"""McpSessionManager — lifecycle and dispatch for MCP servers in the Atelier.

Separates MCP infrastructure (server start, session management, tool dispatch)
from the agentic loop logic in SDKExecutor.

Option A — Singleton lifecycle:
  The manager owns its own AsyncExitStack and is started once at brick startup
  via start(), shared across all requests, and closed once at shutdown via close().
  Per-server asyncio.Lock instances serialize concurrent tool calls per server
  (stdio pipes are not concurrent-safe).  Dead sessions (BrokenPipeError,
  ConnectionError, EOFError) are evicted from the pool automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from atelier.profile_loader import ProfileConfig

# ---------------------------------------------------------------------------
# Optional MCP dependency
# ---------------------------------------------------------------------------

try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore[import-untyped]
    from mcp.client.stdio import stdio_client  # type: ignore[import-untyped]

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]

try:
    from mcp.client.sse import sse_client  # type: ignore[import-untyped]

    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False
    sse_client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Exception types that indicate a dead/broken MCP server connection.
# These trigger session eviction rather than a generic error return.
_DEAD_SESSION_ERRORS = (BrokenPipeError, ConnectionError, EOFError)


class McpSessionManager:
    """Manages lifecycle and tool dispatch for MCP servers.

    Supports two usage patterns:

    **Singleton (Option A) — recommended:**
    Start once at brick startup, share across all requests::

        manager = McpSessionManager(profile, mcp_servers)
        await manager.start()
        # ... handle many requests, manager.tools is available ...
        await manager.close()

    **Per-request (legacy) — via start_all():**
    Caller-owned AsyncExitStack; kept for backward compatibility::

        manager = McpSessionManager(profile, mcp_servers)
        async with contextlib.AsyncExitStack() as stack:
            mcp_tools = await manager.start_all(stack)
            result = await manager.call_tool("server__tool_name", tool_input)

    Concurrent-safety:
        Per-server ``asyncio.Lock`` instances serialize calls to the same
        stdio pipe (which is not concurrent-safe).  Calls to different servers
        may proceed concurrently.

    Dead session eviction:
        When ``call_tool`` receives ``BrokenPipeError``, ``ConnectionError``, or
        ``EOFError`` from a session, that session is removed from the active pool
        and a WARNING is logged.  The next call_tool to that server will return
        an error string rather than retrying (the pool is not automatically
        replenished; a hot-reload is needed to reconnect).
    """

    def __init__(self, profile: ProfileConfig, mcp_servers: dict[str, dict[str, object]]) -> None:
        """Initialise the manager.

        Args:
            profile: LLM profile providing mcp_timeout and mcp_max_tools.
            mcp_servers: Dict mapping server names to transport config dicts
                (as returned by ``mcp_loader.load_for_sdk()``).
        """
        self._profile = profile
        self._mcp_servers = mcp_servers
        self._sessions: dict[str, ClientSession] = {}
        # Per-server locks — serialise concurrent calls to the same stdio pipe.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Owned exit stack for singleton lifecycle (Option A).
        self._stack: contextlib.AsyncExitStack | None = None
        # Tool definitions collected during start_all().
        self._tools: list[dict] = []

    # ------------------------------------------------------------------
    # Singleton lifecycle API (Option A)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all configured MCP servers and store their tool schemas.

        Creates a self-owned ``AsyncExitStack``, calls ``start_all(stack)``,
        and caches the returned tools in ``self.tools``.  After this method
        returns, ``is_running`` is True and ``tools`` is populated.

        Calling ``start()`` on an already-running manager is a no-op.

        Raises:
            Any exception from the underlying loaders (e.g. process-spawn
            failures) only if they propagate past ``start_all()``'s per-server
            try/except.
        """
        if self._stack is not None:
            return  # already running

        self._stack = contextlib.AsyncExitStack()
        await self._stack.__aenter__()
        self._tools = await self.start_all(self._stack)
        logger.info(
            "McpSessionManager: singleton started — %d tools across %d servers",
            len(self._tools),
            len(self._sessions),
        )

    async def close(self) -> None:
        """Close all MCP server connections and reset state.

        If the manager was not started this is a safe no-op.  After this
        method returns, ``is_running`` is False and ``tools`` is empty.
        """
        if self._stack is None:
            return

        stack, self._stack = self._stack, None
        try:
            await stack.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("McpSessionManager: error during close: %s", exc)

        self._sessions = {}
        self._session_locks = {}
        self._tools = []

    @property
    def is_running(self) -> bool:
        """True when the singleton has been started and not yet closed."""
        return self._stack is not None

    @property
    def tools(self) -> list[dict]:
        """Tool definitions collected during the last successful start_all() call."""
        return self._tools

    # ------------------------------------------------------------------
    # Legacy per-request API (backward compatibility)
    # ------------------------------------------------------------------

    @property
    def sessions(self) -> dict[str, ClientSession]:
        """Active MCP sessions keyed by server name."""
        return self._sessions

    async def start_all(self, stack: contextlib.AsyncExitStack) -> list[dict]:
        """Start all configured MCP servers and return their tool schemas.

        Tool names are prefixed ``{server_name}__`` to avoid cross-server
        collisions. Sessions are stored internally and available for
        ``call_tool()`` once this method returns.

        Each server entry in ``mcp_servers`` must have the shape returned
        by ``mcp_loader.load_for_sdk()``:

        - stdio: ``{type: "stdio", command: str, args: list[str], env: dict}``
        - SSE:   ``{type: "sse", url: str, env: dict}``

        The ``type`` key defaults to ``"stdio"`` when absent.

        Args:
            stack: AsyncExitStack owning the server/connection lifetimes.
                Servers are stopped automatically when the stack exits.

        Returns:
            List of tool defs in Anthropic format (name, description,
            input_schema). Empty when ``mcp`` is not installed or no
            servers are configured.
        """
        if not _MCP_AVAILABLE:
            if self._mcp_servers:
                logger.warning(
                    "MCP servers are configured but the 'mcp' package is not "
                    "installed — tool calls to these servers will fail. "
                    "Install it with: pip install mcp"
                )
            return []

        tools: list[dict] = []
        self._sessions = {}
        self._session_locks = {}

        for server_name, cfg in self._mcp_servers.items():
            try:
                transport = cfg.get("type", "stdio")

                if transport == "stdio":
                    params = StdioServerParameters(
                        command=cfg["command"],
                        args=cfg.get("args", []),
                        env=cfg.get("env") or None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))

                elif transport == "sse":
                    if not _SSE_AVAILABLE:
                        logger.warning(
                            "MCP server '%s' uses SSE transport but 'mcp' SSE "
                            "client is not available — skipping. "
                            "Ensure mcp[sse] is installed.",
                            server_name,
                        )
                        continue
                    read, write = await stack.enter_async_context(
                        sse_client(cfg["url"])
                    )

                else:
                    logger.warning(
                        "MCP server '%s' has unknown transport '%s' — skipping",
                        server_name,
                        transport,
                    )
                    continue

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                tool_list = await session.list_tools()
                for tool in tool_list.tools:
                    tools.append({
                        "name": f"{server_name}__{tool.name}",
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema,
                    })

                self._sessions[server_name] = session
                self._session_locks[server_name] = asyncio.Lock()
                logger.debug(
                    "MCP server '%s' (%s) started with %d tools",
                    server_name,
                    transport,
                    len(tool_list.tools),
                )

            except Exception as exc:
                logger.warning(
                    "Failed to start MCP server '%s': %s — skipping",
                    server_name,
                    exc,
                )

        return tools

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Dispatch a tool call to the appropriate MCP session.

        Acquires the per-server lock before calling the session so that
        concurrent requests to the same stdio server are serialized.
        Calls to different servers proceed concurrently.

        On ``BrokenPipeError``, ``ConnectionError``, or ``EOFError``, the
        dead session is evicted from the pool and a WARNING is logged.
        For all other exceptions an error string is returned (errors are
        never re-raised to keep the agentic loop alive).

        Args:
            tool_name: Prefixed name in the form ``{server}__{real_tool}``.
            tool_input: Argument dict from the model.

        Returns:
            Concatenated text from the result content, or an error description
            when the call fails.
        """
        server_name, _, real_name = tool_name.partition("__")
        session = self._sessions.get(server_name)
        if session is None:
            return f"Error: MCP server '{server_name}' not found or inactive."

        lock = self._session_locks.get(server_name)
        if lock is None:
            # Shouldn't happen in normal operation; create one defensively.
            lock = asyncio.Lock()
            self._session_locks[server_name] = lock

        async with lock:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(real_name, tool_input),
                    timeout=self._profile.mcp_timeout,
                )
                return "".join(
                    item.text
                    for item in result.content
                    if hasattr(item, "text") and item.text
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP tool '%s' timed out after %ss",
                    tool_name,
                    self._profile.mcp_timeout,
                )
                return f"Error: tool '{real_name}' timed out after {self._profile.mcp_timeout}s"
            except _DEAD_SESSION_ERRORS as exc:
                logger.warning(
                    "MCP server '%s' connection died (%s: %s) — evicting session",
                    server_name,
                    type(exc).__name__,
                    exc,
                )
                self._sessions.pop(server_name, None)
                self._session_locks.pop(server_name, None)
                return f"Error: MCP server '{server_name}' connection lost ({type(exc).__name__})"
            except Exception as exc:
                logger.warning("MCP tool '%s' failed: %s", tool_name, exc)
                return f"Error calling {real_name}: {exc}"
