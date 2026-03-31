"""SDK-based executor for the Atelier brick.

Uses the ``anthropic`` Python SDK (AsyncAnthropic) with an explicit tool-use
agentic loop. MCP stdio servers are started via the ``mcp`` Python SDK.
Compatible with LiteLLM proxy via ANTHROPIC_BASE_URL.

**Why messages.create() instead of messages.stream():**
The agentic loop uses ``client.messages.create()`` (non-streaming) for ALL
turns. When streaming through LiteLLM proxy, ``input_json_delta`` SSE events
are silently dropped, resulting in ``block.input == {}`` for ``tool_use``
blocks — the model calls ``read_skill`` but the ``skill_name`` argument never
arrives. ``create()`` returns the fully-assembled JSON object, making
``block.input`` reliable. The ``stream_callback`` is still supported: each
``text`` block in the response is forwarded to the callback immediately after
the API call returns, which is sufficient for Discord/Telegram rendering.

Error contract (critical for at-least-once delivery):
- ``anthropic.APIStatusError``  → non-retriable, wrapped in ``SDKExecutionError``
  → caller ACKs and routes to DLQ.
- ``anthropic.APIConnectionError`` → transient, propagates unwrapped
  → caller does NOT ACK, message stays in PEL for re-delivery.
- Any other exception → propagates unwrapped (same PEL re-delivery behaviour).
"""

from __future__ import annotations

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

    Attributes:
        response_body: Raw response body from the API (truncated to 4000 chars),
            useful for DLQ diagnostics.
    """

    def __init__(self, message: str, response_body: str | None = None) -> None:
        super().__init__(message)
        self.response_body: str | None = response_body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_api_error(exc: anthropic.APIStatusError) -> str:
    """Classify the origin of an APIStatusError as litellm_proxy, upstream_model, or unknown.

    Args:
        exc: The APIStatusError to inspect.

    Returns:
        One of ``"litellm_proxy"``, ``"upstream_model"``, or ``"unknown"``.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return "unknown"
    error = body.get("error", {})
    if not isinstance(error, dict):
        return "unknown"
    msg = str(error.get("message", "")).lower()
    err_type = str(error.get("type", "")).lower()
    if "litellm" in msg or err_type in ("none", ""):
        return "litellm_proxy"
    # Anthropic upstream signals: overloaded_error, api_error; OpenAI: server_error
    if err_type in ("overloaded_error", "api_error", "server_error"):
        return "upstream_model"
    return "unknown"


def _extract_response_body(exc: anthropic.APIStatusError, max_chars: int = 2000) -> str:
    """Extract a human-readable, truncated body string from an APIStatusError.

    Args:
        exc: The exception to inspect.
        max_chars: Maximum length of the returned string.

    Returns:
        Body text, truncated to ``max_chars``.
    """
    body = getattr(exc, "body", None)
    if body is not None:
        text = str(body)
    else:
        response = getattr(exc, "response", None)
        text = getattr(response, "text", "") or ""
    return text[:max_chars]


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
        self._tool_schemas: list[dict] = [
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
                block as it arrives from the model (per-block, not per-token).

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
                all_tools = self._get_anthropic_tools(mcp_tools)
                return await self._run_agentic_loop(
                    messages, all_tools, mcp_manager, stream_callback
                )
        except (anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            # Transient errors — log details then propagate unwrapped so Atelier
            # returns False, leaving the message in the PEL for re-delivery (no ACK).
            body_text = _extract_response_body(exc)
            origin = _classify_api_error(exc)
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                retry_after = getattr(response.headers, "get", lambda k, d=None: d)("retry-after")
            logger.error(
                "Transient API error %d (origin=%s, retry_after=%s): %s | body: %s",
                exc.status_code,
                origin,
                retry_after,
                exc.message,
                body_text,
            )
            raise
        except anthropic.APIStatusError as exc:
            # Non-retriable: bad request, auth error, quota exceeded, etc.
            body_text = _extract_response_body(exc)
            origin = _classify_api_error(exc)
            logger.error(
                "Non-retriable API error %d (origin=%s): %s | body: %s",
                exc.status_code,
                origin,
                exc.message,
                body_text,
            )
            raise SDKExecutionError(
                f"Anthropic API error {exc.status_code}: {exc.message}",
                response_body=_extract_response_body(exc, max_chars=4000),
            ) from exc
        # anthropic.APIConnectionError is intentionally NOT caught here.
        # It propagates to Atelier._handle_message which returns False,
        # leaving the message in the PEL for re-delivery.

    # ------------------------------------------------------------------
    # Tool schema assembly
    # ------------------------------------------------------------------

    def _get_anthropic_tools(self, mcp_tools: list[dict]) -> list[dict]:
        """Merge internal and MCP tool schemas into a single Anthropic-format list.

        Internal tools are never capped. MCP tools are limited to
        ``profile.mcp_max_tools`` entries (0 = no MCP tools exposed).

        Args:
            mcp_tools: Tool definitions collected from MCP servers, already in
                Anthropic format (``{name, description, input_schema}``).

        Returns:
            Merged list ready to pass as ``tools=`` to the Anthropic API.
        """
        return self._tool_schemas + mcp_tools[: self._profile.mcp_max_tools]

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _run_agentic_loop(
        self,
        messages: list[dict],
        all_tools: list[dict],
        mcp_manager: McpSessionManager,
        stream_callback: Callable[[str], Awaitable[None]] | None,
    ) -> str:
        """Multi-turn agentic loop handling tool use transparently.

        Uses ``client.messages.create()`` (non-streaming) for every turn so
        that ``block.input`` in ``tool_use`` blocks is always fully populated.
        Streaming via LiteLLM proxy loses ``input_json_delta`` SSE events,
        causing ``block.input == {}``. See module docstring for full rationale.

        ``stream_callback`` is invoked synchronously (from the caller's
        perspective) once per ``text`` block after each API call returns.
        This is sufficient for real-time Discord/Telegram rendering.

        Args:
            messages: Structured message list to start from.
            all_tools: Merged tool definitions (internal + MCP) in Anthropic
                format, already assembled by the caller.
            mcp_manager: Active MCP session manager for tool dispatch.
            stream_callback: Optional async callable for per-block text output.

        Returns:
            Accumulated text reply across all turns.
        """
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

            _log = logger.info if turn == 0 else logger.debug
            _log(
                "API call turn %d/%d: model=%s messages=%d tools=%d system_len=%d",
                turn + 1,
                self._profile.max_turns,
                self._profile.model,
                len(messages),
                len(all_tools),
                len(self._soul_prompt),
            )

            # Non-streaming call: block.input is fully populated in the response.
            response = await self._client.messages.create(**kwargs)

            # Emit text blocks and accumulate reply.
            for block in response.content:
                if block.type == "text":
                    full_reply += block.text
                    if stream_callback is not None:
                        await stream_callback(block.text)

            if response.stop_reason == "max_tokens":
                logger.warning(
                    "Model hit max_tokens limit (%d) — reply may be truncated",
                    self._profile.max_tokens,
                )
                break
            elif response.stop_reason != "tool_use":
                break

            # Build assistant turn from all content blocks.
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,  # always populated via create()
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute each tool call and collect results.
            tool_results = []
            for block in response.content:
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
            List of ``{role, content}`` dicts ready for ``messages.create()``.
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
