"""Serialization/deserialization helpers for LangChain message objects.

Provides a stable, JSON-serializable wire format for the complete message
list captured from a DeepAgents agentic turn.  Used by AgentExecutor to
produce ``messages_raw`` and by Souvenir to restore LangChain message objects
from the short/long-term store.

Supported message types:
- HumanMessage  → role='human'
- AIMessage     → role='ai'  (with optional tool_calls list)
- SystemMessage → role='system'
- ToolMessage   → role='tool' (with tool_call_id and name)
"""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


def serialize_messages(messages: list[BaseMessage]) -> list[dict]:
    """Convert a list of LangChain messages to JSON-serializable dicts.

    Each dict contains at minimum a ``role`` and ``content`` key.
    AIMessage with tool_calls also includes a ``tool_calls`` key.
    ToolMessage also includes ``tool_call_id`` and ``name`` keys.

    Args:
        messages: List of LangChain ``BaseMessage`` subclass instances.

    Returns:
        List of dicts suitable for ``json.dumps()`` and later round-trip via
        ``deserialize_messages()``.
    """
    result: list[dict] = []
    for msg in messages:
        content = msg.content

        if isinstance(msg, HumanMessage):
            result.append({"role": "human", "content": content})

        elif isinstance(msg, (AIMessage, AIMessageChunk)):
            entry: dict = {"role": "ai", "content": content}
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = list(tool_calls)
            result.append(entry)

        elif isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": content})

        elif isinstance(msg, ToolMessage):
            result.append({
                "role": "tool",
                "content": content,
                "tool_call_id": getattr(msg, "tool_call_id", ""),
                "name": getattr(msg, "name", ""),
            })

        else:
            # Fallback: store with the raw type name so it is not silently lost
            result.append({"role": type(msg).__name__.lower(), "content": content})

    return result


def deserialize_messages(data: list[dict]) -> list[BaseMessage]:
    """Convert a list of role/content dicts back to LangChain message objects.

    Reconstructs the exact LangChain type from the ``role`` field:
    - ``'human'``  → HumanMessage
    - ``'ai'``     → AIMessage (with ``tool_calls`` when present)
    - ``'system'`` → SystemMessage
    - ``'tool'``   → ToolMessage

    Args:
        data: List of dicts as produced by ``serialize_messages()``.

    Returns:
        List of LangChain ``BaseMessage`` subclass instances.

    Raises:
        ValueError: If a dict contains an unknown ``role`` value that cannot
            be mapped to a LangChain message type.
    """
    result: list[BaseMessage] = []
    for entry in data:
        role = entry.get("role", "")
        content = entry.get("content", "")

        if role == "human":
            result.append(HumanMessage(content=content))

        elif role == "ai":
            tool_calls = entry.get("tool_calls", [])
            for tc in tool_calls:
                missing = [k for k in ("name", "args") if k not in tc]
                if missing:
                    raise ValueError(
                        f"Malformed tool_call dict — missing required keys {missing}. "
                        f"Got: {tc!r}"
                    )
            result.append(AIMessage(content=content, tool_calls=tool_calls))

        elif role == "system":
            result.append(SystemMessage(content=content))

        elif role == "tool":
            result.append(
                ToolMessage(
                    content=content,
                    tool_call_id=entry.get("tool_call_id", ""),
                    name=entry.get("name", ""),
                )
            )

        else:
            raise ValueError(
                f"Cannot deserialize message with unknown role '{role}'. "
                "Expected one of: 'human', 'ai', 'system', 'tool'."
            )

    return result
