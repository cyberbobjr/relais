"""InternalTool — native (non-MCP) tool definition for the Atelier agentic loop.

Internal tools are handled directly by the Python process instead of being
dispatched to an external MCP stdio server. They are merged with MCP tools
before being sent to the Anthropic API; the agentic loop calls the handler
when the model selects one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class InternalTool:
    """Immutable definition of a native tool callable from the agentic loop.

    Attributes:
        name: Tool identifier sent to the Anthropic API (no spaces, use
            underscores). Must be unique across all tools (internal + MCP).
        description: Short description shown to the model to help it decide
            when to call this tool.
        input_schema: JSON Schema dict in the format Anthropic expects:
            ``{"type": "object", "properties": {...}, "required": [...]}``.
        handler: Callable invoked when the model selects this tool. Receives
            the model-supplied arguments as ``**kwargs`` matching the schema
            properties. May be sync or async; the loop awaits it either way.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str | Awaitable[str]]
