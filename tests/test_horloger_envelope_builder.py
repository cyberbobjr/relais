"""Tests for horloger/envelope_builder.py — HORLOGER trigger envelope construction.

Tests are written FIRST (TDD red phase) before the implementation exists.

All tests are unit-level: no Redis, no I/O, no external dependencies.
"""

from __future__ import annotations

import time

import pytest

from horloger.job_model import JobSpec


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_job() -> JobSpec:
    """Return a minimal but fully-valid JobSpec for testing."""
    return JobSpec(
        id="weather-morning",
        owner_id="usr_alice",
        schedule="0 8 * * *",
        channel="discord",
        prompt="Donne-moi la météo",
        enabled=True,
        created_at="2026-04-20T08:00:00Z",
        description="test job",
        timezone="Europe/Paris",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(job: JobSpec):
    """Import and call build_trigger_envelope with a fixed scheduled_for."""
    from horloger.envelope_builder import build_trigger_envelope  # noqa: PLC0415

    scheduled_for = time.time()
    return build_trigger_envelope(job, scheduled_for)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_trigger_envelope_content(sample_job: JobSpec) -> None:
    """envelope.content must equal job.prompt."""
    envelope = _build(sample_job)
    assert envelope.content == sample_job.prompt


@pytest.mark.unit
def test_build_trigger_envelope_sender_id(sample_job: JobSpec) -> None:
    """sender_id must follow the horloger:{owner_id} pattern."""
    envelope = _build(sample_job)
    assert envelope.sender_id == "horloger:usr_alice"


@pytest.mark.unit
def test_build_trigger_envelope_channel(sample_job: JobSpec) -> None:
    """channel must be 'horloger' (virtual channel)."""
    envelope = _build(sample_job)
    assert envelope.channel == "horloger"


@pytest.mark.unit
def test_build_trigger_envelope_action(sample_job: JobSpec) -> None:
    """action must be the canonical ACTION_HORLOGER_TRIGGER constant value."""
    from common.envelope_actions import ACTION_HORLOGER_TRIGGER  # noqa: PLC0415

    envelope = _build(sample_job)
    assert envelope.action == ACTION_HORLOGER_TRIGGER
    assert envelope.action == "horloger.trigger"


@pytest.mark.unit
def test_build_trigger_envelope_session_id_stable(sample_job: JobSpec) -> None:
    """Calling twice with the same job must produce the same session_id."""
    from horloger.envelope_builder import build_trigger_envelope  # noqa: PLC0415

    now = time.time()
    env1 = build_trigger_envelope(sample_job, now)
    env2 = build_trigger_envelope(sample_job, now + 10)
    assert env1.session_id == env2.session_id
    assert env1.session_id == f"horloger-{sample_job.id}"


@pytest.mark.unit
def test_build_trigger_envelope_correlation_id_unique(sample_job: JobSpec) -> None:
    """Each call must produce a distinct correlation_id (UUID)."""
    from horloger.envelope_builder import build_trigger_envelope  # noqa: PLC0415

    now = time.time()
    env1 = build_trigger_envelope(sample_job, now)
    env2 = build_trigger_envelope(sample_job, now)
    assert env1.correlation_id != env2.correlation_id


@pytest.mark.unit
def test_build_trigger_envelope_portail_context(sample_job: JobSpec) -> None:
    """context['portail']['user_id'] must equal job.owner_id."""
    from common.contexts import CTX_PORTAIL  # noqa: PLC0415

    envelope = _build(sample_job)
    portail_ctx = envelope.context.get(CTX_PORTAIL, {})
    assert portail_ctx.get("user_id") == "usr_alice"


@pytest.mark.unit
def test_build_trigger_envelope_portail_llm_profile(sample_job: JobSpec, monkeypatch: pytest.MonkeyPatch) -> None:
    """context['portail']['llm_profile'] must come from get_default_llm_profile()."""
    from common.contexts import CTX_PORTAIL  # noqa: PLC0415
    import horloger.envelope_builder as eb  # noqa: PLC0415

    monkeypatch.setattr(eb, "get_default_llm_profile", lambda: "default")
    envelope = _build(sample_job)
    portail_ctx = envelope.context.get(CTX_PORTAIL, {})
    assert portail_ctx.get("llm_profile") == "default"


@pytest.mark.unit
def test_build_trigger_envelope_aiguilleur_reply_to(sample_job: JobSpec) -> None:
    """context['aiguilleur']['reply_to'] must equal job.channel."""
    from common.contexts import CTX_AIGUILLEUR  # noqa: PLC0415

    envelope = _build(sample_job)
    aig_ctx = envelope.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx.get("reply_to") == "discord"
    assert aig_ctx.get("reply_to") == sample_job.channel


@pytest.mark.unit
def test_build_trigger_envelope_aiguilleur_streaming(sample_job: JobSpec) -> None:
    """context['aiguilleur']['streaming'] must be False (horloger jobs never stream)."""
    from common.contexts import CTX_AIGUILLEUR  # noqa: PLC0415

    envelope = _build(sample_job)
    aig_ctx = envelope.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx.get("streaming") is False


@pytest.mark.unit
def test_build_trigger_envelope_aiguilleur_channel_profile(sample_job: JobSpec) -> None:
    """context['aiguilleur']['channel_profile'] must be 'default'."""
    from common.contexts import CTX_AIGUILLEUR  # noqa: PLC0415

    envelope = _build(sample_job)
    aig_ctx = envelope.context.get(CTX_AIGUILLEUR, {})
    assert aig_ctx.get("channel_profile") == "default"


@pytest.mark.unit
def test_build_trigger_envelope_timestamp_recent(sample_job: JobSpec) -> None:
    """timestamp must be within 2 seconds of the current time."""
    before = time.time()
    envelope = _build(sample_job)
    after = time.time()
    assert before - 2 <= envelope.timestamp <= after + 2


@pytest.mark.unit
def test_build_trigger_envelope_serializable(sample_job: JobSpec) -> None:
    """Envelope must serialize to JSON without raising (action is set)."""
    envelope = _build(sample_job)
    json_str = envelope.to_json()
    assert "horloger.trigger" in json_str


@pytest.mark.unit
def test_build_trigger_envelope_traces_empty(sample_job: JobSpec) -> None:
    """Fresh envelope should have no traces (builder does not add traces)."""
    envelope = _build(sample_job)
    assert envelope.traces == []
