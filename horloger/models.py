"""SQLModel ORM models for the Horloger brick."""

from sqlmodel import Field, SQLModel


class HorlogerExecution(SQLModel, table=True):
    """One execution record produced when a scheduled job fires.

    Each row represents a single trigger attempt, whether successful or failed.
    Multiple rows may share the same ``job_id`` (one per firing).

    Attributes:
        id: Auto-incremented primary key.
        correlation_id: UUID correlating this execution to a RELAIS pipeline run.
            Indexed for fast lookup from Atelier/Souvenir.
        job_id: Identifier of the scheduled job that produced this execution.
            Indexed for all per-job queries.
        owner_id: Stable user identifier of the job owner (e.g. ``"usr_admin"``).
            Indexed to support per-user history queries.
        channel: Target channel the triggered message is sent on (e.g. ``"discord"``).
        prompt: The prompt text that was (or would have been) published.
        scheduled_for: Epoch seconds of when the job was planned to fire.
        triggered_at: Epoch seconds of when the job actually fired.
        status: Outcome of the trigger attempt.  One of:
            ``"triggered"`` — message published successfully;
            ``"publish_failed"`` — Redis publish raised an error;
            ``"skipped_disabled"`` — job was disabled at fire time;
            ``"skipped_catchup"`` — catch-up firing skipped by policy.
        error: Optional human-readable error detail when ``status`` indicates
            failure.  ``None`` on success.
    """

    __tablename__ = "horloger_executions"

    id: int | None = Field(default=None, primary_key=True)
    correlation_id: str = Field(index=True)
    job_id: str = Field(index=True)
    owner_id: str = Field(index=True)
    channel: str
    prompt: str
    scheduled_for: float
    triggered_at: float
    status: str
    error: str | None = None
