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
    created_at: float = Field(default_factory=time.time)
    # ID of the patch that was active at the time this trace was captured.
    # None = trace captured before any patch was applied.
    patch_id: str | None = Field(default=None, index=True)


class SkillPatch(SQLModel, table=True):
    """A versioned improvement patch applied to a skill file.

    Tracks the full lifecycle of a patch: pending → applied → validated or
    rolled_back.  The original_content field is the rollback source of truth
    (the .bak file is a filesystem-level backup; this is the DB-level backup).
    """

    __tablename__ = "skill_patches"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), primary_key=True
    )
    skill_name: str = Field(index=True)
    original_content: str            # snapshot before patch (rollback source)
    patched_content: str             # improved version proposed by the LLM
    diff: str                        # unified diff string (human-readable)
    rationale: str                   # LLM explanation of the changes
    trigger_correlation_id: str      # correlation_id that triggered this analysis
    created_at: float = Field(default_factory=time.time)
    applied_at: float | None = Field(default=None)
    rolled_back_at: float | None = Field(default=None)
    pre_patch_error_rate: float      # error_rate on the N traces used for analysis
    post_patch_error_rate: float | None = Field(default=None)  # updated by validator
    # pending | applied | rolled_back | validated
    status: str = Field(default="pending")


class SessionSummary(SQLModel, table=True):
    """Une session archivée analysée par Forgeron pour détecter des patterns.

    One row per archived turn analyzed by Forgeron's archive consumer.
    ``intent_label`` is extracted by IntentLabeler (Haiku); None means no
    clear reusable task pattern was detected.
    """

    __tablename__ = "session_summaries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)       # session_id de l'archive
    correlation_id: str = Field(index=True)   # correlation_id du tour
    channel: str                               # canal d'origine ("discord", "telegram", …)
    sender_id: str                             # sender_id d'origine
    intent_label: str | None = Field(default=None, index=True)  # None = pas de pattern clair
    user_content_preview: str = Field(default="")  # premiers 200 chars du message utilisateur
    created_at: float = Field(default_factory=time.time)


class SkillProposal(SQLModel, table=True):
    """Agrégat d'intentions récurrentes en attente ou réalisées de création de skill.

    One row per unique intent_label. ``session_count`` is incremented each time
    a new session with this label is archived. When ``session_count`` reaches
    the threshold, ``status`` transitions from "pending" to "created".
    """

    __tablename__ = "skill_proposals"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    intent_label: str = Field(index=True, unique=True)  # clé de regroupement
    candidate_name: str                                   # nom proposé pour le skill (e.g. "send-email")
    session_count: int = Field(default=1)                # nb de sessions qui ont ce label
    representative_session_ids: str = Field(default="[]")  # JSON list[str] des N session_ids repr.
    draft_content: str | None = Field(default=None)      # SKILL.md généré (None = pas encore créé)
    # pending | created | skipped
    status: str = Field(default="pending")
    created_at: float = Field(default_factory=time.time)
    created_skill_name: str | None = Field(default=None)  # nom du skill finalement créé
