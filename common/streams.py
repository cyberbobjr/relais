"""Canonical stream name constants for the RELAIS Redis bus.

All stream names must be imported from this module — never use string literals
in application code.  This prevents typos and makes renaming streams a
single-file change.

Usage::

    from common.streams import STREAM_TASKS, stream_outgoing

    await redis.xadd(STREAM_TASKS, {"payload": envelope.to_json()})
    await redis.xadd(stream_outgoing(channel), {"payload": reply.to_json()})
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Static stream names
# ---------------------------------------------------------------------------

STREAM_LOGS: str = "relais:logs"
"""Operational log entries from all bricks."""

STREAM_INCOMING: str = "relais:messages:incoming"
"""Messages arriving from external channels (Aiguilleur → Portail)."""

STREAM_SECURITY: str = "relais:security"
"""Enriched envelopes awaiting ACL validation (Portail → Sentinelle)."""

STREAM_TASKS: str = "relais:tasks"
"""Authorized normal messages for LLM processing (Sentinelle → Atelier)."""

STREAM_TASKS_FAILED: str = "relais:tasks:failed"
"""Dead-letter queue for tasks that exhausted all retry attempts."""

STREAM_COMMANDS: str = "relais:commands"
"""Authorized slash commands (Sentinelle → Commandant)."""

STREAM_OUTGOING_PENDING: str = "relais:messages:outgoing_pending"
"""Fully assembled replies awaiting outgoing guardrails (Atelier → Sentinelle)."""

STREAM_MEMORY_REQUEST: str = "relais:memory:request"
"""Memory operation requests (Atelier/Commandant → Souvenir)."""

STREAM_MEMORY_RESPONSE: str = "relais:memory:response"
"""Memory operation responses (Souvenir → Atelier/Commandant)."""

STREAM_EVENTS_SYSTEM: str = "relais:events:system"
"""System-level event notifications."""

STREAM_EVENTS_MESSAGES: str = "relais:events:messages"
"""Message-level event notifications."""

STREAM_ADMIN_PENDING_USERS: str = "relais:admin:pending_users"
"""Unknown-sender identifiers awaiting manual approval."""

STREAM_SKILL_TRACE: str = "relais:skill:trace"
"""Skill execution traces published by Atelier after each turn using skills.
Consumed by Forgeron for statistical analysis and skill improvement."""

STREAM_OUTGOING_FAILED: str = "relais:messages:outgoing:failed"
"""Dead-letter queue for outgoing messages that failed to deliver."""

STREAM_INCOMING_HORLOGER: str = "relais:messages:incoming:horloger"
"""Scheduled job triggers fired by Horloger (CRON scheduler brick)."""

KEY_WHATSAPP_PAIRING: str = "relais:whatsapp:pairing"
"""Redis key storing the active WhatsApp QR pairing context (JSON, TTL 300s)."""

# ---------------------------------------------------------------------------
# Dynamic stream name helpers
# ---------------------------------------------------------------------------


def stream_outgoing(channel: str) -> str:
    """Return the per-channel outgoing stream name.

    Args:
        channel: Channel identifier (e.g. ``"discord"``).

    Returns:
        Stream name such as ``"relais:messages:outgoing:discord"``.
    """
    return f"relais:messages:outgoing:{channel}"


def stream_outgoing_user(channel: str, user_id: str) -> str:
    """Return the per-user outgoing push stream name.

    Used by the REST adapter to mirror replies into a user-scoped stream
    that the SSE push endpoint reads via XREAD BLOCK.

    Args:
        channel: Channel identifier (e.g. ``"rest"``).
        user_id: Stable user identifier (e.g. ``"usr_admin"``).

    Returns:
        Stream name such as ``"relais:messages:outgoing:rest:usr_admin"``.
    """
    return f"relais:messages:outgoing:{channel}:{user_id}"


def stream_streaming(channel: str, corr_id: str) -> str:
    """Return the streaming token stream name for a single request.

    Args:
        channel: Channel identifier.
        corr_id: Correlation ID of the request being streamed.

    Returns:
        Stream name such as
        ``"relais:messages:streaming:discord:some-uuid"``.
    """
    return f"relais:messages:streaming:{channel}:{corr_id}"


def key_active_sessions(sender_id: str) -> str:
    """Return the active-session Redis Hash key for a sender.

    Note: despite the ``stream_*`` naming convention used for other helpers in
    this module, this function returns a Redis **Hash** key, not a Stream name.
    The ``key_`` prefix reflects that distinction.

    Args:
        sender_id: Sender identifier (e.g. ``"discord:admin001"``).

    Returns:
        Key such as ``"relais:active_sessions:discord:admin001"``.
    """
    return f"relais:active_sessions:{sender_id}"


def stream_config_reload(brick: str) -> str:
    """Return the Pub/Sub channel used to trigger a hot-reload for a brick.

    Args:
        brick: Brick name (e.g. ``"portail"``).

    Returns:
        Channel name such as ``"relais:config:reload:portail"``.
    """
    return f"relais:config:reload:{brick}"
