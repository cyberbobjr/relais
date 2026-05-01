"""Unit tests for atelier.mcp_adapter — wraps McpSessionManager as LangChain tools.

Tests validate:
- make_mcp_tools() returns _BoundMcpTool instances (subclass of BaseTool)
- each tool is named '{server}__{tool_name}'
- tool invocation delegates to McpSessionManager.call_tool()
- call_tool exceptions are caught and returned as error strings
- a server whose tool listing fails is skipped; others are still returned
- empty sessions dict returns empty list
- adapter unavailable (ImportError) returns empty list with error log
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import BaseTool
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _AnySchema(BaseModel):
    """Minimal open args_schema used in tests where schema contents don't matter."""

    model_config = {"extra": "allow"}


def _make_raw_tool(name: str, description: str = "") -> MagicMock:
    """Return a mock object mimicking a StructuredTool from load_mcp_tools."""
    t = MagicMock()
    t.name = name
    t.description = description or f"Does {name}."
    t.args_schema = _AnySchema
    return t


def _make_session_manager(sessions: dict | None = None) -> MagicMock:
    """Return a MagicMock mimicking McpSessionManager."""
    manager = MagicMock()
    manager.sessions = sessions or {}
    manager.call_tool = AsyncMock(return_value="tool result")
    return manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mcp_adapter_imports() -> None:
    """atelier.mcp_adapter must be importable."""
    from atelier import mcp_adapter  # noqa: F401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_returns_empty_when_no_sessions() -> None:
    """make_mcp_tools returns [] when the session manager has no sessions."""
    from atelier.mcp_adapter import make_mcp_tools

    manager = _make_session_manager(sessions={})
    tools = await make_mcp_tools(manager)
    assert tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_returns_base_tools() -> None:
    """Each returned tool must be a LangChain BaseTool."""
    from atelier.mcp_adapter import make_mcp_tools

    raw = [_make_raw_tool("search"), _make_raw_tool("read_file")]

    with patch("atelier.mcp_adapter._load_mcp_tools", new=AsyncMock(return_value=raw)):
        session = MagicMock()
        manager = _make_session_manager(sessions={"srv": session})
        tools = await make_mcp_tools(manager)

    assert len(tools) == 2
    for tool in tools:
        assert isinstance(tool, BaseTool)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_names_are_prefixed() -> None:
    """Tool names must be '{server_name}__{tool_name}'."""
    from atelier.mcp_adapter import make_mcp_tools

    raw = [_make_raw_tool("search")]

    with patch("atelier.mcp_adapter._load_mcp_tools", new=AsyncMock(return_value=raw)):
        manager = _make_session_manager(sessions={"brave": MagicMock()})
        tools = await make_mcp_tools(manager)

    assert tools[0].name == "brave__search"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_multiple_servers() -> None:
    """Tools from multiple servers are all returned with distinct prefixed names."""
    from atelier.mcp_adapter import make_mcp_tools

    async def _per_session_load(session: object) -> list:
        return [_make_raw_tool(getattr(session, "_tool_name", "tool"))]

    session_a = MagicMock()
    session_a._tool_name = "tool_x"
    session_b = MagicMock()
    session_b._tool_name = "tool_y"

    with patch("atelier.mcp_adapter._load_mcp_tools", side_effect=_per_session_load):
        manager = _make_session_manager(
            sessions={"server_a": session_a, "server_b": session_b}
        )
        tools = await make_mcp_tools(manager)

    names = {t.name for t in tools}
    assert "server_a__tool_x" in names
    assert "server_b__tool_y" in names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_invoke_delegates_to_call_tool() -> None:
    """Invoking a wrapped tool must call manager.call_tool with prefixed name and input dict."""
    from atelier.mcp_adapter import make_mcp_tools

    raw = [_make_raw_tool("search")]

    with patch("atelier.mcp_adapter._load_mcp_tools", new=AsyncMock(return_value=raw)):
        manager = _make_session_manager(sessions={"brave": MagicMock()})
        manager.call_tool = AsyncMock(return_value="search results")
        tools = await make_mcp_tools(manager)

    search_tool = tools[0]
    result = await search_tool.ainvoke({"query": "hello"})

    manager.call_tool.assert_called_once()
    call_args = manager.call_tool.call_args
    assert call_args[0][0] == "brave__search"
    assert call_args[0][1].get("query") == "hello"
    assert result == "search results"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_invoke_returns_error_string_on_exception() -> None:
    """If call_tool raises, the wrapper must return an error string, not propagate."""
    from atelier.mcp_adapter import make_mcp_tools

    raw = [_make_raw_tool("search")]

    with patch("atelier.mcp_adapter._load_mcp_tools", new=AsyncMock(return_value=raw)):
        manager = _make_session_manager(sessions={"brave": MagicMock()})
        manager.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
        tools = await make_mcp_tools(manager)

    result = await tools[0].ainvoke({"query": "hello"})

    assert "Error" in result
    assert "connection lost" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_skips_server_on_load_error() -> None:
    """If _load_mcp_tools raises for a server, that server is skipped; others are returned."""
    from atelier.mcp_adapter import make_mcp_tools

    async def _selective_load(session: object) -> list:
        if getattr(session, "_fail", False):
            raise RuntimeError("server down")
        return [_make_raw_tool("ping")]

    bad_session = MagicMock()
    bad_session._fail = True
    good_session = MagicMock()
    good_session._fail = False

    with patch("atelier.mcp_adapter._load_mcp_tools", side_effect=_selective_load):
        manager = _make_session_manager(
            sessions={"bad_server": bad_session, "good_server": good_session}
        )
        tools = await make_mcp_tools(manager)

    assert len(tools) == 1
    assert tools[0].name == "good_server__ping"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_returns_empty_when_adapter_unavailable() -> None:
    """make_mcp_tools returns [] and logs an error when the adapter is not installed."""
    from atelier import mcp_adapter

    manager = _make_session_manager(sessions={"srv": MagicMock()})
    with patch.object(mcp_adapter, "_ADAPTER_AVAILABLE", False):
        tools = await mcp_adapter.make_mcp_tools(manager)
    assert tools == []
