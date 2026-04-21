"""SQLModel ORM models for the Forgeron brick."""

import time
import uuid

from sqlmodel import Field, SQLModel


class SkillTrace(SQLModel, table=True):
    """A single skill execution trace captured by Atelier.

    One row per completed agent turn that used at least one skill and made
    at least one tool call. Used by Forgeron to accumulate evidence before
    triggering an LLM analysis.
    """

    __tablename__ = "skill_traces"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), primary_key=True
    )
    skill_name: str = Field(index=True)
    correlation_id: str = Field(index=True)
    tool_call_count: int
    tool_error_count: int
    messages_raw: str = Field(default="[]")  # JSON blob — full LangChain message list
    skill_path: str | None = Field(default=None)  # absolute skill dir path (set for bundle skills)
    created_at: float = Field(default_factory=time.time)


class SessionSummary(SQLModel, table=True):
    """An archived session analyzed by Forgeron to detect patterns.

    One row per archived turn analyzed by Forgeron's archive consumer.
    ``intent_label`` is extracted by IntentLabeler (Haiku); None means no
    clear reusable task pattern was detected.
    """

    __tablename__ = "session_summaries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)       # session_id of the archive
    correlation_id: str = Field(index=True)   # correlation_id of the turn
    channel: str                               # origin channel ("discord", "telegram", …)
    sender_id: str                             # origin sender_id
    intent_label: str | None = Field(default=None, index=True)  # None = no clear pattern
    user_content_preview: str = Field(default="")  # first 200 chars of the user message
    created_at: float = Field(default_factory=time.time)


class SkillProposal(SQLModel, table=True):
    """Aggregate of recurring intents pending or completed skill creation.

    One row per unique intent_label. ``session_count`` is incremented each time
    a new session with this label is archived. When ``session_count`` reaches
    the threshold, ``status`` transitions from "pending" to "created".
    """

    __tablename__ = "skill_proposals"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    intent_label: str = Field(index=True, unique=True)  # grouping key
    candidate_name: str                                   # proposed skill name (e.g. "send-email")
    session_count: int = Field(default=1)                # number of sessions with this label
    representative_session_ids: str = Field(default="[]")  # JSON list[str] of N representative session_ids
    draft_content: str | None = Field(default=None)      # generated SKILL.md (None = not yet created)
    # pending | created | skipped
    status: str = Field(default="pending")
    created_at: float = Field(default_factory=time.time)
    created_skill_name: str | None = Field(default=None)  # name of the skill finally created
