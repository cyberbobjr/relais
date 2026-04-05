import copy
from dataclasses import dataclass, field
import uuid
import time
import json
from typing import Any, Self


@dataclass(frozen=True)
class MediaRef:
    """Represents a reference to a media file in the RELAIS system.

    Attributes:
        media_id: Unique identifier for the media.
        path: Path to the media file.
        mime_type: MIME type of the file.
        size_bytes: Size of the file in bytes.
        expires_in_hours: Expiration time for the media link.
    """
    media_id: str
    path: str
    mime_type: str
    size_bytes: int
    expires_in_hours: int = 24


@dataclass
class Envelope:
    """Standard message envelope for RELAIS inter-brick communication.

    An Envelope is composed of a fixed **header** and a namespaced **context**:

    Header fields (common to all envelopes, always present):
        content:        Main text content (user message, reply, or "" for
                        action-only envelopes such as memory requests).
        sender_id:      Origin identifier in "channel:id" format.
        channel:        Communication channel name (discord, telegram, …).
        session_id:     Stable session identifier.
        correlation_id: UUID tracking the request end-to-end.
        timestamp:      Unix epoch of envelope creation.
        action:         Self-describing action token. Set by each producing
                        brick before publishing. See common/envelope_actions.py
                        for the canonical list. Not used for routing — stream
                        names handle routing. Required; fail-fast if absent.
        traces:         Ordered list of pipeline steps. Each brick appends an
                        entry via add_trace(). Read by Archiviste for audit.
        media_refs:     Attached media files.

    Context (namespaced, free-form):
        context:        dict[str, dict] where each top-level key is a brick
                        name (see common/contexts.py for CTX_* constants).
                        Invariant: a brick MUST only write to context[CTX_SELF].
                        It MAY read from any namespace.
                        Known namespaces and their TypedDicts:
                          "aiguilleur"       → AiguilleurCtx
                          "portail"          → PortailCtx
                          "sentinelle"       → SentinelleCtx
                          "atelier"          → AtelierCtx
                          "souvenir_request" → SouvenirRequest (request payload,
                                               written by caller, read by Souvenir)
    """
    content: str
    sender_id: str
    channel: str
    session_id: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    action: str = ""
    traces: list[dict[str, Any]] = field(default_factory=list)
    media_refs: list[MediaRef] = field(default_factory=list)
    context: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_parent(cls, parent: "Envelope", content: str) -> "Envelope":
        """Creates a derived envelope from a parent, inheriting tracking fields.

        The child does NOT inherit the parent's action — each producing brick
        must set action explicitly before publishing. The context is deep-copied
        so that child mutations never affect the parent.

        Args:
            parent: The parent envelope to copy tracking info from.
            content: The new content for the derived envelope.

        Returns:
            A new Envelope with copied tracking fields and deep-copied context.
        """
        return cls(
            content=content,
            sender_id=parent.sender_id,
            channel=parent.channel,
            session_id=parent.session_id,
            correlation_id=parent.correlation_id,
            traces=copy.deepcopy(parent.traces),
            context=copy.deepcopy(parent.context),
        )

    def add_trace(self, brick: str, step: str) -> None:
        """Appends a pipeline step record to the traces list.

        Args:
            brick: Name of the micro-brick processing the message.
            step: Short description of the operation performed.
        """
        self.traces.append({
            "brick": brick,
            "step": step,
            "timestamp": time.time(),
        })

    def to_dict(self) -> dict[str, Any]:
        """Converts the envelope to a dictionary for serialization.

        Returns:
            Dictionary representation of the envelope.
        """
        return {
            "content": self.content,
            "sender_id": self.sender_id,
            "channel": self.channel,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "traces": self.traces,
            "media_refs": [vars(r) for r in self.media_refs],
            "context": self.context,
        }

    def to_json(self) -> str:
        """Serializes the envelope to a JSON string.

        Returns:
            JSON string representation.
        """
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, data: str) -> "Envelope":
        """Deserializes an envelope from a JSON string.

        Fails fast if the data does not match the current schema. There is no
        legacy fallback — drain Redis streams before deploying this version.

        Args:
            data: JSON string to deserialize.

        Returns:
            A new Envelope instance.

        Raises:
            KeyError: If a required field is absent in the serialized data.
            ValueError: If the JSON cannot be parsed.
        """
        from dataclasses import fields as dc_fields
        payload = json.loads(data)

        media_refs_data = payload.pop("media_refs", [])
        media_refs = [MediaRef(**m) for m in media_refs_data]

        valid_field_names = {f.name for f in dc_fields(cls)}
        envelope_data = {k: v for k, v in payload.items() if k in valid_field_names}

        envelope = cls(**envelope_data)
        object.__setattr__(envelope, "media_refs", media_refs) if False else None
        envelope.media_refs = media_refs
        return envelope

    @classmethod
    def create_response_to(cls, parent: "Envelope", content: str) -> "Envelope":
        """Convenience alias for from_parent.

        Args:
            parent: The original envelope.
            content: The response content.

        Returns:
            A new Envelope instance derived from parent.
        """
        return cls.from_parent(parent, content)


@dataclass(frozen=True)
class PushEnvelope:
    """Envelope for proactive notifications via Le Crieur.

    Attributes:
        content: Message to be pushed.
        urgency: Level of urgency (normal, high, critical).
        target_user_id: ID of the user to notify.
        target_role: Target role for RBAC-based notifications.
        session_id: Associated session if any.
        correlation_id: Unique tracking ID.
        timestamp: Creation time.
        source: Component that triggered the push.
    """
    content: str
    urgency: str = "normal"
    target_user_id: str | None = None
    target_role: str | None = None
    session_id: str | None = None
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source: str = "system"

    def to_dict(self) -> dict[str, Any]:
        """Converts the push envelope to a dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "content": self.content,
            "urgency": self.urgency,
            "target_user_id": self.target_user_id,
            "target_role": self.target_role,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "source": self.source,
        }
