"""Tests for Forgeron 'success after failure' analysis trigger.

When a skill turn succeeds (tool_error_count == 0) but the PREVIOUS turn
for the same skill had errors, Forgeron should trigger analysis on the
successful turn — that's where the correction/fix is captured.

TDD RED phase — tests written before implementation.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from common.envelope import Envelope
from common.contexts import CTX_SKILL_TRACE
from common.envelope_actions import ACTION_SKILL_TRACE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace_envelope(
    *,
    skill_name: str = "mail-agent",
    tool_call_count: int = 5,
    tool_error_count: int = 0,
    messages_raw: list | None = None,
    correlation_id: str = "corr-test",
) -> Envelope:
    """Build a trace envelope for testing."""
    return Envelope(
        content="",
        sender_id="atelier:1",
        channel="discord",
        session_id="sess-1",
        correlation_id=correlation_id,
        timestamp=0.0,
        action=ACTION_SKILL_TRACE,
        traces=[],
        context={
            CTX_SKILL_TRACE: {
                "skill_names": [skill_name],
                "tool_call_count": tool_call_count,
                "tool_error_count": tool_error_count,
                "messages_raw": messages_raw or [],
            }
        },
        media_refs=[],
    )


def _make_forgeron(annotation_call_threshold: int = 10):
    """Return a Forgeron instance with mocked config and stores."""
    from forgeron.main import Forgeron
    from common.profile_loader import ProfileConfig, ResilienceConfig
    from forgeron.config import ForgeonConfig

    profile = ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )

    forgeron = Forgeron.__new__(Forgeron)
    forgeron._brick_name = "forgeron"
    forgeron._brick_logger = None
    forgeron._config = ForgeonConfig(
        annotation_mode=True,
        annotation_call_threshold=annotation_call_threshold,
    )
    forgeron._annotation_profile = profile
    forgeron._consolidation_profile = profile
    forgeron._skill_call_counts = {}
    forgeron._last_had_errors = {}
    forgeron._trace_store = AsyncMock()
    forgeron._trace_store.add_trace = AsyncMock()

    return forgeron


# ---------------------------------------------------------------------------
# Test 1 — Success after failure triggers analysis
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_after_failure_triggers_analysis(caplog) -> None:
    """A turn with 0 errors must trigger analysis if the previous turn
    for the same skill had errors.

    This captures the 'correction turn' — the one where the agent found
    the right syntax after failing on previous attempts.
    """
    forgeron = _make_forgeron()
    redis_conn = AsyncMock()

    # Turn 1: mail-agent with errors
    env_error = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=2,
        correlation_id="corr-error",
    )

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=None)
        MockWriter.return_value = mock_writer

        await forgeron._handle_trace(env_error, redis_conn)

    # Turn 2: mail-agent with 0 errors (success after failure)
    env_success = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=0,
        tool_call_count=5,
        correlation_id="corr-success",
    )

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=None)
        MockWriter.return_value = mock_writer

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(env_success, redis_conn)

        # ChangelogWriter.observe MUST be called for the success turn
        mock_writer.observe.assert_called_once()

    # Log must mention "success after failure"
    saf_logs = [
        r for r in caplog.records
        if "success" in r.message.lower() and "fail" in r.message.lower()
    ]
    assert len(saf_logs) >= 1, (
        f"Expected log mentioning 'success after failure', got: "
        f"{[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Success WITHOUT prior failure does NOT trigger
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_without_prior_failure_no_trigger() -> None:
    """A turn with 0 errors and no prior failure must NOT trigger analysis
    (unless the call threshold is reached)."""
    forgeron = _make_forgeron(annotation_call_threshold=100)
    redis_conn = AsyncMock()

    env = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=0,
        correlation_id="corr-normal",
    )

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=False)
        MockWriter.return_value = mock_writer

        await forgeron._handle_trace(env, redis_conn)

        # ChangelogWriter.observe must NOT be called
        mock_writer.observe.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — "success after failure" flag resets after being consumed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_after_failure_flag_resets() -> None:
    """After a 'success after failure' analysis, the flag must reset so that
    the NEXT success turn does NOT trigger again."""
    forgeron = _make_forgeron(annotation_call_threshold=100)
    redis_conn = AsyncMock()

    # Turn 1: error
    env_error = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=1,
        correlation_id="corr-err",
    )
    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=None)
        MockWriter.return_value = mock_writer
        await forgeron._handle_trace(env_error, redis_conn)

    # Turn 2: success (triggers analysis)
    env_success1 = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=0,
        correlation_id="corr-suc1",
    )
    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=None)
        MockWriter.return_value = mock_writer
        await forgeron._handle_trace(env_success1, redis_conn)
        mock_writer.observe.assert_called_once()

    # Turn 3: success again (must NOT trigger — flag was consumed)
    env_success2 = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=0,
        correlation_id="corr-suc2",
    )
    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=False)
        MockWriter.return_value = mock_writer
        await forgeron._handle_trace(env_success2, redis_conn)
        mock_writer.observe.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — Different skills have independent flags
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_after_failure_per_skill_isolation() -> None:
    """The 'last had errors' flag is tracked per skill — an error on
    mail-agent must not trigger analysis on search-web's success turn."""
    forgeron = _make_forgeron(annotation_call_threshold=100)
    redis_conn = AsyncMock()

    # mail-agent error
    env_mail_err = _make_trace_envelope(
        skill_name="mail-agent",
        tool_error_count=2,
        correlation_id="corr-mail-err",
    )
    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=None)
        MockWriter.return_value = mock_writer
        await forgeron._handle_trace(env_mail_err, redis_conn)

    # search-web success (no prior error for search-web)
    env_search_ok = _make_trace_envelope(
        skill_name="search-web",
        tool_error_count=0,
        correlation_id="corr-search-ok",
    )
    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=False)
        MockWriter.return_value = mock_writer
        await forgeron._handle_trace(env_search_ok, redis_conn)
        mock_writer.observe.assert_not_called()
