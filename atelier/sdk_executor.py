"""SDK-based executor for the Atelier brick.

Uses the ``anthropic`` Python SDK (AsyncAnthropic) with an explicit tool-use
agentic loop. MCP stdio servers are started via the ``mcp`` Python SDK.
Compatible with LiteLLM proxy via ANTHROPIC_BASE_URL.

Error contract (critical for at-least-once delivery):
- ``anthropic.APIStatusError``  → non-retriable, wrapped in ``SDKExecutionError``
  → caller ACKs and routes to DLQ.
- ``anthropic.APIConnectionError`` → transient, propagates unwrapped
  → caller does NOT ACK, message stays in PEL for re-delivery.
- Any other exception → propagates unwrapped (same PEL re-delivery behaviour).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from typing import Awaitable, Callable

import anthropic

from atelier.internal_tool import InternalTool
from atelier.mcp_session_manager import McpSessionManager
from atelier.profile_loader import ProfileConfig
from common.envelope import Envelope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class SDKExecutionError(Exception):
    """Raised when the agentic loop encounters a non-retriable failure.

    Routed to the DLQ by Atelier and ACKed so the message is not redelivered.
    """


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class SDKExecutor:
    """Executes LLM calls via the Anthropic Python SDK with an agentic loop.

    Supports internal (Python) tools and external MCP stdio servers.
    Tool use is handled with an explicit multi-turn loop: the model requests
    tools, results are injected as user messages, and the loop continues until
    stop_reason is "end_turn" or max_turns is exhausted.

    Attributes:
        resilience: ResilienceConfig from the profile, exposed so callers can
            implement retry/backoff logic around execute().
            # TODO(Phase 5): implement retry/backoff in Atelier._handle_message
            # using executor.resilience before routing to DLQ.
    """

    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        mcp_servers: dict,
        tools: list[InternalTool] | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        """Initialise the executor.

        Args:
            profile: LLM profile (model, max_turns, max_tokens, resilience).
            soul_prompt: Pre-assembled system prompt string.
            mcp_servers: Dict mapping server names to {command, args, env} config.
            tools: Optional list of InternalTool instances for native Python tools.
            client: Optional shared AsyncAnthropic instance.  When provided the
                executor reuses the caller's connection pool instead of creating
                its own.  When omitted a new client is created from env vars
                (useful in tests and standalone use).
        """
        self._profile = profile
        self._soul_prompt = soul_prompt
        self._mcp_servers = mcp_servers
        self._internal_tools: dict[str, InternalTool] = {
            t.name: t for t in (tools or [])
        }
        # Pre-compute Anthropic-format schemas once — reused every agentic turn.
        self._internal_tool_schemas: list[dict] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in (tools or [])
        ]
        self.resilience = profile.resilience
        if client is not None:
            self._client = client
        else:
            # API key resolution priority: ANTHROPIC_API_KEY > ANTHROPIC_AUTH_TOKEN.
            # Falls back to an empty string when neither is set, which defers the
            # auth failure to the first API call.
            self._client = anthropic.AsyncAnthropic(
                api_key=os.environ.get(
                    "ANTHROPIC_API_KEY",
                    os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
                ),
                base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:4000"),
            )

    async def execute(
        self,
        envelope: Envelope,
        context: list[dict],
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Run the agentic loop and return the complete assistant reply.

        Builds structured messages from context + envelope, starts MCP servers,
        then runs the tool-use loop until the model stops or max_turns is hit.

        Args:
            envelope: Current task envelope.
            context: Prior conversation turns from Souvenir.
            stream_callback: Optional async callable invoked with each text
                chunk as it arrives from the model.

        Returns:
            Complete assistant reply (all text blocks across all turns joined).

        Raises:
            SDKExecutionError: Non-retriable HTTP error (4xx/5xx status).
                Caller should ACK and route to DLQ.
            anthropic.APIConnectionError: Transient network error.
                Propagates unwrapped so the caller can leave the message in the
                PEL for automatic re-delivery (no ACK).
        """
        messages = self._build_messages(envelope, context)
        mcp_manager = McpSessionManager(self._profile, self._mcp_servers)

        try:
            async with contextlib.AsyncExitStack() as stack:
                mcp_tools = await mcp_manager.start_all(stack)
                return await self._run_agentic_loop(
                    messages, mcp_tools, mcp_manager, stream_callback
                )
        except (anthropic.RateLimitError, anthropic.InternalServerError):
            # Transient errors — propagate unwrapped so Atelier returns False,
            # leaving the message in the PEL for automatic re-delivery (no ACK).
            raise
        except anthropic.APIStatusError as exc:
            # Non-retriable: bad request, auth error, quota exceeded, etc.
            raise SDKExecutionError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc
        # anthropic.APIConnectionError is intentionally NOT caught here.
        # It propagates to Atelier._handle_message which returns False,
        # leaving the message in the PEL for re-delivery.

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _run_agentic_loop(
        self,
        messages: list[dict],
        mcp_tools: list[dict],
        mcp_manager: McpSessionManager,
        stream_callback: Callable[[str], Awaitable[None]] | None,
    ) -> str:
        """Multi-turn agentic loop handling tool use transparently.

        Args:
            messages: Structured message list to start from.
            mcp_tools: Tool definitions from active MCP servers (Anthropic format).
            mcp_manager: Active MCP session manager for tool dispatch.
            stream_callback: Optional chunk callback for streaming-capable channels.

        Returns:
            Accumulated text reply across all turns.
        """
        all_tools = self._get_anthropic_tools(mcp_tools)
        full_reply = ""

        for turn in range(self._profile.max_turns):
            kwargs: dict = dict(
                model=self._profile.model,
                max_tokens=self._profile.max_tokens,
                system=self._soul_prompt,
                messages=messages,
            )
            if all_tools:
                kwargs["tools"] = all_tools

            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    full_reply += text
                    if stream_callback is not None:
                        await stream_callback(text)
                final_msg = await stream.get_final_message()

            if final_msg.stop_reason == "max_tokens":
                logger.warning(
                    "Model hit max_tokens limit (%d) — reply may be truncated",
                    self._profile.max_tokens,
                )
                break
            elif final_msg.stop_reason != "tool_use":
                break

            # Build assistant turn from all content blocks
            assistant_content = []
            for block in final_msg.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute each tool call and collect results
            tool_results = []
            for block in final_msg.content:
                if block.type != "tool_use":
                    continue
                result_text = await self._call_tool(block.name, block.input, mcp_manager)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})

            logger.debug(
                "Agentic turn %d/%d: %d tool calls executed",
                turn + 1,
                self._profile.max_turns,
                len(tool_results),
            )

        return full_reply

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _call_tool(
        self,
        tool_name: str,
        tool_input: dict,
        mcp_manager: McpSessionManager,
    ) -> str:
        """Dispatch a tool call to an internal handler or the MCP manager.

        Args:
            tool_name: Tool name as returned by the model. MCP tools use the
                convention ``{server_name}__{tool_name}``.
            tool_input: Argument dict supplied by the model.
            mcp_manager: Active MCP session manager for external tool dispatch.

        Returns:
            String result to inject as tool_result content.
        """
        if tool_name in self._internal_tools:
            result = self._internal_tools[tool_name].handler(**tool_input)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        return await mcp_manager.call_tool(tool_name, tool_input)

    # ------------------------------------------------------------------
    # Tool schema helpers
    # ------------------------------------------------------------------

    def _get_anthropic_tools(self, mcp_tools: list[dict]) -> list[dict]:
        """Merge internal and MCP tools into the Anthropic API format.

        Internal tool schemas are pre-computed at construction time.
        MCP tools are capped at ``profile.mcp_max_tools``.

        Args:
            mcp_tools: Tool defs already in Anthropic format from MCP servers.

        Returns:
            Combined list: all internal tools + up to mcp_max_tools MCP tools.
        """
        return self._internal_tool_schemas + mcp_tools[: self._profile.mcp_max_tools]

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        envelope: Envelope,
        context: list[dict],
    ) -> list[dict]:
        """Build the structured message list for the Anthropic API.

        Filters context to valid roles, ensures the list does not start with
        an "assistant" turn (Anthropic API requirement), and appends the new
        user message from the envelope.

        Args:
            envelope: Current task envelope (its content is the new user message).
            context: Prior conversation turns from Souvenir.

        Returns:
            List of ``{role, content}`` dicts ready for ``messages.stream()``.
        """
        messages: list[dict] = []

        for turn in context:
            role = turn.get("role", "")
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": turn.get("content", "")})

        # The Anthropic API requires conversations to start with a user turn.
        # When context begins with an assistant message (e.g. an injected
        # greeting), we prepend a synthetic empty user message. The empty
        # content string is intentional — the model treats it as a no-op and
        # uses the following assistant turn as-is. Do NOT replace with a
        # placeholder like "[start]" as that may affect the model's tone.
        if messages and messages[0]["role"] == "assistant":
            messages.insert(0, {"role": "user", "content": ""})

        messages.append({"role": "user", "content": envelope.content})
        return messages
