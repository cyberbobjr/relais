"""Unit tests for atelier.sdk_executor — uses anthropic.AsyncAnthropic mocks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from atelier.profile_loader import ProfileConfig, ResilienceConfig
from atelier.internal_tool import InternalTool


# ---------------------------------------------------------------------------
# Fake Anthropic SDK types
# ---------------------------------------------------------------------------


class FakeTextBlock:
    """Fake text content block."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeToolUseBlock:
    """Fake tool_use content block."""

    type = "tool_use"

    def __init__(self, tool_id: str, name: str, input: dict) -> None:
        self.id = tool_id
        self.name = name
        self.input = input


class FakeFinalMessage:
    """Fake final message returned by stream.get_final_message()."""

    def __init__(self, stop_reason: str, content: list | None = None) -> None:
        self.stop_reason = stop_reason
        self.content = content or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(model: str = "claude-opus-4-5", max_turns: int = 10) -> ProfileConfig:
    return ProfileConfig(
        model=model,
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=3, retry_delays=[2, 5, 15]),
        max_turns=max_turns,
    )


def _make_envelope(content: str = "Hello") -> Envelope:
    return Envelope(
        content=content,
        sender_id="discord:123",
        channel="discord",
        session_id="sess-001",
        correlation_id="corr-test-001",
    )


def _make_stream_cm(
    chunks: list[str],
    stop_reason: str = "end_turn",
    content: list | None = None,
):
    """Build an async context manager that mimics client.messages.stream().

    Args:
        chunks: Text chunks yielded by text_stream.
        stop_reason: Value of final_message.stop_reason.
        content: List of blocks in final_message.content.

    Returns:
        Async context manager mock yielding a stream_obj with text_stream
        and get_final_message().
    """
    async def _text_stream():
        for c in chunks:
            yield c

    stream_obj = MagicMock()
    stream_obj.text_stream = _text_stream()
    stream_obj.get_final_message = AsyncMock(
        return_value=FakeFinalMessage(stop_reason, content or [])
    )

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=stream_obj)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_executor(mock_client=None, profile=None, tools=None, mcp_servers=None):
    """Instantiate SDKExecutor with a patched AsyncAnthropic client.

    Args:
        mock_client: The mock to return from AsyncAnthropic().
        profile: ProfileConfig to use (defaults to _make_profile()).
        tools: InternalTool list (defaults to []).
        mcp_servers: MCP servers dict (defaults to {}).

    Returns:
        SDKExecutor instance with patched Anthropic client.
    """
    from atelier.sdk_executor import SDKExecutor

    if mock_client is None:
        mock_client = MagicMock()
    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        return SDKExecutor(
            profile=profile or _make_profile(),
            soul_prompt="You are helpful.",
            mcp_servers=mcp_servers if mcp_servers is not None else {},
            tools=tools or [],
        )


# ---------------------------------------------------------------------------
# execute() — basic behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_text_reply() -> None:
    """execute() returns concatenated text from the stream."""
    mock_client = MagicMock()
    mock_client.messages.stream.return_value = _make_stream_cm(
        ["Hello", " world"], stop_reason="end_turn"
    )

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(), soul_prompt="soul", mcp_servers={}
        )
        result = await executor.execute(envelope=_make_envelope(), context=[])

    assert result == "Hello world"


@pytest.mark.asyncio
async def test_execute_calls_stream_callback() -> None:
    """execute() calls stream_callback for each text chunk."""
    mock_client = MagicMock()
    mock_client.messages.stream.return_value = _make_stream_cm(
        ["chunk1", "chunk2"], stop_reason="end_turn"
    )

    callback_calls: list[str] = []

    async def stream_cb(text: str) -> None:
        callback_calls.append(text)

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(), soul_prompt="soul", mcp_servers={}
        )
        await executor.execute(
            envelope=_make_envelope(), context=[], stream_callback=stream_cb
        )

    assert callback_calls == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_execute_no_stream_callback_works() -> None:
    """execute() works correctly when stream_callback is None."""
    mock_client = MagicMock()
    mock_client.messages.stream.return_value = _make_stream_cm(
        ["answer"], stop_reason="end_turn"
    )

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(), soul_prompt="soul", mcp_servers={}
        )
        result = await executor.execute(
            envelope=_make_envelope(), context=[], stream_callback=None
        )

    assert result == "answer"


@pytest.mark.asyncio
async def test_execute_uses_profile_model_and_max_tokens() -> None:
    """messages.stream() receives model and max_tokens from ProfileConfig."""
    mock_client = MagicMock()
    mock_client.messages.stream.return_value = _make_stream_cm(
        [], stop_reason="end_turn"
    )

    profile = _make_profile(model="claude-haiku-4-5")
    profile = ProfileConfig(
        model="claude-haiku-4-5",
        temperature=0.5,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        max_turns=3,
    )

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=profile, soul_prompt="soul", mcp_servers={}
        )
        await executor.execute(envelope=_make_envelope(), context=[])

    kwargs = mock_client.messages.stream.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 512


# ---------------------------------------------------------------------------
# execute() — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_raises_sdk_error_on_api_status_error() -> None:
    """execute() raises SDKExecutionError on anthropic.APIStatusError."""
    import httpx
    import anthropic as _anthropic
    from atelier.sdk_executor import SDKExecutionError

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {"request-id": "test-id"}
    mock_response.request = MagicMock(spec=httpx.Request)
    api_err = _anthropic.APIStatusError("server error", response=mock_response, body=None)

    mock_client = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=api_err)
    cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.messages.stream.return_value = cm

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(), soul_prompt="soul", mcp_servers={}
        )
        with pytest.raises(SDKExecutionError, match="500"):
            await executor.execute(envelope=_make_envelope(), context=[])


@pytest.mark.asyncio
async def test_execute_propagates_connection_error_unwrapped() -> None:
    """execute() lets APIConnectionError propagate WITHOUT wrapping it in SDKExecutionError.

    Transient network errors must NOT be converted to SDKExecutionError
    (which would cause Atelier to ACK and route to DLQ). Instead they must
    propagate as-is so _handle_message catches them in the generic except
    branch and returns False (message stays in PEL for re-delivery).
    """
    import anthropic as _anthropic
    from atelier.sdk_executor import SDKExecutionError

    conn_err = _anthropic.APIConnectionError(request=MagicMock())

    mock_client = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=conn_err)
    cm.__aexit__ = AsyncMock(return_value=False)
    mock_client.messages.stream.return_value = cm

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(), soul_prompt="soul", mcp_servers={}
        )
        # Must raise the original APIConnectionError, NOT SDKExecutionError
        with pytest.raises(_anthropic.APIConnectionError):
            await executor.execute(envelope=_make_envelope(), context=[])


@pytest.mark.asyncio
async def test_execute_exits_after_max_turns() -> None:
    """execute() stops the agentic loop after max_turns iterations without hanging.

    When the model always returns stop_reason='tool_use', the loop must exit
    cleanly after max_turns and return whatever text was accumulated.
    """
    mock_client = MagicMock()

    tool_block = FakeToolUseBlock("t-loop", "list_skills", {})
    # Every turn: stop_reason=tool_use — loop would run forever without the cap
    loop_cm = _make_stream_cm(["partial "], stop_reason="tool_use", content=[tool_block])

    mock_client.messages.stream.return_value = loop_cm
    # side_effect is not set: always returns the same tool_use mock

    tool = InternalTool(
        name="list_skills",
        description="list",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda: "- skill_a",
    )

    max_turns = 3
    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(max_turns=max_turns),
            soul_prompt="soul",
            mcp_servers={},
            tools=[tool],
        )
        # Must return without raising; stream is called exactly max_turns times
        result = await executor.execute(envelope=_make_envelope(), context=[])

    assert mock_client.messages.stream.call_count == max_turns
    assert "partial" in result  # text accumulated across turns


# ---------------------------------------------------------------------------
# execute() — tool-use agentic loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_handles_tool_use_loop() -> None:
    """execute() performs a second turn when stop_reason is tool_use."""
    mock_client = MagicMock()

    tool_block = FakeToolUseBlock("tool-1", "list_skills", {})
    turn1_content = [FakeTextBlock("thinking..."), tool_block]

    turn1_cm = _make_stream_cm(
        ["thinking..."], stop_reason="tool_use", content=turn1_content
    )
    turn2_cm = _make_stream_cm(["Final answer"], stop_reason="end_turn")

    mock_client.messages.stream.side_effect = [turn1_cm, turn2_cm]

    tool = InternalTool(
        name="list_skills",
        description="list skills",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda: "skill_a",
    )

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(max_turns=5),
            soul_prompt="soul",
            mcp_servers={},
            tools=[tool],
        )
        result = await executor.execute(envelope=_make_envelope(), context=[])

    assert "Final answer" in result
    assert mock_client.messages.stream.call_count == 2


@pytest.mark.asyncio
async def test_execute_injects_tool_result_into_messages() -> None:
    """After tool execution, the result is injected as a user message."""
    mock_client = MagicMock()

    tool_block = FakeToolUseBlock("t-1", "list_skills", {})
    turn1_content = [tool_block]
    turn1_cm = _make_stream_cm([], stop_reason="tool_use", content=turn1_content)
    turn2_cm = _make_stream_cm(["done"], stop_reason="end_turn")

    mock_client.messages.stream.side_effect = [turn1_cm, turn2_cm]

    tool = InternalTool(
        name="list_skills",
        description="list",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda: "- skill_a",
    )

    captured_messages: list = []

    def capture_stream(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        # Return the appropriate mock based on call count
        return mock_client.messages.stream.side_effect[0]

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=_make_profile(max_turns=5),
            soul_prompt="soul",
            mcp_servers={},
            tools=[tool],
        )
        await executor.execute(envelope=_make_envelope(), context=[])

    # Second call to messages.stream must include tool_result in messages
    second_call_messages = mock_client.messages.stream.call_args_list[1].kwargs["messages"]
    tool_result_msgs = [
        m for m in second_call_messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_msgs) == 1


# ---------------------------------------------------------------------------
# resilience config exposure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executor_exposes_resilience_config() -> None:
    """SDKExecutor exposes the resilience config so callers can implement retry."""
    resilience = ResilienceConfig(retry_attempts=3, retry_delays=[2, 5, 15], fallback_model=None)
    mock_client = MagicMock()

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(
            profile=ProfileConfig(
                model="test-model",
                temperature=0.7,
                max_tokens=1024,
                max_turns=10,
                resilience=resilience,
            ),
            soul_prompt="test",
            mcp_servers={},
        )

    assert executor.resilience.retry_attempts == 3
    assert executor.resilience.retry_delays == [2, 5, 15]
    assert executor.resilience.fallback_model is None


# ---------------------------------------------------------------------------
# _build_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_messages_empty_context() -> None:
    """_build_messages with empty context returns a single user message."""
    executor = _make_executor()
    envelope = _make_envelope(content="first message")
    messages = executor._build_messages(envelope, [])

    assert messages == [{"role": "user", "content": "first message"}]


@pytest.mark.unit
def test_build_messages_with_context() -> None:
    """_build_messages appends the envelope content after context turns."""
    executor = _make_executor()
    context = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    envelope = _make_envelope(content="next question")
    messages = executor._build_messages(envelope, context)

    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1] == {"role": "assistant", "content": "hi"}
    assert messages[2] == {"role": "user", "content": "next question"}


@pytest.mark.unit
def test_build_messages_inserts_empty_user_when_context_starts_with_assistant() -> None:
    """_build_messages inserts an empty user turn when context starts with assistant."""
    executor = _make_executor()
    context = [{"role": "assistant", "content": "hi"}]
    envelope = _make_envelope(content="hello")
    messages = executor._build_messages(envelope, context)

    assert messages[0] == {"role": "user", "content": ""}
    assert messages[1] == {"role": "assistant", "content": "hi"}
    assert messages[2] == {"role": "user", "content": "hello"}


@pytest.mark.unit
def test_build_messages_filters_unknown_roles() -> None:
    """_build_messages drops turns with roles other than user/assistant."""
    executor = _make_executor()
    context = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "ignored"},
        {"role": "assistant", "content": "hello"},
    ]
    messages = executor._build_messages(_make_envelope(), context)

    roles = [m["role"] for m in messages]
    assert "system" not in roles
    assert len([r for r in roles if r == "user"]) == 2  # context + envelope


# ---------------------------------------------------------------------------
# _get_anthropic_tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_anthropic_tools_includes_internal_tools() -> None:
    """_get_anthropic_tools returns internal tools in Anthropic format."""
    tool = InternalTool(
        name="my_tool",
        description="does something",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=lambda x: x,
    )
    executor = _make_executor(tools=[tool])
    result = executor._get_anthropic_tools([])

    assert len(result) == 1
    assert result[0]["name"] == "my_tool"
    assert result[0]["description"] == "does something"


@pytest.mark.unit
def test_get_anthropic_tools_merges_mcp_tools() -> None:
    """_get_anthropic_tools merges internal and MCP tools."""
    tool = InternalTool(
        name="internal_tool",
        description="internal",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda: "x",
    )
    mcp_tool = {
        "name": "server__mcp_tool",
        "description": "from mcp",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    executor = _make_executor(tools=[tool])
    result = executor._get_anthropic_tools([mcp_tool])

    names = [t["name"] for t in result]
    assert "internal_tool" in names
    assert "server__mcp_tool" in names


# ---------------------------------------------------------------------------
# _call_tool dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_internal_handler() -> None:
    """_call_tool invokes the internal handler for known tool names."""
    from atelier.mcp_session_manager import McpSessionManager

    tool = InternalTool(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        handler=lambda msg: f"echoed: {msg}",
    )
    executor = _make_executor(tools=[tool])
    mcp_manager = McpSessionManager(_make_profile(), {})
    result = await executor._call_tool("echo", {"msg": "hello"}, mcp_manager)
    assert result == "echoed: hello"


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_async_internal_handler() -> None:
    """_call_tool awaits async internal handlers correctly."""
    from atelier.mcp_session_manager import McpSessionManager

    async def async_handler(x: str) -> str:
        return f"async: {x}"

    tool = InternalTool(
        name="async_tool",
        description="async",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=async_handler,
    )
    executor = _make_executor(tools=[tool])
    mcp_manager = McpSessionManager(_make_profile(), {})
    result = await executor._call_tool("async_tool", {"x": "val"}, mcp_manager)
    assert result == "async: val"


# ---------------------------------------------------------------------------
# mcp_max_tools — _get_anthropic_tools caps MCP tool list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_anthropic_tools_caps_mcp_tools_at_mcp_max_tools() -> None:
    """_get_anthropic_tools includes at most mcp_max_tools MCP tools."""
    profile = ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        mcp_timeout=10,
        mcp_max_tools=2,
    )
    executor = _make_executor(profile=profile)

    mcp_tools = [
        {"name": f"srv__tool_{i}", "description": "", "input_schema": {}}
        for i in range(5)
    ]
    result = executor._get_anthropic_tools(mcp_tools)

    mcp_names = [t["name"] for t in result if t["name"].startswith("srv__")]
    assert len(mcp_names) == 2


@pytest.mark.unit
def test_get_anthropic_tools_zero_mcp_max_tools_exposes_no_mcp_tools() -> None:
    """_get_anthropic_tools exposes zero MCP tools when mcp_max_tools is 0."""
    profile = ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        mcp_timeout=5,
        mcp_max_tools=0,
    )
    executor = _make_executor(profile=profile)

    mcp_tools = [
        {"name": "srv__tool_a", "description": "", "input_schema": {}},
        {"name": "srv__tool_b", "description": "", "input_schema": {}},
    ]
    result = executor._get_anthropic_tools(mcp_tools)

    mcp_names = [t["name"] for t in result if t["name"].startswith("srv__")]
    assert mcp_names == []


@pytest.mark.unit
def test_get_anthropic_tools_internal_tools_not_counted_in_cap() -> None:
    """Internal tools are never capped by mcp_max_tools."""
    internal = InternalTool(
        name="my_internal",
        description="internal",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda: "x",
    )
    profile = ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        mcp_timeout=10,
        mcp_max_tools=1,
    )
    executor = _make_executor(profile=profile, tools=[internal])

    mcp_tools = [
        {"name": "srv__tool_a", "description": "", "input_schema": {}},
        {"name": "srv__tool_b", "description": "", "input_schema": {}},
    ]
    result = executor._get_anthropic_tools(mcp_tools)

    names = [t["name"] for t in result]
    # Internal tool always present
    assert "my_internal" in names
    # MCP tools capped at 1
    mcp_names = [n for n in names if n.startswith("srv__")]
    assert len(mcp_names) == 1


# ---------------------------------------------------------------------------
# load_profiles — mcp_timeout and mcp_max_tools parsed from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_parses_mcp_timeout_and_max_tools(tmp_path) -> None:
    """load_profiles() correctly parses mcp_timeout and mcp_max_tools per profile."""
    import textwrap
    from atelier.profile_loader import load_profiles

    yaml_content = textwrap.dedent("""\
        profiles:
          test_profile:
            model: test-model
            temperature: 0.7
            max_tokens: 1024
            mcp_timeout: 15
            mcp_max_tools: 25
            memory_scope: own
            resilience:
              retry_attempts: 1
              retry_delays: [1]
    """)
    config_file = tmp_path / "profiles.yaml"
    config_file.write_text(yaml_content)

    profiles = load_profiles(config_path=config_file)
    p = profiles["test_profile"]

    assert p.mcp_timeout == 15
    assert p.mcp_max_tools == 25


@pytest.mark.unit
def test_load_profiles_defaults_mcp_timeout_and_max_tools(tmp_path) -> None:
    """load_profiles() uses defaults (10, 20) when mcp_timeout/mcp_max_tools are absent."""
    import textwrap
    from atelier.profile_loader import load_profiles

    yaml_content = textwrap.dedent("""\
        profiles:
          minimal:
            model: test-model
            temperature: 0.5
            max_tokens: 512
            memory_scope: own
            resilience:
              retry_attempts: 1
              retry_delays: [1]
    """)
    config_file = tmp_path / "profiles.yaml"
    config_file.write_text(yaml_content)

    profiles = load_profiles(config_path=config_file)
    p = profiles["minimal"]

    assert p.mcp_timeout == 10
    assert p.mcp_max_tools == 20


# ---------------------------------------------------------------------------
# max_tokens stop_reason — warning branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_logs_warning_on_max_tokens(caplog) -> None:
    """_run_agentic_loop emits a WARNING when stop_reason is 'max_tokens'."""
    import logging

    mock_client = MagicMock()
    mock_client.messages.stream.return_value = _make_stream_cm(
        ["truncated reply"], stop_reason="max_tokens"
    )

    profile = ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=256,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        max_turns=3,
    )

    with patch("atelier.sdk_executor.anthropic.AsyncAnthropic", return_value=mock_client):
        from atelier.sdk_executor import SDKExecutor
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        with caplog.at_level(logging.WARNING, logger="atelier.sdk_executor"):
            result = await executor.execute(envelope=_make_envelope(), context=[])

    assert result == "truncated reply"
    assert any("max_tokens" in record.message for record in caplog.records)
    assert any("256" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# skills_tools — path traversal rejection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_skill_rejects_path_with_slash(tmp_path) -> None:
    """read_skill returns an error string for skill names containing '/'."""
    from atelier.skills_tools import make_skills_tools

    tools = make_skills_tools(tmp_path)
    read_tool = next(t for t in tools if t.name == "read_skill")
    result = read_tool.handler(skill_name="../secret")
    assert "invalid" in result.lower() or "error" in result.lower()


@pytest.mark.unit
def test_read_skill_rejects_double_dot(tmp_path) -> None:
    """read_skill returns an error string for skill names containing '..'."""
    from atelier.skills_tools import make_skills_tools

    tools = make_skills_tools(tmp_path)
    read_tool = next(t for t in tools if t.name == "read_skill")
    result = read_tool.handler(skill_name="..")
    assert "invalid" in result.lower() or "error" in result.lower()


@pytest.mark.unit
def test_read_skill_rejects_backslash(tmp_path) -> None:
    """read_skill returns an error string for skill names containing backslash."""
    from atelier.skills_tools import make_skills_tools

    tools = make_skills_tools(tmp_path)
    read_tool = next(t for t in tools if t.name == "read_skill")
    result = read_tool.handler(skill_name="foo\\..\\bar")
    assert "invalid" in result.lower() or "error" in result.lower()


@pytest.mark.unit
def test_read_skill_returns_content_for_valid_skill(tmp_path) -> None:
    """read_skill returns the SKILL.md content for a valid, existing skill."""
    from atelier.skills_tools import make_skills_tools

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill\nSome content.")

    tools = make_skills_tools(tmp_path)
    read_tool = next(t for t in tools if t.name == "read_skill")
    result = read_tool.handler(skill_name="my-skill")
    assert "My Skill" in result
    assert "Some content" in result
