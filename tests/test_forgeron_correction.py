"""Tests for Forgeron correction detection and skill-designer routing.

Covers:
- _process_archive(): correction path — triggers _trigger_skill_design()
- _process_archive(): normal path — does NOT trigger skill design
- _trigger_skill_design(): BRPOP timeout path — logs and returns
- _trigger_skill_design(): happy path — publishes task to relais:tasks
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from common.contexts import CTX_FORGERON, CTX_SOUVENIR_REQUEST
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MEMORY_ARCHIVE,
    ACTION_MEMORY_HISTORY_READ,
    ACTION_MESSAGE_TASK,
)
from common.profile_loader import ProfileConfig, ResilienceConfig
from common.streams import STREAM_MEMORY_REQUEST, STREAM_TASKS, STREAM_OUTGOING_PENDING
from common.envelope_actions import ACTION_MESSAGE_OUTGOING_PENDING
from forgeron.intent_labeler import IntentLabelLLMResponse, IntentLabelResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile() -> ProfileConfig:
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _make_archive_envelope(session_id: str = "sess-1") -> Envelope:
    """Build a minimal archive envelope as published by Atelier."""
    inner_env = Envelope(
        content="Hi",
        sender_id="discord:123",
        channel="discord",
        session_id=session_id,
        correlation_id="corr-abc",
    )
    inner_env.action = ACTION_MEMORY_ARCHIVE

    outer = Envelope(
        content="",
        sender_id="discord:123",
        channel="discord",
        session_id=session_id,
        correlation_id="corr-abc",
        action=ACTION_MEMORY_ARCHIVE,
        context={
            CTX_SOUVENIR_REQUEST: {
                "envelope_json": inner_env.to_json(),
                "messages_raw": json.dumps([
                    {"type": "human", "content": "That was wrong, do it differently"}
                ]),
            }
        },
    )
    return outer


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=-2)
    redis.set = AsyncMock()
    redis.xadd = AsyncMock()
    redis.brpop = AsyncMock(return_value=None)
    return redis


# ---------------------------------------------------------------------------
# Tests — _process_archive() normal path (no correction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_archive_no_correction_does_not_trigger_skill_design(
    tmp_path: Path, mock_redis: AsyncMock
) -> None:
    """When is_correction=False, _trigger_skill_design is NOT called."""
    from forgeron.main import Forgeron

    label_result = IntentLabelResult(
        label="send_email",
        is_correction=False,
        corrected_behavior=None,
        skill_name_hint=None,
    )

    with (
        patch("forgeron.main.load_forgeron_config") as mock_cfg,
        patch("forgeron.main.load_profiles", return_value={}),
        patch("forgeron.main.resolve_profile", return_value=_make_profile()),
        patch("forgeron.main.resolve_storage_dir", return_value=tmp_path),
        patch("forgeron.intent_labeler.build_chat_model"),
    ):
        from forgeron.config import ForgeonConfig

        cfg = ForgeonConfig(
            correction_mode=True,
            creation_mode=True,
            skills_dir=tmp_path / "skills",
        )
        mock_cfg.return_value = cfg

        forgeron = Forgeron.__new__(Forgeron)
        forgeron._config = cfg
        forgeron._annotation_profile = _make_profile()
        forgeron._llm_profile = _make_profile()
        forgeron._consolidation_profile = _make_profile()

        from forgeron.session_store import SessionStore

        forgeron._session_store = SessionStore(db_path=tmp_path / "fg.db")
        await forgeron._session_store._create_tables()

        with (
            patch.object(forgeron, "_trigger_skill_design", new_callable=AsyncMock) as mock_tsd,
            patch("forgeron.intent_labeler.IntentLabeler") as mock_labeler_cls,
        ):
            mock_labeler_inst = AsyncMock()
            mock_labeler_inst.label = AsyncMock(return_value=label_result)
            mock_labeler_cls.return_value = mock_labeler_inst

            await forgeron._process_archive(_make_archive_envelope(), mock_redis)

        mock_tsd.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — _process_archive() correction path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_archive_correction_triggers_skill_design(
    tmp_path: Path, mock_redis: AsyncMock
) -> None:
    """When is_correction=True and correction_mode=True, _trigger_skill_design is called."""
    from forgeron.main import Forgeron

    label_result = IntentLabelResult(
        label=None,
        is_correction=True,
        corrected_behavior="Use plain text, not HTML",
        skill_name_hint="send_plain_email",
    )

    from forgeron.config import ForgeonConfig

    cfg = ForgeonConfig(
        correction_mode=True,
        creation_mode=False,
        skills_dir=tmp_path / "skills",
    )

    forgeron = Forgeron.__new__(Forgeron)
    forgeron._config = cfg
    forgeron._annotation_profile = _make_profile()
    forgeron._llm_profile = _make_profile()
    forgeron._consolidation_profile = _make_profile()

    from forgeron.session_store import SessionStore

    forgeron._session_store = SessionStore(db_path=tmp_path / "fg.db")
    await forgeron._session_store._create_tables()

    with (
        patch.object(forgeron, "_trigger_skill_design", new_callable=AsyncMock) as mock_tsd,
        patch("forgeron.intent_labeler.IntentLabeler") as mock_labeler_cls,
    ):
        mock_labeler_inst = AsyncMock()
        mock_labeler_inst.label = AsyncMock(return_value=label_result)
        mock_labeler_cls.return_value = mock_labeler_inst

        env = _make_archive_envelope()
        await forgeron._process_archive(env, mock_redis)

    mock_tsd.assert_called_once()
    call_kwargs = mock_tsd.call_args.kwargs
    assert call_kwargs["corrected_behavior"] == "Use plain text, not HTML"
    assert call_kwargs["skill_name_hint"] == "send_plain_email"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_archive_correction_skipped_when_mode_disabled(
    tmp_path: Path, mock_redis: AsyncMock
) -> None:
    """When correction_mode=False, _trigger_skill_design is NOT called even on correction."""
    from forgeron.main import Forgeron
    from forgeron.config import ForgeonConfig

    label_result = IntentLabelResult(
        label=None,
        is_correction=True,
        corrected_behavior="Do it differently",
        skill_name_hint=None,
    )

    cfg = ForgeonConfig(correction_mode=False, skills_dir=tmp_path / "skills")
    forgeron = Forgeron.__new__(Forgeron)
    forgeron._config = cfg
    forgeron._annotation_profile = _make_profile()
    forgeron._llm_profile = _make_profile()
    forgeron._consolidation_profile = _make_profile()

    from forgeron.session_store import SessionStore

    forgeron._session_store = SessionStore(db_path=tmp_path / "fg.db")
    await forgeron._session_store._create_tables()

    with (
        patch.object(forgeron, "_trigger_skill_design", new_callable=AsyncMock) as mock_tsd,
        patch("forgeron.intent_labeler.IntentLabeler") as mock_labeler_cls,
    ):
        mock_labeler_inst = AsyncMock()
        mock_labeler_inst.label = AsyncMock(return_value=label_result)
        mock_labeler_cls.return_value = mock_labeler_inst

        await forgeron._process_archive(_make_archive_envelope(), mock_redis)

    mock_tsd.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — _trigger_skill_design()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_trigger_skill_design_publishes_task_envelope(
    tmp_path: Path,
) -> None:
    """Happy path: fetches history, then publishes task with force_subagent."""
    from forgeron.main import Forgeron
    from forgeron.config import ForgeonConfig

    cfg = ForgeonConfig(
        correction_mode=True,
        history_read_timeout_seconds=5,
        skills_dir=tmp_path / "skills",
    )

    forgeron = Forgeron.__new__(Forgeron)
    forgeron._config = cfg
    forgeron._annotation_profile = _make_profile()

    history_turns = [[{"type": "human", "content": "hello"}, {"type": "ai", "content": "hi"}]]
    redis = AsyncMock()
    redis.xadd = AsyncMock()
    # BRPOP returns (key, JSON-encoded history_turns)
    redis.brpop = AsyncMock(return_value=(b"key", json.dumps(history_turns).encode()))

    env = _make_archive_envelope()

    await forgeron._trigger_skill_design(
        envelope=env,
        channel="discord",
        sender_id="discord:123",
        corrected_behavior="Use plain text",
        skill_name_hint="send_plain_email",
        redis_conn=redis,
    )

    # Must have published to STREAM_MEMORY_REQUEST (history read request)
    memory_calls = [
        c for c in redis.xadd.call_args_list if c.args[0] == STREAM_MEMORY_REQUEST
    ]
    assert len(memory_calls) == 1

    # Must have published to STREAM_TASKS (task envelope)
    task_calls = [
        c for c in redis.xadd.call_args_list if c.args[0] == STREAM_TASKS
    ]
    assert len(task_calls) == 1

    # Verify the task envelope has force_subagent set
    task_env = Envelope.from_json(task_calls[0].args[1]["payload"])
    assert task_env.action == ACTION_MESSAGE_TASK
    assert task_env.context.get(CTX_FORGERON, {}).get("force_subagent") == "skill-designer"
    assert task_env.context.get(CTX_FORGERON, {}).get("corrected_behavior") == "Use plain text"

    # Must have published notification to STREAM_OUTGOING_PENDING (before BRPOP)
    notif_calls = [
        c for c in redis.xadd.call_args_list if c.args[0] == STREAM_OUTGOING_PENDING
    ]
    assert len(notif_calls) == 1
    notif_env = Envelope.from_json(notif_calls[0].args[1]["payload"])
    assert notif_env.action == ACTION_MESSAGE_OUTGOING_PENDING
    assert "skill" in notif_env.content.lower()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_trigger_skill_design_brpop_timeout_returns_early(
    tmp_path: Path,
) -> None:
    """When BRPOP times out (returns None), the task is NOT published."""
    from forgeron.main import Forgeron
    from forgeron.config import ForgeonConfig

    cfg = ForgeonConfig(
        correction_mode=True,
        history_read_timeout_seconds=1,
        skills_dir=tmp_path / "skills",
    )

    forgeron = Forgeron.__new__(Forgeron)
    forgeron._config = cfg

    redis = AsyncMock()
    redis.xadd = AsyncMock()
    redis.brpop = AsyncMock(return_value=None)  # timeout

    await forgeron._trigger_skill_design(
        envelope=_make_archive_envelope(),
        channel="discord",
        sender_id="discord:123",
        corrected_behavior="Do it right",
        skill_name_hint=None,
        redis_conn=redis,
    )

    # Only the history read XADD + notification XADD, not the task XADD
    task_calls = [
        c for c in redis.xadd.call_args_list if c.args[0] == STREAM_TASKS
    ]
    assert len(task_calls) == 0

    # Notification IS published even on timeout (fires before BRPOP)
    notif_calls = [
        c for c in redis.xadd.call_args_list if c.args[0] == STREAM_OUTGOING_PENDING
    ]
    assert len(notif_calls) == 1
