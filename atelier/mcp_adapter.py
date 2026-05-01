"""MCP adapter — wraps McpSessionManager sessions as LangChain BaseTool instances.

Uses ``load_mcp_tools()`` from ``langchain-mcp-adapters`` to harvest proper
Pydantic schemas from active MCP sessions, then rebinds execution through
``McpSessionManager.call_tool()`` to preserve per-server ``asyncio.Lock``,
``mcp_timeout`` enforcement, and dead-session eviction.

Each generated tool:
- is named ``{server_name}__{tool_name}`` (prefix convention preserved)
- delegates ``_arun`` to ``manager.call_tool()``, which holds lock/timeout/eviction
- exposes the Pydantic model from ``load_mcp_tools`` as ``args_schema``
- catches exceptions and returns an error string (keeps the agentic loop alive)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, PrivateAttr

try:
    from langchain_mcp_adapters.tools import load_mcp_tools as _load_mcp_tools

    _ADAPTER_AVAILABLE = True
except ImportError:
    _load_mcp_tools = None  # type: ignore[assignment]
    _ADAPTER_AVAILABLE = False

if TYPE_CHECKING:
    from atelier.mcp_session_manager import McpSessionManager

logger = logging.getLogger(__name__)


class _BoundMcpTool(BaseTool):
    """LangChain BaseTool that binds schema from load_mcp_tools but routes
    execution through McpSessionManager.call_tool().

    This preserves the manager's per-server asyncio.Lock, mcp_timeout
    enforcement, dead-session eviction, and result formatting while gaining
    proper Pydantic validation from the langchain-mcp-adapters schema.
    """

    name: str
    description: str
    args_schema: type[BaseModel] | None = None

    _session_manager: "McpSessionManager" = PrivateAttr()

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(
        self,
        session_manager: "McpSessionManager",
        **data: object,
    ) -> None:
        """Initialise the bound tool.

        Args:
            session_manager: Running McpSessionManager that owns the session
                lock, timeout enforcement, and dead-session eviction logic.
            **data: Remaining keyword arguments forwarded to BaseTool
                (``name``, ``description``, ``args_schema``).
        """
        super().__init__(**data)
        self._session_manager = session_manager

    def _to_args_and_kwargs(  # type: ignore[override]
        self,
        tool_input: str | dict,
        tool_call_id: str | None = None,
    ) -> tuple[tuple, dict]:
        """Bypass schema validation — pass raw input dict directly to _arun.

        load_mcp_tools() may return args_schema as a raw JSON-Schema dict rather
        than a Pydantic model class.  LangChain's default _parse_input would then
        strip all fields (no declared model_fields to iterate over).  Returning
        a copy of the raw dict here ensures _arun always receives the full tool input.
        """
        if isinstance(tool_input, str):
            return (tool_input,), {}
        return (), dict(tool_input)

    def _run(self, **kwargs: object) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async variant via ainvoke().")

    async def _arun(self, **kwargs: object) -> str:  # type: ignore[override]
        """Delegate execution to McpSessionManager.call_tool().

        Args:
            **kwargs: Tool arguments as validated by args_schema.

        Returns:
            Formatted result string from the MCP server, or an error string if
            the call raises.
        """
        try:
            return await self._session_manager.call_tool(self.name, kwargs)
        except Exception as exc:
            logger.warning(
                "MCP tool '%s' raised during invocation: %s", self.name, exc
            )
            return f"Error calling '{self.name}': {exc}"


async def make_mcp_tools(session_manager: "McpSessionManager") -> list[BaseTool]:
    """Create LangChain tool wrappers for all active MCP sessions.

    Uses ``load_mcp_tools()`` from ``langchain-mcp-adapters`` to harvest proper
    Pydantic schemas from each active ``ClientSession``, then wraps each tool in
    ``_BoundMcpTool`` so execution routes through ``McpSessionManager.call_tool()``
    (preserving per-server locks, ``mcp_timeout``, and dead-session eviction).

    Tool names follow the ``{server}__{tool}`` convention so ``ToolPolicy``
    patterns and ``allowed_mcp_tools`` entries in ``portail.yaml`` remain valid.

    If ``langchain-mcp-adapters`` is not installed, an error is logged and an
    empty list is returned so the agentic loop degrades gracefully.

    If a session's tool listing raises, that server is skipped with a warning
    and the remaining servers are still wrapped.  All sessions are queried
    concurrently so startup latency does not scale with the number of servers.

    Args:
        session_manager: Running McpSessionManager whose ``.sessions`` maps
            server names to active MCP ``ClientSession`` objects.

    Returns:
        List of ``_BoundMcpTool`` instances, one per MCP tool discovered across
        all active sessions. Empty when no sessions are configured or when the
        adapter library is unavailable.
    """
    if not _ADAPTER_AVAILABLE:
        logger.error(
            "langchain-mcp-adapters not installed — MCP tools unavailable. "
            "Run: uv add 'langchain-mcp-adapters>=0.1.0,<0.2.0'"
        )
        return []

    async def _list_one(server_name: str, session: object) -> list[BaseTool]:
        try:
            raw_tools = await _load_mcp_tools(session)
            return [
                _BoundMcpTool(
                    name=f"{server_name}__{raw_tool.name}",
                    description=raw_tool.description or f"MCP tool: {server_name}__{raw_tool.name}",
                    args_schema=raw_tool.args_schema,
                    session_manager=session_manager,
                )
                for raw_tool in raw_tools
            ]
        except Exception as exc:
            logger.warning(
                "Failed to list tools for MCP server '%s': %s — skipping",
                server_name,
                exc,
            )
            return []

    results = await asyncio.gather(
        *(_list_one(name, sess) for name, sess in session_manager.sessions.items())
    )
    return [tool for server_tools in results for tool in server_tools]
