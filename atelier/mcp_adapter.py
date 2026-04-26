"""MCP adapter — wraps McpSessionManager sessions as LangChain @tool instances.

The McpSessionManager (unchanged from the SDK era) owns the MCP server lifecycle
and provides ``call_tool(prefixed_name, input_dict) -> str``.

This module bridges from McpSessionManager to the DeepAgents/LangChain tool
interface by generating a BaseTool per MCP tool exposed by active sessions.

Each generated tool:
- is named ``{server_name}__{tool_name}`` (same prefix convention as before)
- delegates calls asynchronously to ``manager.call_tool()``
- exposes ``mcp_tool.inputSchema`` verbatim as ``args_schema`` (a JSON Schema dict)
  so the LLM receives each argument's name, type, description and required flag
- catches exceptions and returns an error string (keeps the agentic loop alive)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, PrivateAttr

if TYPE_CHECKING:
    from atelier.mcp_session_manager import McpSessionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fallback schema — used only when inputSchema is absent or not an object
# ---------------------------------------------------------------------------

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# BaseTool subclass — holds session_manager reference via PrivateAttr
# ---------------------------------------------------------------------------


class _McpTool(BaseTool):
    """LangChain BaseTool that forwards calls to McpSessionManager.call_tool().

    ``args_schema`` is set per-instance to the raw ``inputSchema`` dict from
    the MCP server, giving the LLM the full JSON Schema (property descriptions,
    types, required constraints) without any lossy Pydantic conversion.
    """

    name: str
    description: str
    # LangChain's ArgsSchema = TypeBaseModel | dict[str, Any]; we use the dict path.
    args_schema: dict[str, Any] = _EMPTY_OBJECT_SCHEMA  # type: ignore[assignment]

    _prefixed_name: str = PrivateAttr()
    _session_manager: "McpSessionManager" = PrivateAttr()

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
        """Forward the input dict as kwargs unchanged.

        With a dict args_schema, LangChain's _parse_input already returns the
        raw dict. We just unpack it here so _arun receives named kwargs.
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

    ``mcp_tool.inputSchema`` is passed verbatim as ``args_schema`` so the LLM
    sees the full JSON Schema with per-argument descriptions, types, and
    required constraints — exactly as the MCP server declared them.

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

                # Use the MCP server's inputSchema directly as args_schema.
                # This preserves every property description, type, and required
                # constraint so the LLM knows exactly what each argument expects.
                raw_schema = getattr(mcp_tool, "inputSchema", None)
                args_schema: dict[str, Any] = (
                    raw_schema
                    if isinstance(raw_schema, dict)
                    else _EMPTY_OBJECT_SCHEMA
                )

                tool = _McpTool(
                    name=prefixed_name,
                    description=description or f"MCP tool: {prefixed_name}",
                    args_schema=args_schema,
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
