"""Namespaced context definitions for RELAIS Envelope.context.

Each brick owns exactly one namespace key in envelope.context.
A brick MUST only write to context[CTX_SELF] and MAY read from any namespace.

Usage:
    from common.contexts import CTX_PORTAIL, PortailCtx, ensure_ctx

    # Write (own namespace only)
    ensure_ctx(envelope, CTX_PORTAIL).update({
        "user_id": "usr_admin",
        "llm_profile": "precise",
    })

    # Read (any namespace)
    ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})
    user_id = ctx.get("user_id", envelope.sender_id)

Special case — SouvenirRequest:
    context["souvenir_request"] is a *request payload namespace*, not a brick
    context. It is written by the caller (Atelier or Commandant) and read by
    Souvenir. This is the documented exception to the brick-ownership rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from typing import TypedDict

if TYPE_CHECKING:
    from common.envelope import Envelope

# ---------------------------------------------------------------------------
# Namespace key constants
# ---------------------------------------------------------------------------

CTX_AIGUILLEUR = "aiguilleur"
CTX_PORTAIL = "portail"
CTX_SENTINELLE = "sentinelle"
CTX_ATELIER = "atelier"
CTX_SOUVENIR_REQUEST = "souvenir_request"


# ---------------------------------------------------------------------------
# Per-brick TypedDicts (total=False — all keys are optional)
# ---------------------------------------------------------------------------

class AiguilleurCtx(TypedDict, total=False):
    """Context stamped by Aiguilleur on incoming messages."""
    channel_profile: str        # LLM profile name from aiguilleur.yaml
    channel_prompt_path: str    # Path to channel prompt overlay (Layer 4)
    reply_to: str               # Channel/thread ID for reply routing
    content_type: str           # "text" | "image" | …
    access_context: str         # "dm" | "server" — ACL scope hint
    streaming: bool             # True if this channel supports token-by-token streaming


class PortailCtx(TypedDict, total=False):
    """Context stamped by Portail after user resolution and enrichment."""
    user_id: str        # Stable cross-channel user key (e.g. "usr_admin")
    user_record: dict[str, Any]  # Full UserRecord dict from portail.yaml
    llm_profile: str    # Resolved LLM profile name
    session_start: bool # True if this message opens a new session


class SentinelleCtx(TypedDict, total=False):
    """Context stamped by Sentinelle after ACL check."""
    acl_passed: bool    # True if message cleared ACL
    acl_role: str       # Role used for ACL evaluation
    outgoing_checked: bool  # True on outgoing envelopes after guardrail


class AtelierCtx(TypedDict, total=False):
    """Context stamped by Atelier on response and progress envelopes."""
    streamed: bool      # True if reply was streamed token-by-token
    user_message: str   # Original user content (for Souvenir archival)
    progress_event: str         # Progress event type (e.g. "thinking")
    progress_detail: str        # Human-readable progress description


class SouvenirRequest(TypedDict, total=False):
    """Request payload written by callers (Atelier/Commandant) for Souvenir.

    This is NOT a brick context — it is a typed request payload. Souvenir
    reads it via envelope.context[CTX_SOUVENIR_REQUEST].
    """
    session_id: str
    user_id: str
    envelope_json: str      # Serialized parent envelope (for archive)
    messages_raw: str       # Serialized LangChain message list (JSON)
    path: str               # File path (file_* actions)
    content: str            # File content (file_write action)
    overwrite: bool         # Overwrite flag (file_write action)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def ensure_ctx(envelope: "Envelope", key: str) -> dict[str, Any]:
    """Return the context sub-dict for *key*, creating it if absent.

    This is the canonical write helper. Call it before updating a namespace
    to guarantee the sub-dict exists.

    Args:
        envelope: The envelope whose context to write to.
        key: The namespace key (use a CTX_* constant).

    Returns:
        The mutable sub-dict for the given namespace key.
    """
    if key not in envelope.context:
        envelope.context[key] = {}
    return envelope.context[key]
