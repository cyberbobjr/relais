"""Unit tests for atelier.sdk_executor — all claude_agent_sdk imports are mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from atelier.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Fake SDK types (distinct classes for reliable isinstance() checks)
# ---------------------------------------------------------------------------


class FakeTextBlock:
    """Fake TextBlock with a .text attribute."""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAssistantMessage:
    """Fake AssistantMessage with a .content list of blocks."""

    def __init__(self, texts: list[str]) -> None:
        self.content = [FakeTextBlock(t) for t in texts]


class FakeResultMessage:
    """Fake ResultMessage with a .subtype attribute."""

    def __init__(self, subtype: str) -> None:
        self.subtype = subtype


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(model: str = "claude-opus-4-5", max_turns: int = 10) -> ProfileConfig:
    """Build a minimal ProfileConfig for testing.

    Args:
        model: LLM model identifier.
        max_turns: Maximum agentic turns.

    Returns:
        A ProfileConfig instance.
    """
    return ProfileConfig(
        model=model,
        temperature=0.7,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=3, retry_delays=[2, 5, 15]),
        max_turns=max_turns,
    )


def _make_envelope(content: str = "Hello") -> Envelope:
    """Build a minimal Envelope for testing.

    Args:
        content: Message content.

    Returns:
        An Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id="discord:123",
        channel="discord",
        session_id="sess-001",
        correlation_id="corr-test-001",
    )


def _make_sdk_context_manager(messages: list) -> AsyncMock:
    """Build a mock ClaudeSDKClient async context manager.

    The mock client yields the given messages from receive_response().

    Args:
        messages: List of message objects to yield from receive_response().

    Returns:
        An async-context-manager mock for ClaudeSDKClient.
    """
    async def _aiter():
        for m in messages:
            yield m

    client = AsyncMock()
    client.query = AsyncMock()
    client.receive_response = MagicMock(return_value=_aiter())

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _sdk_patches(cm: AsyncMock):
    """Return a stack of patches replacing the module-level SDK names.

    Uses the Fake* classes so isinstance() behaves correctly in execute().

    Args:
        cm: The mock context manager to use for ClaudeSDKClient.

    Returns:
        A list of active patch context managers (use with nested `with`
        statements or apply them manually).
    """
    return [
        patch("atelier.sdk_executor.shutil.which", return_value="/usr/local/bin/claude"),
        patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm),
        patch("atelier.sdk_executor.ClaudeAgentOptions"),
        patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage),
        patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_text_reply() -> None:
    """execute() returns the concatenated text from AssistantMessage blocks."""
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([
        FakeAssistantMessage(["Hello"]),
        FakeResultMessage("success"),
    ])

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="You are helpful.", mcp_servers={})
        result = await executor.execute(envelope=_make_envelope(), context=[])
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == "Hello"


@pytest.mark.asyncio
async def test_execute_calls_stream_callback() -> None:
    """execute() calls stream_callback for each text chunk in AssistantMessage."""
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([
        FakeAssistantMessage(["chunk1"]),
        FakeAssistantMessage(["chunk2"]),
        FakeResultMessage("success"),
    ])

    callback_calls: list[str] = []

    async def stream_cb(text: str) -> None:
        callback_calls.append(text)

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="You are helpful.", mcp_servers={})
        await executor.execute(
            envelope=_make_envelope(),
            context=[],
            stream_callback=stream_cb,
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert callback_calls == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_execute_raises_sdk_execution_error_on_non_success() -> None:
    """execute() raises SDKExecutionError when ResultMessage.subtype != 'success'."""
    from atelier.sdk_executor import SDKExecutor, SDKExecutionError

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("error")])

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="You are helpful.", mcp_servers={})
        with pytest.raises(SDKExecutionError):
            await executor.execute(envelope=_make_envelope(), context=[])
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.mark.asyncio
async def test_cli_path_uses_shutil_which() -> None:
    """ClaudeAgentOptions is called with cli_path from shutil.which('claude')."""
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    mock_which = patch("atelier.sdk_executor.shutil.which", return_value="/custom/bin/claude")
    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which as which_m, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        await executor.execute(envelope=_make_envelope(), context=[])

    which_m.assert_called_with("claude")
    call_kwargs = options_m.call_args.kwargs
    assert call_kwargs.get("cli_path") == "/custom/bin/claude"


@pytest.mark.asyncio
async def test_execute_uses_profile_model_and_max_turns() -> None:
    """ClaudeAgentOptions receives model and max_turns from the ProfileConfig."""
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile(model="claude-opus-4-5", max_turns=42)
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_which = patch("atelier.sdk_executor.shutil.which", return_value="/usr/bin/claude")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        await executor.execute(envelope=_make_envelope(), context=[])

    call_kwargs = options_m.call_args.kwargs
    assert call_kwargs.get("model") == "claude-opus-4-5"
    assert call_kwargs.get("max_turns") == 42


@pytest.mark.asyncio
async def test_execute_no_stream_callback_works() -> None:
    """execute() works correctly when stream_callback is None."""
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([
        FakeAssistantMessage(["answer"]),
        FakeResultMessage("success"),
    ])

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        result = await executor.execute(
            envelope=_make_envelope(),
            context=[],
            stream_callback=None,
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == "answer"


@pytest.mark.unit
def test_sdk_executor_exposes_resilience_config() -> None:
    """SDKExecutor exposes the resilience config so callers can implement retry logic.

    The SDK does not retry on proxy/network errors; callers are expected to
    inspect executor.resilience and wrap execute() with their own backoff loop.
    """
    from atelier.sdk_executor import SDKExecutor

    resilience = ResilienceConfig(retry_attempts=3, retry_delays=[2, 5, 15], fallback_model=None)
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
# Phase 4.4 — Additional gap-filling tests (A1–A5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_prompt_with_non_empty_context() -> None:
    """_build_prompt returns context turns followed by the new user message.

    Given a two-turn context (user + assistant) and a new envelope message,
    the resulting prompt must be each turn prefixed with [role]: joined by
    newlines, with the envelope content appended as the final [user]: line.
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
    context = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    envelope = _make_envelope(content="next question")
    result = executor._build_prompt(envelope, context)
    assert result == "[user]: hello\n[assistant]: hi\n[user]: next question"


@pytest.mark.unit
def test_build_prompt_with_empty_context() -> None:
    """_build_prompt with empty context returns only the new user message line.

    There must be no leading newlines or empty lines when context is [].
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
    envelope = _make_envelope(content="first message")
    result = executor._build_prompt(envelope, [])
    assert result == "[user]: first message"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_with_cli_path_none_passes_none_to_options() -> None:
    """When shutil.which returns None, ClaudeAgentOptions is called with cli_path=None.

    The startup guard in Atelier.__init__ is the caller's responsibility.
    SDKExecutor itself must faithfully pass whatever shutil.which returns —
    including None — to ClaudeAgentOptions so the SDK can surface the error.
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    mock_which = patch("atelier.sdk_executor.shutil.which", return_value=None)
    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        await executor.execute(envelope=_make_envelope(), context=[])

    call_kwargs = options_m.call_args.kwargs
    assert call_kwargs.get("cli_path") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_with_mixed_text_and_non_text_blocks() -> None:
    """execute() concatenates only text blocks, skipping non-text block types.

    An AssistantMessage containing [TextBlock, ToolUseBlock, TextBlock] should
    produce "hello world" (text blocks only, non-text blocks ignored).
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()

    # Build mock blocks: two text blocks surrounding a block with no .text attribute
    text_block_1 = MagicMock()
    text_block_1.text = "hello"
    tool_block = MagicMock(spec=["id"])  # only has .id, no .text
    del tool_block.text  # ensure getattr(block, "text", None) returns None
    text_block_2 = MagicMock()
    text_block_2.text = " world"

    mixed_message = FakeAssistantMessage([])
    mixed_message.content = [text_block_1, tool_block, text_block_2]

    cm = _make_sdk_context_manager([mixed_message, FakeResultMessage("success")])

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        result = await executor.execute(envelope=_make_envelope(), context=[])
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == "hello world"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_returns_empty_string_when_no_assistant_message() -> None:
    """execute() returns "" when the stream contains only a ResultMessage success.

    No AssistantMessage before the final ResultMessage means no text was
    produced; execute() should return an empty string without raising.
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    patches = _sdk_patches(cm)
    for p in patches:
        p.start()
    try:
        executor = SDKExecutor(profile=profile, soul_prompt="soul", mcp_servers={})
        result = await executor.execute(envelope=_make_envelope(), context=[])
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == ""


# ---------------------------------------------------------------------------
# Phase 5 — Subagent support tests (T10–T12)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_with_subagents_passes_agents_to_options() -> None:
    """SDKExecutor passes agents= to ClaudeAgentOptions when subagents are provided.

    When SDKExecutor is constructed with a non-empty subagents dict, the agents
    kwarg forwarded to ClaudeAgentOptions must equal that dict.
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    fake_agent = MagicMock()
    subagents = {"memory-retriever": fake_agent}

    mock_which = patch("atelier.sdk_executor.shutil.which", return_value="/usr/bin/claude")
    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(
            profile=profile,
            soul_prompt="soul",
            mcp_servers={},
            subagents=subagents,
        )
        await executor.execute(envelope=_make_envelope(), context=[])

    call_kwargs = options_m.call_args.kwargs
    assert call_kwargs.get("agents") == subagents


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_with_subagents_does_not_set_allowed_tools() -> None:
    """SDKExecutor does NOT set allowed_tools when subagents dict is non-empty.

    The SDK makes the Task tool available implicitly when agents= is set.
    No explicit allowed_tools override is needed — passing one would
    incorrectly restrict other tools available to the principal agent.
    The allowed_tools kwarg must be absent (or None) even when subagents are
    present.
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    fake_agent = MagicMock()
    subagents = {"code-explorer": fake_agent}

    mock_which = patch("atelier.sdk_executor.shutil.which", return_value="/usr/bin/claude")
    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(
            profile=profile,
            soul_prompt="soul",
            mcp_servers={},
            subagents=subagents,
        )
        await executor.execute(envelope=_make_envelope(), context=[])

    call_kwargs = options_m.call_args.kwargs
    # allowed_tools must not be set (or be None) — the SDK handles Task implicitly
    allowed_tools = call_kwargs.get("allowed_tools")
    assert allowed_tools is None, (
        f"expected allowed_tools=None but got {allowed_tools!r}; "
        "the SDK exposes Task implicitly via agents=, no override required"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_with_empty_subagents_passes_agents_none() -> None:
    """SDKExecutor passes agents=None when subagents dict is empty.

    An empty dict must be normalized to None so the SDK does not receive
    an empty agents dict (which could differ from None in SDK behavior).
    """
    from atelier.sdk_executor import SDKExecutor

    profile = _make_profile()
    cm = _make_sdk_context_manager([FakeResultMessage("success")])

    mock_which = patch("atelier.sdk_executor.shutil.which", return_value="/usr/bin/claude")
    mock_client = patch("atelier.sdk_executor.ClaudeSDKClient", return_value=cm)
    mock_options = patch("atelier.sdk_executor.ClaudeAgentOptions")
    mock_am = patch("atelier.sdk_executor.AssistantMessage", new=FakeAssistantMessage)
    mock_rm = patch("atelier.sdk_executor.ResultMessage", new=FakeResultMessage)

    with mock_which, mock_client, mock_options as options_m, mock_am, mock_rm:
        executor = SDKExecutor(
            profile=profile,
            soul_prompt="soul",
            mcp_servers={},
            subagents={},
        )
        await executor.execute(envelope=_make_envelope(), context=[])

    call_kwargs = options_m.call_args.kwargs
    assert call_kwargs.get("agents") is None
