"""HORLOGER envelope builder — constructs trigger envelopes for scheduled jobs.

When a CRON job fires, this module builds the :class:`~common.envelope.Envelope`
that is published to ``STREAM_INCOMING_HORLOGER``.  The envelope impersonates
the job owner so that Portail and Sentinelle apply the correct ACL, and
pre-stamps ``context["portail"]`` so Portail can skip the UserRegistry channel
resolution step (``"horloger"`` is not a real channel in ``portail.yaml``).
"""

from __future__ import annotations

import time
import uuid

from common.config_loader import get_default_llm_profile
from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import ACTION_HORLOGER_TRIGGER
from horloger.job_model import JobSpec


def build_trigger_envelope(job: JobSpec, scheduled_for: float) -> Envelope:
    """Build an Envelope for a HORLOGER job trigger.

    Creates a new incoming envelope that impersonates the job owner.
    Pre-stamps ``context["portail"]`` so Portail can skip UserRegistry lookup.
    Pre-stamps ``context["aiguilleur"]`` with channel metadata so the downstream
    pipeline knows where to send the reply.

    The ``session_id`` is stable per job (``f"horloger-{job.id}"``), which
    preserves conversation context across repeated runs of the same job.
    The ``correlation_id`` is a fresh UUID on every call so each individual
    firing is independently traceable.

    Args:
        job: The :class:`~horloger.job_model.JobSpec` for the job being triggered.
        scheduled_for: The epoch timestamp when the job was scheduled to fire.
            Stored for audit purposes; not used to set ``envelope.timestamp``
            (which always reflects the actual wall-clock time of construction).

    Returns:
        A fully constructed :class:`~common.envelope.Envelope` ready to publish
        to :data:`~common.streams.STREAM_INCOMING_HORLOGER`.
    """
    envelope = Envelope(
        content=job.prompt,
        sender_id=f"horloger:{job.owner_id}",
        channel="horloger",
        session_id=f"horloger-{job.id}",
        correlation_id=str(uuid.uuid4()),
        timestamp=time.time(),
        action=ACTION_HORLOGER_TRIGGER,
    )

    # Pre-stamp portail context so Portail skips UserRegistry lookup.
    portail_ctx = ensure_ctx(envelope, CTX_PORTAIL)
    portail_ctx["user_id"] = job.owner_id
    portail_ctx["llm_profile"] = get_default_llm_profile()

    # Pre-stamp aiguilleur context so the pipeline knows the reply destination.
    aig_ctx = ensure_ctx(envelope, CTX_AIGUILLEUR)
    aig_ctx["channel_profile"] = "default"
    aig_ctx["streaming"] = False
    aig_ctx["reply_to"] = job.channel

    return envelope
