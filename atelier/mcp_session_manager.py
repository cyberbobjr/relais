"""McpSessionManager — lifecycle and dispatch for MCP servers in the Atelier.

Separates MCP infrastructure (server start, session management, tool dispatch)
from the agentic loop logic in SDKExecutor.
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


class McpSessionManager:
    """Manages lifecycle and tool dispatch for MCP servers.

    Responsibilities:
    - Start MCP servers (stdio / SSE) within an AsyncExitStack
    - Collect tool schemas in Anthropic format
    - Dispatch tool calls to the appropriate session with timeout

    Usage::

        manager = McpSessionManager(profile, mcp_servers)
        async with contextlib.AsyncExitStack() as stack:
            mcp_tools = await manager.start_all(stack)
            # ... agentic loop ...
            result = await manager.call_tool("server__tool_name", tool_input)
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

    async def call_tool(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Dispatch a tool call to the appropriate MCP session.

        Args:
            tool_name: Prefixed name in the form ``{server}__{real_tool}``.
            tool_input: Argument dict from the model.

        Returns:
            Concatenated text from the result content, or an error description
            when the call fails (errors are not re-raised to keep the loop alive).
        """
        server_name, _, real_name = tool_name.partition("__")
        session = self._sessions.get(server_name)
        if session is None:
            return f"Error: MCP server '{server_name}' not found or inactive."
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
                "MCP tool '%s' timed out after %ss", tool_name, self._profile.mcp_timeout
            )
            return f"Error: tool '{real_name}' timed out after {self._profile.mcp_timeout}s"
        except Exception as exc:
            logger.warning("MCP tool '%s' failed: %s", tool_name, exc)
            return f"Error calling {real_name}: {exc}"
