"""Unit tests for atelier.mcp_session_manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(mcp_timeout: int = 10, mcp_max_tools: int = 20) -> ProfileConfig:
    return ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
        mcp_timeout=mcp_timeout,
        mcp_max_tools=mcp_max_tools,
    )


def _make_manager(profile=None, mcp_servers=None):
    """Instantiate McpSessionManager with given profile and servers."""
    from atelier.mcp_session_manager import McpSessionManager

    return McpSessionManager(
        profile=profile or _make_profile(),
        mcp_servers=mcp_servers if mcp_servers is not None else {},
    )


# ---------------------------------------------------------------------------
# call_tool — error cases (no active sessions needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_returns_error_when_server_not_found() -> None:
    """call_tool returns an error string when the MCP server is not in sessions."""
    manager = _make_manager()
    result = await manager.call_tool("unknown__tool", {})
    assert "not found" in result or "unknown" in result


# ---------------------------------------------------------------------------
# call_tool — timeout handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_passes_timeout_to_wait_for() -> None:
    """call_tool wraps session.call_tool with asyncio.wait_for using mcp_timeout."""
    manager = _make_manager(profile=_make_profile(mcp_timeout=7))

    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="ok")]

    mock_wf = AsyncMock(return_value=fake_result)
    mock_session = AsyncMock()
    manager._sessions = {"srv": mock_session}

    with patch("atelier.mcp_session_manager.asyncio.wait_for", mock_wf):
        await manager.call_tool("srv__tool", {})

    mock_wf.assert_called_once()
    assert mock_wf.call_args.kwargs.get("timeout") == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_returns_error_on_timeout() -> None:
    """call_tool returns a descriptive error string on asyncio.TimeoutError."""
    import asyncio as _asyncio

    manager = _make_manager(profile=_make_profile(mcp_timeout=3))
    mock_session = AsyncMock()
    manager._sessions = {"srv": mock_session}

    with patch(
        "atelier.mcp_session_manager.asyncio.wait_for",
        AsyncMock(side_effect=_asyncio.TimeoutError()),
    ):
        result = await manager.call_tool("srv__tool", {})

    assert "timed out" in result
    assert "3s" in result


# ---------------------------------------------------------------------------
# call_tool — generic exception handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_returns_error_string_on_exception() -> None:
    """call_tool returns a descriptive string instead of raising on unexpected errors."""
    manager = _make_manager()
    mock_session = AsyncMock()
    manager._sessions = {"srv": mock_session}

    with patch(
        "atelier.mcp_session_manager.asyncio.wait_for",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await manager.call_tool("srv__a_tool", {})

    assert "boom" in result or "Error" in result


# ---------------------------------------------------------------------------
# start_all — MCP unavailable
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_all_returns_empty_when_mcp_unavailable() -> None:
    """start_all returns empty list when _MCP_AVAILABLE is False."""
    import contextlib
    from atelier.mcp_session_manager import McpSessionManager

    manager = McpSessionManager(
        profile=_make_profile(),
        mcp_servers={"a_server": {"type": "stdio", "command": "echo"}},
    )

    with patch("atelier.mcp_session_manager._MCP_AVAILABLE", False):
        async with contextlib.AsyncExitStack() as stack:
            tools = await manager.start_all(stack)

    assert tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_all_returns_empty_when_no_servers() -> None:
    """start_all returns empty list when mcp_servers is empty."""
    import contextlib

    manager = _make_manager(mcp_servers={})

    with patch("atelier.mcp_session_manager._MCP_AVAILABLE", False):
        async with contextlib.AsyncExitStack() as stack:
            tools = await manager.start_all(stack)

    assert tools == []
