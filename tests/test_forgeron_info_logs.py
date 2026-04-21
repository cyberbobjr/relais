"""Tests verifying that Forgeron emits INFO logs at key processing steps.

Expected behaviour:
- INFO log on trace reception (correlation_id visible)
- INFO log before SkillEditor.edit() is called ("edit TRIGGERED")
- INFO log after edit result ("edit result")
- INFO log when edit is skipped ("edit NOT triggered")
- INFO log for aborted turns
- INFO log confirming trace stored per skill
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
    forgeron._brick_name = "forgeron"
    forgeron._brick_logger = None
    forgeron._config = ForgeonConfig(
        edit_mode=True,
        edit_call_threshold=10,
    )
    forgeron._edit_profile = profile
    forgeron._skill_call_counts = {}
    forgeron._last_had_errors = {}
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

    with patch("forgeron.main.SkillEditor") as MockEditor:
        mock_editor = AsyncMock()
        mock_editor.edit = AsyncMock(return_value=False)
        MockEditor.return_value = mock_editor

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    reception_logs = [r for r in caplog.records if "corr-abc-999" in r.message]
    assert len(reception_logs) >= 1, (
        f"Expected at least one INFO log containing 'corr-abc-999', "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — INFO log before edit is triggered
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_before_edit_trigger(caplog) -> None:
    """An INFO log is emitted before SkillEditor.edit() is called."""
    forgeron = _make_forgeron()
    # tool_error_count > 0 → triggers edit
    envelope = _make_trace_envelope(tool_error_count=2, skill_name="mail-agent")
    redis_conn = AsyncMock()

    observed_after_info: list[bool] = []

    async def spy_edit(*args, **kwargs):
        analysis_logs = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "mail-agent" in r.message
        ]
        observed_after_info.append(len(analysis_logs) > 0)
        return True

    with patch("forgeron.main.SkillEditor") as MockEditor:
        mock_editor = MagicMock()
        mock_editor.edit = spy_edit
        MockEditor.return_value = mock_editor

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    assert observed_after_info, "spy_edit was never called"
    assert observed_after_info[0], (
        "Expected an INFO log for 'mail-agent' before SkillEditor.edit() was called"
    )


# ---------------------------------------------------------------------------
# Test 3 — INFO log after edit result
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_after_edit_result(caplog) -> None:
    """An INFO log mentioning the edit result is emitted after SkillEditor.edit()."""
    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(tool_error_count=2, skill_name="mail-agent")
    redis_conn = AsyncMock()

    with patch("forgeron.main.SkillEditor") as MockEditor:
        mock_editor = AsyncMock()
        mock_editor.edit = AsyncMock(return_value=True)
        MockEditor.return_value = mock_editor

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    result_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "edit result" in r.message.lower()
    ]
    assert len(result_logs) >= 1, (
        f"Expected at least one INFO log about edit result, "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — INFO log when edit is skipped (no errors)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_skipped_when_no_errors(caplog) -> None:
    """An INFO log is emitted even when edit is skipped due to no errors.

    The reception INFO log must still appear so operators can confirm
    the trace was received.
    """
    forgeron = _make_forgeron()
    forgeron._config.edit_mode = True
    forgeron._config.edit_call_threshold = 100
    envelope = _make_trace_envelope(tool_error_count=0, tool_call_count=1)
    redis_conn = AsyncMock()

    with caplog.at_level(logging.INFO, logger="forgeron"):
        await forgeron._handle_trace(envelope, redis_conn)

    info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_logs) >= 1, (
        f"Expected at least one INFO log for trace reception, "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 5 — INFO log when aborted turn is detected (tool_error_count == -1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_info_on_aborted_turn(caplog) -> None:
    """An INFO log mentioning 'aborted' is emitted for tool_error_count == -1 traces."""
    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(
        tool_error_count=-1,
        skill_name="mail-agent",
        correlation_id="corr-aborted-001",
    )
    redis_conn = AsyncMock()

    with patch("forgeron.main.SkillEditor") as MockEditor:
        mock_editor = AsyncMock()
        mock_editor.edit = AsyncMock(return_value=False)
        MockEditor.return_value = mock_editor

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    aborted_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "aborted" in r.message.lower()
    ]
    assert len(aborted_logs) >= 1, (
        f"Expected at least one INFO log mentioning 'aborted', "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 6 — INFO log includes trace-stored confirmation per skill
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_logs_trace_stored_per_skill(caplog) -> None:
    """An INFO log confirming trace storage is emitted for each skill in the trace."""
    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(
        skill_name="search-web",
        tool_call_count=4,
        tool_error_count=0,
        correlation_id="corr-stored-001",
    )
    redis_conn = AsyncMock()

    with caplog.at_level(logging.INFO, logger="forgeron"):
        await forgeron._handle_trace(envelope, redis_conn)

    stored_logs = [
        r for r in caplog.records
        if r.levelno == logging.INFO
        and "search-web" in r.message
        and ("stored" in r.message.lower() or "trace" in r.message.lower())
    ]
    assert len(stored_logs) >= 1, (
        f"Expected INFO log confirming trace stored for 'search-web', "
        f"got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 7 — File-level logger emits on trace reception
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgeron_file_logger_emits_on_trace(caplog) -> None:
    """The module-level 'forgeron' logger must emit an INFO record on trace reception."""
    forgeron = _make_forgeron()
    envelope = _make_trace_envelope(correlation_id="corr-file-log-001")
    redis_conn = AsyncMock()

    with patch("forgeron.main.SkillEditor") as MockEditor:
        mock_editor = AsyncMock()
        mock_editor.edit = AsyncMock(return_value=False)
        MockEditor.return_value = mock_editor

        with caplog.at_level(logging.INFO, logger="forgeron"):
            await forgeron._handle_trace(envelope, redis_conn)

    forgeron_records = [
        r for r in caplog.records
        if r.name == "forgeron" and r.levelno == logging.INFO
    ]
    assert len(forgeron_records) >= 1, (
        f"Expected at least one INFO from the 'forgeron' logger, "
        f"got records from: {set(r.name for r in caplog.records)}"
    )
