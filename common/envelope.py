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

    Attributes:
        content: The main text content of the message.
        sender_id: ID of the user or system component that sent the message.
        channel: Communication channel (e.g., discord, web, api).
        session_id: Unique session identifier.
        correlation_id: ID used for tracing request-chain tracking.
        timestamp: Creation time of the envelope (Unix epoch).
        metadata: Extensible storage for brick-specific data.
        media_refs: List of associated media files.
    """
    content: str
    sender_id: str
    channel: str
    session_id: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    media_refs: list[MediaRef] = field(default_factory=list)

    @classmethod
    def from_parent(cls, parent: "Envelope", content: str) -> "Envelope":
        """Creates a response or derived envelope from a parent one.

        Args:
            parent: The parent envelope to copy tracking info from.
            content: The new content for the derived envelope.

        Returns:
            A new Envelope instance linked to the parent.
        """
        return cls(
            content=content,
            sender_id=parent.sender_id,
            channel=parent.channel,
            session_id=parent.session_id,
            correlation_id=parent.correlation_id,
            metadata=copy.deepcopy(parent.metadata)
        )

    def add_trace(self, brick: str, action: str) -> None:
        """Appends tracking metadata to reconstruct the message route.

        Args:
            brick: Name of the micro-brick processing the message.
            action: Action performed by the brick.
        """
        if "traces" not in self.metadata:
            self.metadata["traces"] = []
        self.metadata["traces"].append({
            "brick": brick,
            "action": action,
            "timestamp": time.time()
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
            "metadata": self.metadata,
            "media_refs": [vars(r) for r in self.media_refs]
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

        Args:
            data: JSON string to deserialize.

        Returns:
            A new Envelope instance.
        """
        from dataclasses import fields
        payload = json.loads(data)
        
        media_refs_data = payload.pop("media_refs", [])
        media_refs = [MediaRef(**m) for m in media_refs_data]
        
        # Filter payload to only include valid Envelope fields
        valid_field_names = {f.name for f in fields(cls)}
        envelope_data = {k: v for k, v in payload.items() if k in valid_field_names}
        
        envelope = cls(**envelope_data)
        envelope.media_refs = media_refs
        return envelope

    @classmethod
    def create_response_to(cls, parent: "Envelope", content: str) -> "Envelope":
        """Convenience method to create a response to a message.

        Args:
            parent: The original envelope.
            content: The response content.

        Returns:
            A new Envelope instance.
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
            "source": self.source
        }
