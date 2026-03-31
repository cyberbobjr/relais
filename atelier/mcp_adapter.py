"""MCP adapter — wraps McpSessionManager sessions as LangChain @tool instances.

The McpSessionManager (unchanged from the SDK era) owns the MCP server lifecycle
and provides ``call_tool(prefixed_name, input_dict) -> str``.

This module bridges from McpSessionManager to the DeepAgents/LangChain tool
interface by generating a BaseTool per MCP tool exposed by active sessions.

Each generated tool:
- is named ``{server_name}__{tool_name}`` (same prefix convention as before)
- delegates calls asynchronously to ``manager.call_tool()``
- accepts arbitrary kwargs (MCP tool schemas vary per server)
- catches exceptions and returns an error string (keeps the agentic loop alive)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, PrivateAttr

if TYPE_CHECKING:
    from atelier.mcp_session_manager import McpSessionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permissive args schema for MCP tools (schema varies per server)
# ---------------------------------------------------------------------------


class _McpToolArgs(BaseModel):
    """Accept any keyword arguments — schema is determined per-tool by the MCP server."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# BaseTool subclass — holds session_manager reference via PrivateAttr
# ---------------------------------------------------------------------------


class _McpTool(BaseTool):
    """LangChain BaseTool that forwards calls to McpSessionManager.call_tool()."""

    name: str
    description: str
    args_schema: type[BaseModel] = _McpToolArgs  # type: ignore[assignment]

    _prefixed_name: str = PrivateAttr()
    _session_manager: "McpSessionManager" = PrivateAttr()

    def __init__(
        self,
        prefixed_name: str,
        session_manager: "McpSessionManager",
        **data: object,
    ) -> None:
        super().__init__(**data)
        self._prefixed_name = prefixed_name
        self._session_manager = session_manager

    def _to_args_and_kwargs(  # type: ignore[override]
        self, tool_input: str | dict, tool_call_id: str | None = None
    ) -> tuple[tuple, dict]:
        """Pass input dict directly as kwargs, bypassing the empty-schema shortcut.

        LangChain's default implementation treats schemas with no declared fields
        as "no-arg tools" and returns ``(), {}``. We override this to forward the
        full input dict so MCP tools can receive arbitrary arguments.
        """
        if isinstance(tool_input, dict):
            return (), tool_input.copy()
        return (tool_input,), {}

    def _run(self, **kwargs: object) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async variant via ainvoke().")

    async def _arun(self, **kwargs: object) -> str:  # type: ignore[override]
        try:
            return await self._session_manager.call_tool(self._prefixed_name, kwargs)
        except Exception as exc:
            logger.warning(
                "MCP tool '%s' raised during invocation: %s", self._prefixed_name, exc
            )
            return f"Error calling '{self._prefixed_name}': {exc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def make_mcp_tools(session_manager: "McpSessionManager") -> list[BaseTool]:
    """Create LangChain tool wrappers for all active MCP sessions.

    Iterates over ``session_manager.sessions`` (dict mapping server name →
    MCP ClientSession), calls ``list_tools()`` on each session, and returns
    one tool per MCP tool discovered.

    If a session's ``list_tools()`` raises, that server is skipped and a
    warning is logged — the remaining servers are still wrapped.

    Args:
        session_manager: A McpSessionManager instance whose ``.sessions``
            property maps server names to active MCP ClientSession objects.

    Returns:
        List of tool instances, one per MCP tool discovered across all active
        sessions. Empty when no sessions are configured.
    """
    tools: list[BaseTool] = []

    for server_name, session in session_manager.sessions.items():
        try:
            tool_list = await session.list_tools()
            for mcp_tool in tool_list.tools:
                prefixed_name = f"{server_name}__{mcp_tool.name}"
                description = getattr(mcp_tool, "description", "") or ""
                tool = _McpTool(
                    name=prefixed_name,
                    description=description or f"MCP tool: {prefixed_name}",
                    prefixed_name=prefixed_name,
                    session_manager=session_manager,
                )
                tools.append(tool)
        except Exception as exc:
            logger.warning(
                "Failed to list tools for MCP server '%s': %s — skipping",
                server_name,
                exc,
            )

    return tools
