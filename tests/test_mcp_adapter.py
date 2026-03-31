"""Unit tests for atelier.mcp_adapter — wraps McpSessionManager as @tool.

Tests validate:
- make_mcp_tools() returns StructuredTool instances
- each tool is named '{server}__{tool_name}'
- tool invocation delegates to McpSessionManager.call_tool()
- stale/missing session returns error string (does not raise)
- empty sessions dict returns empty list
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.tools import BaseTool, StructuredTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session_manager(
    sessions: dict | None = None,
) -> MagicMock:
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

    manager = _make_mock_session_manager(sessions={})
    tools = await make_mcp_tools(manager)
    assert tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_returns_base_tools() -> None:
    """Each returned tool must be a LangChain BaseTool."""
    from atelier.mcp_adapter import make_mcp_tools

    # Two MCP tool schemas (as returned by session.list_tools())
    fake_tool_a = MagicMock()
    fake_tool_a.name = "search"
    fake_tool_a.description = "Search the web."

    fake_tool_b = MagicMock()
    fake_tool_b.name = "read_file"
    fake_tool_b.description = "Read a file."

    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[fake_tool_a, fake_tool_b]))

    manager = _make_mock_session_manager(sessions={"my_server": session})
    tools = await make_mcp_tools(manager)

    assert len(tools) == 2
    for tool in tools:
        assert isinstance(tool, BaseTool)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_names_are_prefixed() -> None:
    """Tool names must be '{server_name}__{tool_name}'."""
    from atelier.mcp_adapter import make_mcp_tools

    fake_tool = MagicMock()
    fake_tool.name = "search"
    fake_tool.description = "Search."

    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[fake_tool]))

    manager = _make_mock_session_manager(sessions={"brave": session})
    tools = await make_mcp_tools(manager)

    assert tools[0].name == "brave__search"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_multiple_servers() -> None:
    """Tools from multiple servers are all returned."""
    from atelier.mcp_adapter import make_mcp_tools

    def _fake_session(tool_name: str) -> AsyncMock:
        t = MagicMock()
        t.name = tool_name
        t.description = f"Does {tool_name}."
        s = AsyncMock()
        s.list_tools = AsyncMock(return_value=MagicMock(tools=[t]))
        return s

    manager = _make_mock_session_manager(
        sessions={
            "server_a": _fake_session("tool_x"),
            "server_b": _fake_session("tool_y"),
        }
    )
    tools = await make_mcp_tools(manager)

    names = {t.name for t in tools}
    assert "server_a__tool_x" in names
    assert "server_b__tool_y" in names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_invoke_delegates_to_call_tool() -> None:
    """Invoking a wrapped tool must call manager.call_tool with prefixed name."""
    from atelier.mcp_adapter import make_mcp_tools

    fake_tool = MagicMock()
    fake_tool.name = "search"
    fake_tool.description = "Search."

    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[fake_tool]))

    manager = _make_mock_session_manager(sessions={"brave": session})
    manager.call_tool = AsyncMock(return_value="search results")

    tools = await make_mcp_tools(manager)
    search_tool = tools[0]

    result = await search_tool.ainvoke({"query": "hello"})
    manager.call_tool.assert_called_once_with("brave__search", {"query": "hello"})
    assert result == "search results"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_invoke_returns_error_string_on_exception() -> None:
    """If call_tool raises, the wrapper must return an error string, not raise."""
    from atelier.mcp_adapter import make_mcp_tools

    fake_tool = MagicMock()
    fake_tool.name = "search"
    fake_tool.description = "Search."

    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[fake_tool]))

    manager = _make_mock_session_manager(sessions={"brave": session})
    manager.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))

    tools = await make_mcp_tools(manager)
    result = await tools[0].ainvoke({"query": "hello"})

    assert "Error" in result
    assert "connection lost" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_make_mcp_tools_skips_server_on_list_tools_error() -> None:
    """If list_tools() raises for a server, that server's tools are skipped."""
    from atelier.mcp_adapter import make_mcp_tools

    bad_session = AsyncMock()
    bad_session.list_tools = AsyncMock(side_effect=RuntimeError("server down"))

    good_tool = MagicMock()
    good_tool.name = "ping"
    good_tool.description = "Ping."
    good_session = AsyncMock()
    good_session.list_tools = AsyncMock(return_value=MagicMock(tools=[good_tool]))

    manager = _make_mock_session_manager(
        sessions={"bad_server": bad_session, "good_server": good_session}
    )
    tools = await make_mcp_tools(manager)

    assert len(tools) == 1
    assert tools[0].name == "good_server__ping"
