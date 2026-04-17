"""Centralised command registry for Commandant.

This module is the single source of truth for global out-of-LLM commands.
Adding a command requires only one change here:
  1. Write the async handler (function below)
  2. Add an entry to COMMAND_REGISTRY

KNOWN_COMMANDS and parse_command() are updated automatically.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from common.contexts import CTX_PORTAIL, CTX_SOUVENIR_REQUEST, PortailCtx
from common.envelope import Envelope
from common.envelope_actions import ACTION_MEMORY_CLEAR, ACTION_MEMORY_SESSIONS, ACTION_MEMORY_RESUME, ACTION_MESSAGE_OUTGOING
from common.streams import STREAM_MEMORY_REQUEST, stream_outgoing
from common.text_utils import strip_outer_quotes

logger = logging.getLogger("commandant.commands")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandSpec:
    """Metadata and handler for a global command.

    Attributes:
        name: Command name in lowercase (e.g. "clear").
        description: Short description displayed by /help.
        handler: async(envelope, redis_conn) coroutine executed when the
                 command is detected.
    """
    name: str
    description: str
    handler: Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CommandResult:
    """Result of parsing a global command.

    Attributes:
        command: Command name in lowercase (e.g. "clear").
        args: Additional arguments (currently always empty).
    """
    command: str
    args: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_clear(envelope: Envelope, redis_conn: Any) -> None:
    """Clear session history: Redis context + SQLite messages.

    Sends action="clear" on relais:memory:request so that Souvenir performs
    the cleanup (context_store.clear + long_term_store.clear_session) and
    publishes a confirmation back to the channel.

    Args:
        envelope: The envelope of the received /clear message.
        redis_conn: Active async Redis connection.
    """
    portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore
    user_id = portail_ctx.get("user_id", envelope.sender_id)
    clear_env = Envelope(
        content="",
        sender_id=envelope.sender_id,
        channel=envelope.channel,
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        action=ACTION_MEMORY_CLEAR,
        context={CTX_SOUVENIR_REQUEST: {"session_id": envelope.session_id, "user_id": user_id, "envelope_json": envelope.to_json()}},
    )

    await redis_conn.xadd(
        STREAM_MEMORY_REQUEST,
        {"payload": clear_env.to_json()},
    )
    logger.info("Clear request sent for session=%s", envelope.session_id)


async def handle_sessions(envelope: Envelope, redis_conn: Any) -> None:
    """List recent sessions for the requesting user.

    Sends action="memory.sessions" on relais:memory:request so that Souvenir
    retrieves the session list and publishes it back to the channel.

    Args:
        envelope: The envelope of the received /sessions message.
        redis_conn: Active async Redis connection.
    """
    portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore
    user_id = portail_ctx.get("user_id", envelope.sender_id)
    sessions_env = Envelope(
        content="",
        sender_id=envelope.sender_id,
        channel=envelope.channel,
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        action=ACTION_MEMORY_SESSIONS,
        context={CTX_SOUVENIR_REQUEST: {"user_id": user_id, "envelope_json": envelope.to_json()}},
    )

    await redis_conn.xadd(
        STREAM_MEMORY_REQUEST,
        {"payload": sessions_env.to_json()},
    )
    logger.info("Sessions request sent for user=%s", user_id)


async def handle_resume(envelope: Envelope, redis_conn: Any) -> None:
    """Resume a previous session by session_id.

    Parses the session_id from envelope.content (after "/resume").
    If no session_id is provided, sends a usage error reply to the channel.
    Otherwise sends action="memory.resume" on relais:memory:request.

    Args:
        envelope: The envelope of the received /resume message.
        redis_conn: Active async Redis connection.
    """
    parts = envelope.content.strip().split()
    if len(parts) < 2:
        response = Envelope.from_parent(envelope, "Usage: /resume <session_id>")
        response.action = ACTION_MESSAGE_OUTGOING
        await redis_conn.xadd(
            stream_outgoing(envelope.channel),
            {"payload": response.to_json()},
        )
        return

    session_id = parts[1]
    portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore
    user_id = portail_ctx.get("user_id", envelope.sender_id)
    resume_env = Envelope(
        content="",
        sender_id=envelope.sender_id,
        channel=envelope.channel,
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        action=ACTION_MEMORY_RESUME,
        context={CTX_SOUVENIR_REQUEST: {"target_session_id": session_id, "user_id": user_id, "envelope_json": envelope.to_json()}},
    )

    await redis_conn.xadd(
        STREAM_MEMORY_REQUEST,
        {"payload": resume_env.to_json()},
    )
    logger.info("Resume request sent for session=%s user=%s", session_id, user_id)


async def handle_help(envelope: Envelope, redis_conn: Any) -> None:
    """Return the list of all available commands with their descriptions.

    The list is built dynamically from COMMAND_REGISTRY, ensuring it is always
    up-to-date without any handler modification.

    Args:
        envelope: The envelope of the received /help message.
        redis_conn: Active async Redis connection.
    """
    lines = ["Available commands:"]
    for spec in COMMAND_REGISTRY.values():
        lines.append(f"  /{spec.name} — {spec.description}")
    help_text = "\n".join(lines)

    response = Envelope.from_parent(envelope, help_text)
    response.action = ACTION_MESSAGE_OUTGOING
    await redis_conn.xadd(
        stream_outgoing(envelope.channel),
        {"payload": response.to_json()},
    )


# ---------------------------------------------------------------------------
# Registry — single source of truth
# ---------------------------------------------------------------------------
#
# Channel installation, configuration and pairing (including the WhatsApp
# QR flow that used to live behind `/settings whatsapp`) are now handled
# end-to-end by the ``relais-config`` subagent via the ``channel-setup``
# skill. Users ask the agent in natural language ("install WhatsApp",
# "pair my phone") and the subagent runs the install script, edits
# ``aiguilleur.yaml``, restarts bricks, and invokes
# ``python -m aiguilleur.channels.whatsapp configure --action pair`` for the deterministic pairing step.

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "clear": CommandSpec(
        name="clear",
        description="Clears conversation history (Redis + SQLite).",
        handler=handle_clear,
    ),
    "help": CommandSpec(
        name="help",
        description="Displays the list of available commands.",
        handler=handle_help,
    ),
    "sessions": CommandSpec(
        name="sessions",
        description="Liste vos sessions récentes.",
        handler=handle_sessions,
    ),
    "resume": CommandSpec(
        name="resume",
        description="Reprend une session précédente (/resume <id>).",
        handler=handle_resume,
    ),
}

KNOWN_COMMANDS: frozenset[str] = frozenset(COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_command(text: str) -> CommandResult | None:
    """Parse a text message to detect a global command.

    A valid command:
    - Starts with '/' after strip()
    - May be wrapped in symmetric single or double quotes
    - The name (after '/') must belong to KNOWN_COMMANDS (case-insensitive)

    Args:
        text: Raw message content.

    Returns:
        CommandResult if a known command is detected, None otherwise.
    """
    stripped = strip_outer_quotes(text)
    if not stripped.startswith("/"):
        return None

    parts = stripped[1:].split()
    if not parts:
        return None

    command_name = parts[0].lower()
    if command_name not in KNOWN_COMMANDS:
        return None

    return CommandResult(command=command_name, args=parts[1:])
