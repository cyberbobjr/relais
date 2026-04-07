"""Tests verifying that Forgeron emits INFO logs at key processing steps.

These tests are written BEFORE the implementation (TDD RED phase).
Expected behaviour:
- INFO log on trace reception (correlation_id visible)
- INFO log before ChangelogWriter.observe() ("analysis trigger")
- INFO log after consolidation decision (triggered or skipped)
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
    tool_call_count: int = 3,
    tool_error_count: int = 2,
    messages_raw: list | None = None,
    correlation_id: str = "corr-test-123",
) -> Envelope:
    env = Envelope(
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
    return env


def _make_forgeron():
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
    # Bypass BrickBase.__init__ side effects; set internal attrs BrickBase expects.
    forgeron._brick_name = "forgeron"
    forgeron._brick_logger = None  # triggers fallback BrickLogger → writes to Python logging (caplog)
    forgeron._config = ForgeonConfig(
        annotation_mode=True,
        annotation_call_threshold=10,
    )
    forgeron._annotation_profile = profile
    forgeron._consolidation_profile = profile
    forgeron._skill_call_counts = {}
    forgeron._trace_store = AsyncMock()
    forgeron._trace_store.add_trace = AsyncMock()

    return forgeron


# ---------------------------------------------------------------------------
# Test 1 — INFO log on trace reception
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_on_trace_reception(caplog) -> None:
    """An INFO log with the correlation_id is emitted when a trace arrives."""
    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(correlation_id="corr-abc-999")
    redis_conn = AsyncMock()

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = AsyncMock()
        mock_writer.observe = AsyncMock(return_value=False)
        MockWriter.return_value = mock_writer

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    reception_logs = [r for r in caplog.records if "corr-abc-999" in r.message]
    assert len(reception_logs) >= 1, (
        f"Expected at least one INFO log containing the correlation_id 'corr-abc-999', "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — INFO log before analysis is triggered
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_before_analysis_trigger(caplog) -> None:
    """An INFO log is emitted before ChangelogWriter.observe() is called."""
    forgeron = _make_forgeron()
    # tool_error_count > 0 → triggers analysis
    envelope = _make_trace_envelope(tool_error_count=2, skill_name="mail-agent")
    redis_conn = AsyncMock()

    observed_after_info: list[bool] = []

    async def spy_observe(*args, **kwargs):
        # Check that an INFO log was emitted before this call
        analysis_logs = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "mail-agent" in r.message
        ]
        observed_after_info.append(len(analysis_logs) > 0)
        return True

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = MagicMock()
        mock_writer.observe = spy_observe
        mock_writer.changelog_path = MagicMock(return_value=None)
        mock_writer.should_consolidate = AsyncMock(return_value=False)
        MockWriter.return_value = mock_writer

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    assert observed_after_info, "spy_observe was never called"
    assert observed_after_info[0], (
        "Expected an INFO log for 'mail-agent' before ChangelogWriter.observe() was called"
    )


# ---------------------------------------------------------------------------
# Test 3 — INFO log after consolidation is triggered
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_when_consolidation_triggered(caplog) -> None:
    """An INFO log is emitted when consolidation is triggered for a skill."""
    from pathlib import Path

    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(tool_error_count=2, skill_name="mail-agent")
    redis_conn = AsyncMock()

    with patch("forgeron.main.ChangelogWriter") as MockWriter:
        mock_writer = MagicMock()
        mock_writer.observe = AsyncMock(return_value=True)
        mock_writer.changelog_path = MagicMock(return_value=Path("/fake/CHANGELOG.md"))
        mock_writer.should_consolidate = AsyncMock(return_value=True)
        MockWriter.return_value = mock_writer

        with patch.object(forgeron, "_maybe_consolidate", new_callable=AsyncMock) as mock_consolidate:
            with caplog.at_level(logging.INFO, logger="forgeron"):
                await forgeron._handle_trace(envelope, redis_conn)

    consolidation_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "consolidat" in r.message.lower()
    ]
    assert len(consolidation_logs) >= 1, (
        f"Expected at least one INFO log about consolidation, "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — INFO log when analysis is skipped (no errors)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_skipped_when_no_errors(caplog) -> None:
    """An INFO log is emitted even when analysis is skipped due to no errors.

    The first reception INFO log must still appear so operators can confirm
    the trace was received.
    """
    forgeron = _make_forgeron()
    # No errors, below threshold → analysis NOT triggered
    forgeron._config.annotation_mode = True
    forgeron._config.annotation_call_threshold = 100
    envelope = _make_trace_envelope(tool_error_count=0, tool_call_count=1)
    redis_conn = AsyncMock()

    with caplog.at_level(logging.INFO, logger="forgeron"):
        await forgeron._handle_trace(envelope, redis_conn)

    info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_logs) >= 1, (
        f"Expected at least one INFO log for trace reception, "
        f"got: {[r.message for r in caplog.records]}"
    )
