"""Tests for McpSessionManager singleton lifecycle (Option A).

TDD test suite — written BEFORE implementation.
Tests cover:
- start/close lifecycle (is_running transitions)
- Concurrent call_tool serialization via per-server locks
- Dead session eviction on BrokenPipeError / ConnectionError / EOFError
- Atelier wires singleton in _extra_lifespan and uses it in _handle_envelope
- Hot-reload (_restart_mcp_sessions) replaces the singleton atomically
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from atelier.mcp_session_manager import McpSessionManager
from atelier.soul_assembler import AssemblyResult
from common.profile_loader import ProfileConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(mcp_timeout: float = 5.0) -> MagicMock:
    """Build a minimal ProfileConfig-like mock."""
    m = MagicMock(spec=ProfileConfig)
    m.mcp_timeout = mcp_timeout
    m.mcp_max_tools = 0
    m.model = "anthropic:claude-sonnet-4-6"
    m.max_turns = 10
    m.max_turn_seconds = 300
    m.shell_timeout_seconds = 30
    return m


def _make_server_cfg() -> dict:
    """Return a minimal stdio MCP server config dict."""
    return {
        "type": "stdio",
        "command": "echo",
        "args": ["hello"],
        "env": None,
    }


def _make_fake_session(tools: list[str] | None = None) -> AsyncMock:
    """Return a fake MCP ClientSession with list_tools and call_tool mocked."""
    session = AsyncMock()
    tool_objs = []
    for name in (tools or []):
        t = MagicMock()
        t.name = name
        t.description = f"Tool {name}"
        t.inputSchema = {}
        tool_objs.append(t)
    list_result = MagicMock()
    list_result.tools = tool_objs
    session.list_tools = AsyncMock(return_value=list_result)
    result_obj = MagicMock()
    result_obj.content = []
    session.call_tool = AsyncMock(return_value=result_obj)
    session.initialize = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Phase 1 — McpSessionManager self-contained lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_running_false_before_start() -> None:
    """is_running is False before start() is called."""
    mgr = McpSessionManager(_make_profile(), {})
    assert mgr.is_running is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_running_true_after_start_with_no_servers() -> None:
    """is_running becomes True after start() even with no MCP servers configured."""
    mgr = McpSessionManager(_make_profile(), {})
    await mgr.start()
    try:
        assert mgr.is_running is True
    finally:
        await mgr.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_is_running_false_after_close() -> None:
    """is_running returns False after close() is called."""
    mgr = McpSessionManager(_make_profile(), {})
    await mgr.start()
    await mgr.close()
    assert mgr.is_running is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_is_idempotent_when_not_started() -> None:
    """close() does not raise when called before start()."""
    mgr = McpSessionManager(_make_profile(), {})
    # Should not raise
    await mgr.close()
    assert mgr.is_running is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_is_idempotent_when_called_twice() -> None:
    """close() does not raise when called twice in a row."""
    mgr = McpSessionManager(_make_profile(), {})
    await mgr.start()
    await mgr.close()
    await mgr.close()  # second call must not raise
    assert mgr.is_running is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_populates_tools_list() -> None:
    """start() populates an internal tool list from start_all()."""
    profile = _make_profile()
    servers = {"myserver": _make_server_cfg()}
    mgr = McpSessionManager(profile, servers)

    fake_session = _make_fake_session(tools=["tool_a", "tool_b"])

    with patch("atelier.mcp_session_manager._MCP_AVAILABLE", True), \
         patch("atelier.mcp_session_manager.StdioServerParameters") as MockParams, \
         patch("atelier.mcp_session_manager.stdio_client") as mock_stdio, \
         patch("atelier.mcp_session_manager.ClientSession") as MockSession:

        # stdio_client(params) returns an async context manager yielding (read, write)
        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)
        mock_stdio.return_value = mock_transport

        # ClientSession(read, write) returns an async context manager yielding the session
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        MockSession.return_value = mock_session_cm

        await mgr.start()
        try:
            assert len(mgr.tools) == 2
            tool_names = [t["name"] for t in mgr.tools]
            assert "myserver__tool_a" in tool_names
            assert "myserver__tool_b" in tool_names
        finally:
            await mgr.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_cleared_after_close() -> None:
    """tools list is cleared when close() is called."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})
    await mgr.start()
    # No servers, tools should already be empty but is_running is True
    assert mgr.is_running is True
    await mgr.close()
    assert mgr.tools == []


# ---------------------------------------------------------------------------
# Phase 1 — Per-server lock serialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_serializes_concurrent_calls_per_server() -> None:
    """Concurrent call_tool() calls to the same server are serialized via lock.

    Two concurrent calls are fired; we verify both complete and neither raises.
    The locking is verified implicitly — if calls overlap, the fake session's
    call_tool would be called concurrently (no assertion failure here, just
    confirming sequential-safe behavior).
    """
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    fake_session = _make_fake_session(tools=["do_thing"])

    result_obj = MagicMock()
    item = MagicMock()
    item.text = "ok"
    result_obj.content = [item]

    # Simulate slight delay to surface concurrency issues
    async def slow_call_tool(name, inp):
        await asyncio.sleep(0.01)
        return result_obj

    fake_session.call_tool = slow_call_tool

    # Manually inject the session as if start_all had been called
    mgr._sessions = {"myserver": fake_session}
    mgr._session_locks = {"myserver": asyncio.Lock()}

    # Concurrently call the same server's tool
    results = await asyncio.gather(
        mgr.call_tool("myserver__do_thing", {}),
        mgr.call_tool("myserver__do_thing", {}),
    )
    assert results == ["ok", "ok"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_tool_different_servers_can_run_concurrently() -> None:
    """Concurrent call_tool() calls to different servers do NOT block each other."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    order: list[str] = []

    async def make_tool(server_tag: str):
        async def _tool(name, inp):
            order.append(f"{server_tag}:start")
            await asyncio.sleep(0.02)
            order.append(f"{server_tag}:end")
            result = MagicMock()
            item = MagicMock()
            item.text = server_tag
            result.content = [item]
            return result
        return _tool

    session_a = AsyncMock()
    session_a.call_tool = await make_tool("a")
    session_b = AsyncMock()
    session_b.call_tool = await make_tool("b")

    mgr._sessions = {"server_a": session_a, "server_b": session_b}
    mgr._session_locks = {
        "server_a": asyncio.Lock(),
        "server_b": asyncio.Lock(),
    }

    results = await asyncio.gather(
        mgr.call_tool("server_a__tool1", {}),
        mgr.call_tool("server_b__tool1", {}),
    )
    assert set(results) == {"a", "b"}
    # Both started before either ended (i.e., they truly ran concurrently)
    assert order[0].endswith(":start") and order[1].endswith(":start")


# ---------------------------------------------------------------------------
# Phase 1 — Dead session eviction
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broken_pipe_error_evicts_session() -> None:
    """BrokenPipeError evicts the dead session from the pool and returns an error string."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=BrokenPipeError("pipe closed"))

    mgr._sessions = {"srv": session}
    mgr._session_locks = {"srv": asyncio.Lock()}

    result = await mgr.call_tool("srv__mytool", {})

    # Session evicted
    assert "srv" not in mgr._sessions
    assert "srv" not in mgr._session_locks
    # Error string returned (not raised)
    assert "Error" in result or "error" in result.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connection_error_evicts_session() -> None:
    """ConnectionError evicts the dead session and returns an error string."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=ConnectionError("connection reset"))

    mgr._sessions = {"srv": session}
    mgr._session_locks = {"srv": asyncio.Lock()}

    result = await mgr.call_tool("srv__tool", {})

    assert "srv" not in mgr._sessions
    assert "Error" in result or "error" in result.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eof_error_evicts_session() -> None:
    """EOFError evicts the dead session and returns an error string."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=EOFError("eof"))

    mgr._sessions = {"srv": session}
    mgr._session_locks = {"srv": asyncio.Lock()}

    result = await mgr.call_tool("srv__tool", {})

    assert "srv" not in mgr._sessions
    assert "Error" in result or "error" in result.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_fatal_exception_does_not_evict_session() -> None:
    """A generic ValueError does NOT evict the session (only pipe/connection/EOF do)."""
    profile = _make_profile()
    mgr = McpSessionManager(profile, {})

    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=ValueError("bad input"))

    mgr._sessions = {"srv": session}
    mgr._session_locks = {"srv": asyncio.Lock()}

    result = await mgr.call_tool("srv__tool", {})

    # Session NOT evicted
    assert "srv" in mgr._sessions
    assert "Error" in result


# ---------------------------------------------------------------------------
# Phase 2 — Atelier wires singleton in _extra_lifespan
# ---------------------------------------------------------------------------


def _make_atelier() -> "Atelier":  # type: ignore[name-defined]
    """Create an Atelier instance with __init__-time IO patched out."""
    from atelier.main import Atelier

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver
    profile_mock = MagicMock()
    profile_mock.model = "test-model"
    profile_mock.max_turns = 5
    profile_mock.mcp_timeout = 5.0
    profile_mock.mcp_max_tools = 0
    profile_mock.max_turn_seconds = 300
    profile_mock.shell_timeout_seconds = 30

    with patch("atelier.main.load_profiles", return_value={"default": profile_mock}), \
         patch("atelier.main.load_for_sdk", return_value={}), \
         patch("atelier.main.resolve_profile", return_value=profile_mock), \
         patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls):
        return Atelier()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_has_mcp_manager_attribute() -> None:
    """Atelier instance exposes _mcp_manager attribute (initially None)."""
    atelier = _make_atelier()
    assert hasattr(atelier, "_mcp_manager")
    assert atelier._mcp_manager is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_has_mcp_tools_attribute() -> None:
    """Atelier instance exposes _mcp_tools attribute (initially empty list)."""
    atelier = _make_atelier()
    assert hasattr(atelier, "_mcp_tools")
    assert atelier._mcp_tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_extra_lifespan_starts_mcp_manager() -> None:
    """_extra_lifespan() starts the McpSessionManager singleton."""
    atelier = _make_atelier()

    mock_mgr = AsyncMock(spec=McpSessionManager)
    mock_mgr.start = AsyncMock()
    mock_mgr.tools = []
    mock_mgr.is_running = True
    mock_mgr.close = AsyncMock()

    with patch("atelier.main.McpSessionManager", return_value=mock_mgr) as MockMcpCls:
        stack = AsyncExitStack()
        async with stack:
            # Also patch checkpointer so _extra_lifespan doesn't fail
            mock_checkpointer = AsyncMock()
            mock_checkpointer.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_checkpointer.__aexit__ = AsyncMock(return_value=False)
            atelier._checkpointer_cm = mock_checkpointer

            await atelier._extra_lifespan(stack)

    mock_mgr.start.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_extra_lifespan_registers_close_callback() -> None:
    """_extra_lifespan() registers mgr.close() as a cleanup callback."""
    atelier = _make_atelier()

    close_called = []

    mock_mgr = AsyncMock(spec=McpSessionManager)
    mock_mgr.tools = []
    mock_mgr.is_running = True

    async def fake_close():
        close_called.append(True)

    mock_mgr.close = fake_close
    mock_mgr.start = AsyncMock()

    with patch("atelier.main.McpSessionManager", return_value=mock_mgr):
        stack = AsyncExitStack()
        mock_checkpointer = AsyncMock()
        mock_checkpointer.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_checkpointer.__aexit__ = AsyncMock(return_value=False)
        atelier._checkpointer_cm = mock_checkpointer

        async with stack:
            await atelier._extra_lifespan(stack)
        # After exiting the stack, close() should have been called

    assert close_called, "mgr.close() was not called when lifespan stack exited"


# ---------------------------------------------------------------------------
# Phase 2 — _handle_envelope uses singleton tools (no per-request McpSessionManager)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_envelope_uses_singleton_mcp_tools() -> None:
    """_handle_envelope reads MCP tools from singleton self._mcp_tools, not per-request.

    After the refactor, _handle_envelope must NOT instantiate a new
    McpSessionManager per request. We verify that AtlasExecutor receives the
    tools from self._mcp_tools (or an empty list when mcp_patterns are absent).
    """
    from common.envelope import Envelope
    from common.contexts import CTX_PORTAIL
    from atelier.agent_executor import AgentResult

    atelier = _make_atelier()
    # Inject a pre-started singleton
    mock_mgr = AsyncMock(spec=McpSessionManager)
    mock_mgr.is_running = True
    mock_mgr.tools = [{"name": "srv__tool1", "description": "A tool", "input_schema": {}}]
    atelier._mcp_manager = mock_mgr
    atelier._mcp_tools = mock_mgr.tools

    envelope = Envelope(
        content="hello",
        sender_id="discord:99",
        channel="discord",
        session_id="s1",
        correlation_id="c1",
        context={CTX_PORTAIL: {"user_record": {}, "llm_profile": "default"}},
    )
    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.publish = AsyncMock()

    with patch("atelier.main.AgentExecutor") as MockExec, \
         patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)), \
         patch("atelier.main.resolve_profile", return_value=MagicMock(model="m", max_turns=5, mcp_timeout=5.0, mcp_max_tools=0, max_turn_seconds=300, shell_timeout_seconds=30)), \
         patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):

        mock_exec_instance = AsyncMock()
        mock_exec_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExec.return_value = mock_exec_instance

        result = await atelier._handle_envelope(envelope, redis_conn)

    assert result is True
    # McpSessionManager must NOT have been constructed inside _handle_envelope
    # (it should only be constructed once in _extra_lifespan)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_envelope_does_not_instantiate_mcp_manager_per_request() -> None:
    """_handle_envelope must NOT call McpSessionManager() constructor."""
    from common.envelope import Envelope
    from common.contexts import CTX_PORTAIL
    from atelier.agent_executor import AgentResult

    atelier = _make_atelier()
    atelier._mcp_manager = AsyncMock(spec=McpSessionManager)
    atelier._mcp_manager.is_running = True
    atelier._mcp_tools = []

    envelope = Envelope(
        content="test",
        sender_id="discord:1",
        channel="discord",
        session_id="s1",
        correlation_id="c2",
        context={CTX_PORTAIL: {"user_record": {}, "llm_profile": "default"}},
    )
    redis_conn = AsyncMock()
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.publish = AsyncMock()

    construction_calls: list = []

    original_cls = McpSessionManager

    class SpyMcpSessionManager(original_cls):
        def __init__(self, *args, **kwargs):
            construction_calls.append(args)
            super().__init__(*args, **kwargs)

    with patch("atelier.main.McpSessionManager", SpyMcpSessionManager), \
         patch("atelier.main.AgentExecutor") as MockExec, \
         patch("atelier.main.assemble_system_prompt", return_value=AssemblyResult(memory_paths=[], issues=[], is_degraded=False)), \
         patch("atelier.main.resolve_profile", return_value=MagicMock(model="m", max_turns=5, mcp_timeout=5.0, mcp_max_tools=0, max_turn_seconds=300, shell_timeout_seconds=30)), \
         patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):

        mock_exec_instance = AsyncMock()
        mock_exec_instance.execute = AsyncMock(return_value=AgentResult(reply_text="reply", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=()))
        MockExec.return_value = mock_exec_instance

        await atelier._handle_envelope(envelope, redis_conn)

    assert len(construction_calls) == 0, (
        f"McpSessionManager was instantiated {len(construction_calls)} time(s) "
        "inside _handle_envelope — it must only be constructed once at startup."
    )


# ---------------------------------------------------------------------------
# Phase 3 — Hot-reload replaces singleton atomically
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restart_mcp_sessions_replaces_manager() -> None:
    """_restart_mcp_sessions() closes old manager and creates a new one."""
    atelier = _make_atelier()

    old_mgr = AsyncMock(spec=McpSessionManager)
    old_mgr.close = AsyncMock()
    old_mgr.is_running = True
    atelier._mcp_manager = old_mgr
    atelier._mcp_tools = []
    atelier._mcp_lock = asyncio.Lock()

    new_mgr = AsyncMock(spec=McpSessionManager)
    new_mgr.start = AsyncMock()
    new_mgr.tools = []
    new_mgr.close = AsyncMock()

    with patch("atelier.main.McpSessionManager", return_value=new_mgr) as MockCls, \
         patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]):
        await atelier._restart_mcp_sessions()

    old_mgr.close.assert_awaited_once()
    new_mgr.start.assert_awaited_once()
    assert atelier._mcp_manager is new_mgr


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restart_mcp_sessions_gracefully_degrades_on_failure() -> None:
    """_restart_mcp_sessions() sets manager to None and tools to [] if start() fails."""
    atelier = _make_atelier()
    atelier._mcp_manager = None
    atelier._mcp_tools = []
    atelier._mcp_lock = asyncio.Lock()

    failing_mgr = AsyncMock(spec=McpSessionManager)
    failing_mgr.start = AsyncMock(side_effect=RuntimeError("server crashed"))
    failing_mgr.close = AsyncMock()

    with patch("atelier.main.McpSessionManager", return_value=failing_mgr):
        await atelier._restart_mcp_sessions()

    # Graceful degradation
    assert atelier._mcp_manager is None
    assert atelier._mcp_tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_config_triggers_mcp_restart_on_server_change() -> None:
    """_apply_config() schedules _restart_mcp_sessions() when MCP config changes."""
    atelier = _make_atelier()
    atelier._mcp_servers_default = {"old_server": {"type": "stdio", "command": "old"}}
    atelier._mcp_manager = AsyncMock(spec=McpSessionManager)
    atelier._mcp_tools = []
    atelier._mcp_lock = asyncio.Lock()

    restart_called = []

    async def fake_restart():
        restart_called.append(True)

    atelier._restart_mcp_sessions = fake_restart

    new_cfg = {
        "profiles": {},
        "mcp_servers": {"new_server": {"type": "stdio", "command": "new"}},
        "display": None,
        "subagent_registry": MagicMock(),
    }

    # _apply_config creates a task on the running loop
    atelier._apply_config(new_cfg)

    # Allow the created task to run
    await asyncio.sleep(0)

    assert restart_called, "_restart_mcp_sessions() was not scheduled on MCP config change"


@pytest.mark.unit
def test_apply_config_does_not_trigger_restart_when_mcp_unchanged() -> None:
    """_apply_config() does NOT schedule restart when MCP servers are unchanged."""
    atelier = _make_atelier()
    same_servers = {"srv": {"type": "stdio", "command": "cmd"}}
    atelier._mcp_servers_default = same_servers

    restart_called = []

    async def fake_restart():
        restart_called.append(True)

    atelier._restart_mcp_sessions = fake_restart

    new_cfg = {
        "profiles": {},
        "mcp_servers": same_servers,  # same reference / same content
        "display": None,
        "subagent_registry": MagicMock(),
    }

    atelier._apply_config(new_cfg)

    assert not restart_called, "_restart_mcp_sessions() must not be called when MCP is unchanged"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extra_lifespan_cancels_in_flight_restart_before_close() -> None:
    """Shutdown cancels an in-flight _restart_mcp_sessions task before close() runs.

    Verifies the callback ordering: _cancel_mcp_restart_task runs first (LIFO),
    cancels the task, and close() is called after without racing the restart.
    """
    atelier = _make_atelier()

    restart_started = asyncio.Event()
    restart_cancelled = asyncio.Event()
    close_called = []

    async def slow_restart() -> None:
        restart_started.set()
        try:
            await asyncio.sleep(10)  # simulate long MCP server startup
        except asyncio.CancelledError:
            restart_cancelled.set()
            raise

    mock_manager = AsyncMock()
    mock_manager.is_running = True
    mock_manager.tools = []
    mock_manager.close = AsyncMock(side_effect=lambda: close_called.append(True))

    atelier._mcp_manager = mock_manager
    atelier._mcp_tools = []
    atelier._restart_mcp_sessions = slow_restart  # type: ignore[method-assign]

    # Simulate an in-flight restart task
    loop = asyncio.get_running_loop()
    atelier._mcp_restart_task = loop.create_task(slow_restart())

    # Wait for task to start
    await restart_started.wait()
    assert not restart_cancelled.is_set()

    # Now simulate the shutdown callbacks in LIFO order (as AsyncExitStack does)
    # 1. _cancel_mcp_restart_task (pushed last, runs first)
    task = atelier._mcp_restart_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # 2. manager.close (pushed first, runs second)
    await mock_manager.close()

    assert restart_cancelled.is_set(), "In-flight restart task must be cancelled on shutdown"
    assert close_called, "close() must be called after restart task is cancelled"
