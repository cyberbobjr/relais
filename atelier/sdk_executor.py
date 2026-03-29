"""SDK-based executor for the Atelier brick.

Replaces the httpx/LiteLLM executor with the claude-agent-sdk.
The sdk imports are deferred to execute() to allow mocking in tests
and graceful degradation when the package is not installed.

Bug #677 workaround: cli_path=shutil.which("claude") ensures that the
system claude CLI binary is used, which respects ANTHROPIC_BASE_URL,
allowing the LiteLLM proxy to route calls to any backend.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Awaitable, Callable

from atelier.profile_loader import ProfileConfig
from common.envelope import Envelope

# Module-level sentinel so patch targets are stable and testable.
# The real imports happen inside execute() for optional-dependency safety,
# but we re-export the names at module level so tests can patch them directly
# via `atelier.sdk_executor.ClaudeSDKClient` etc.
try:
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        AgentDefinition,
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
    )
except ImportError:  # package not installed in this environment
    AgentDefinition = None  # type: ignore[assignment,misc]
    AssistantMessage = None  # type: ignore[assignment,misc]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    ResultMessage = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


class SDKExecutionError(Exception):
    """Raised when the claude-agent-sdk returns a non-success result."""


class SDKExecutor:
    """Executes LLM calls via the claude-agent-sdk.

    Attributes:
        resilience: ResilienceConfig from the profile, exposed so callers can
            implement retry/backoff logic around execute(). The SDK itself does
            NOT retry on proxy or network errors (e.g. LiteLLM 502/503 or
            httpx.ConnectError); that responsibility belongs to the caller.
        _profile: LLM profile configuration (model, max_turns, etc.).
        _soul_prompt: Assembled system prompt string.
        _mcp_servers: MCP server config dict for ClaudeAgentOptions.
        _subagents: Optional dict of AgentDefinition instances for subagent
            invocation. The SDK exposes Task implicitly when agents= is set;
            no allowed_tools override is required.
    """

    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        mcp_servers: dict,
        subagents: dict | None = None,
    ) -> None:
        """Initialise the executor with profile, soul prompt, MCP servers, and subagents.

        Args:
            profile: ProfileConfig specifying model, max_turns, and resilience.
            soul_prompt: Pre-assembled multi-layer system prompt string.
            mcp_servers: Dict mapping server names to their config, as expected
                by ClaudeAgentOptions.
            subagents: Optional dict mapping subagent names to AgentDefinition
                instances. The SDK makes Task available implicitly when agents=
                is set; no explicit allowed_tools override is needed. An empty
                dict is treated equivalently to None (no subagents).
        """
        self._profile = profile
        self._soul_prompt = soul_prompt
        self._mcp_servers = mcp_servers
        self._subagents: dict | None = subagents if subagents else None
        self.resilience = profile.resilience

    async def execute(
        self,
        envelope: Envelope,
        context: list[dict],
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Call the claude-agent-sdk and return the full assistant reply.

        Builds a prompt from the conversation context and the envelope content,
        opens a ClaudeSDKClient session, streams the response, and raises
        SDKExecutionError if the final ResultMessage is not a success.

        Retry responsibility: The claude CLI subprocess handles 429 rate-limit
        responses internally, but does NOT retry on proxy/network errors such as
        LiteLLM 502/503/504 responses or httpx.ConnectError. Callers that need
        resilience against infrastructure failures should wrap this method with
        their own backoff loop, using ``self.resilience`` (a ResilienceConfig)
        for retry_attempts and retry_delays values.

        Args:
            envelope: The task envelope being processed.
            context: List of prior role/content message dicts (conversation
                history retrieved from Souvenir).
            stream_callback: Optional async callable invoked with each text
                chunk as it arrives. When None, chunks are accumulated silently.

        Returns:
            The complete assistant reply text (all chunks concatenated).

        Raises:
            SDKExecutionError: The SDK returned a ResultMessage whose subtype
                is not "success".
        """
        # NOTE: permission_mode="bypassPermissions" grants the principal agent
        # and all subagents (when agents= is set) unrestricted tool access.
        # This level is appropriate for trusted, single-user deployments but
        # MUST be reviewed before production multi-user or public deployments.
        options = ClaudeAgentOptions(
            cli_path=shutil.which("claude"),
            env={
                "ANTHROPIC_BASE_URL": os.environ.get(
                    "ANTHROPIC_BASE_URL", "http://localhost:4000"
                ),
                "ANTHROPIC_API_KEY": os.environ.get(
                    "ANTHROPIC_API_KEY",
                    os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
                ),
            },
            system_prompt=self._soul_prompt,
            model=self._profile.model,
            max_turns=self._profile.max_turns,
            mcp_servers=self._mcp_servers,
            permission_mode="bypassPermissions",
            agents=self._subagents,
        )

        prompt = self._build_prompt(envelope, context)
        full_reply = ""

        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        text = getattr(block, "text", None)
                        if text:
                            full_reply += text
                            if stream_callback is not None:
                                await stream_callback(text)
                elif isinstance(message, ResultMessage):
                    if message.subtype != "success":
                        raise SDKExecutionError(
                            f"SDK returned non-success result: {message.subtype}"
                        )
                    break

        return full_reply

    def _build_prompt(self, envelope: Envelope, context: list[dict]) -> str:
        """Build a single prompt string from context history and the new message.

        Args:
            envelope: The current task envelope (its content is the new user
                message appended at the end).
            context: List of prior role/content dicts from memory.

        Returns:
            A formatted string with each turn prefixed by [role]: and the new
            user message appended as the final line.
        """
        lines: list[str] = []
        for turn in context:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            lines.append(f"[{role}]: {content}")
        lines.append(f"[user]: {envelope.content}")
        return "\n".join(lines)
